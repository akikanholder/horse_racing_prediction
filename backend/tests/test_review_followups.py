"""Review follow-ups for section boundaries, denial pages, and the page cache."""

import json
import os
import shutil
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from bs4 import BeautifulSoup, NavigableString

import backend.scrapers.page_cache as page_cache
from backend.scrapers.jra_scraper import JRAScraper
from backend.scrapers.jra_track_scraper import JRATrackScraper, measurement_sections_from_soup
from backend.scrapers.page_cache import _REPLAYABLE_VERSION, CachingHTTPClient
from backend.scrapers.page_fallback import (
    ODDS_CELL_READY_JS,
    PageFallback,
    looks_like_forbidden_page,
    page_block_reason,
)
from backend.services.historical_service import HistoricalService
from backend.services.race_trend_service import RaceTrendService
from backend.utils.date_utils import TOKYO

FIXTURES = Path(__file__).parent / "fixtures"
FETCHED = datetime(2026, 10, 18, 9, 0, tzinfo=TOKYO)
BABA = "https://www.jra.go.jp/keiba/baba/index.html"
BABA_OTHER = "https://www.jra.go.jp/keiba/baba/index2.html"
ODDS = "https://race.netkeiba.com/api/api_get_jra_odds.html"
ACCESS_H = "https://www.jra.go.jp/JRADB/accessH.html"
PAST_RACE_ID = "202511200601"
TODAY_RACE_ID = "202610180501"
NETKEIBA_RACE_ID = "202605030811"
UNDATED = "pw01hli00/03"


class _Gets:
    def __init__(self, gets: dict[str, str]):
        self.gets = gets

    def get(self, url, params=None, headers=None, encoding=None):
        return self.gets.get(url, "")


def _baba(venue: str, body: str = "") -> str:
    return (
        "<html><head><title>馬場情報（"
        f"{venue}競馬場）　JRA</title></head><body>{body}</body></html>"
    )


def test_known_section_labels_leave_the_measurement_and_h4_stays():
    html = (FIXTURES / "jra_baba_section_stops.html").read_text(encoding="utf-8")
    sections = measurement_sections_from_soup(BeautifulSoup(html, "lxml"))
    moisture = sections["含水率"]
    cushion = sections["クッション値"]
    assert "5時30分" in moisture
    assert "8時" not in moisture
    assert "計測情報" in moisture
    assert "7時00分" in cushion
    assert "測定時刻" in cushion
    assert "9時" not in cushion
    assert "散水情報" not in cushion

    course = """
        <h2>含水率</h2>
        <p>10月18日 5時30分</p>
        <h3>使用コース</h3>
        <p>10月18日 10時</p>
    """
    grass = """
        <h2>含水率</h2>
        <p>10月18日 5時30分</p>
        <p>芝の状態</p>
        <p>10月18日 11時</p>
    """
    course_text = measurement_sections_from_soup(BeautifulSoup(course, "lxml"))["含水率"]
    grass_text = measurement_sections_from_soup(BeautifulSoup(grass, "lxml"))["含水率"]
    assert "5時30分" in course_text and "10時" not in course_text
    assert "5時30分" in grass_text and "11時" not in grass_text

    info = JRATrackScraper(
        http_client=_Gets({BABA: html, BABA_OTHER: _baba("京都")}),  # type: ignore[arg-type]
        headless_render="off",
    ).scrape(
        "tokyo",
        fetched_at=FETCHED,
        race_date=date(2026, 10, 18),
        today=date(2026, 10, 18),
    )
    five_thirty = datetime(2026, 10, 18, 5, 30, tzinfo=TOKYO)
    seven = datetime(2026, 10, 18, 7, 0, tzinfo=TOKYO)
    assert info.measured_at == seven
    assert info.turf is not None and info.turf.cushion is not None
    assert info.turf.cushion.measured_at == seven
    assert info.turf.cushion.value == 9.1
    assert info.turf.cushion.source == "jra_html"
    assert info.turf.cushion.fetched_at == FETCHED
    assert info.turf.moisture is not None
    assert info.turf.moisture.measured_at == five_thirty
    assert info.turf.moisture.goal_front == 14.7
    assert info.turf.moisture.source == "jra_html"
    assert info.turf.moisture.fetched_at == FETCHED
    assert info.warning is None


def test_cloudflare_title_must_be_exact_and_japanese_captcha_stops():
    article = (
        "<html><head><title>It was just a moment ago</title></head>"
        "<body><p>The paddock was just a moment from the gate.</p>"
        + ("notes " * 30)
        + "</body></html>"
    )
    assert looks_like_forbidden_page(article) is False
    challenge = (
        "<html><head><title>Just a moment...</title></head><body>"
        + ("please wait " * 40)
        + "</body></html>"
    )
    assert looks_like_forbidden_page(challenge) is True
    marker = (
        "<html><head><title>News</title></head><body>"
        '<form id="cf-challenge"></form>' + ("story " * 40) + "</body></html>"
    )
    assert looks_like_forbidden_page(marker) is True
    captcha = (
        "<html><head><title>CAPTCHA認証</title></head><body>" + ("wait " * 40) + "</body></html>"
    )
    assert looks_like_forbidden_page(captcha) is True
    assert looks_like_forbidden_page(
        "<html><head><title>キャプチャ認証</title></head><body>continue</body></html>"
    )
    derby = (
        "<html><head><title>第403回 日本ダービー</title></head>"
        "<body><h1>403万円</h1><p>確定オッズ 403円</p></body></html>"
    )
    assert looks_like_forbidden_page(derby) is False

    seen: list[str] = []

    def render(url: str) -> str:
        seen.append(url)
        return "<html><body>bypassed</body></html>"

    fetched = PageFallback(object(), site="jra", headless_render=render).load(
        url="https://www.jra.go.jp/keiba/baba/index.html",
        params=None,
        headers=None,
        encoding="cp932",
        fetched_at=FETCHED,
        should_fallback=lambda _html: True,
        still_incomplete=lambda _pages: True,
        html_body=captcha,
    )
    assert seen == []
    assert fetched.status == "blocked"
    assert fetched.blocked_reason == "captcha"
    assert fetched.http_status is None
    assert fetched.login_required is False
    assert fetched.warning == "jra returned a captcha"
    assert fetched.pages == []

    denied = PageFallback(object(), site="jra", headless_render=render).load(
        url="https://www.jra.go.jp/keiba/baba/index.html",
        params=None,
        headers=None,
        encoding="cp932",
        fetched_at=FETCHED,
        should_fallback=lambda _html: True,
        still_incomplete=lambda _pages: True,
        html_body=(
            "<html><head><title>403 Forbidden</title></head>"
            "<body><h1>Access Denied</h1></body></html>"
        ),
    )
    assert seen == []
    assert denied.status == "blocked"
    assert denied.blocked_reason == "http_403"
    assert denied.http_status is None
    assert denied.warning == "jra returned 403"


def test_old_replayable_mark_is_rechecked_and_legacy_denial_is_memoized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    url = "https://example.test/kept"
    ok = "<html><head><title>結果</title></head><body>確定オッズ 3.4</body></html>"
    forbidden = (
        "<html><head><title>403 Forbidden</title></head><body><h1>Access Denied</h1></body></html>"
    )
    calls: list[str] = []

    class _Transport:
        def get(self, url, params=None, headers=None, encoding=None):
            calls.append(url)
            return ok

    client = CachingHTTPClient(tmp_path, transport=_Transport(), min_request_interval=0)
    key = client._cache_key("GET", url, None, None)
    path = client._path(key)
    path.parent.mkdir(parents=True, exist_ok=True)

    def seed(body: str, replayable: object | None) -> None:
        payload: dict[str, object] = {
            "url": url,
            "fetched_at": "2026-10-18T07:00:00",
            "body": body,
        }
        if replayable is not None:
            payload["replayable"] = replayable
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    parses: list[int] = []
    original = page_cache.looks_like_forbidden_page

    def counted(body: str) -> bool:
        parses.append(1)
        return original(body)

    monkeypatch.setattr(page_cache, "looks_like_forbidden_page", counted)
    seed(ok, None)
    assert client.get(url) == ok
    assert client.get(url) == ok
    assert calls == []
    assert parses == [1]
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000))
    assert client.get(url) == ok
    assert parses == [1, 1]

    seed(forbidden, True)
    parses.clear()
    assert client.get(url) == ok
    assert calls == [url]
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["replayable"] == _REPLAYABLE_VERSION
    assert "Access Denied" not in stored["body"]

    seed(forbidden, _REPLAYABLE_VERSION)

    def _must_not_parse(body: str) -> bool:
        raise AssertionError(body)

    monkeypatch.setattr(page_cache, "looks_like_forbidden_page", _must_not_parse)
    monkeypatch.setattr(page_cache, "looks_like_login_wall", _must_not_parse)
    assert client.get(url) == forbidden
    assert calls == [url]


def test_naive_cache_fetched_at_and_computed_at_use_tokyo_offset(tmp_path: Path):
    url = "https://example.test/stamp"
    body = "<html><head><title>結果</title></head><body>確定オッズ</body></html>"

    class _Transport:
        def get(self, url, params=None, headers=None, encoding=None):
            raise AssertionError("cached page was fetched")

    client = CachingHTTPClient(tmp_path, transport=_Transport(), min_request_interval=0)
    path = client._path(client._cache_key("GET", url, None, None))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "url": url,
                "fetched_at": "2026-10-18T07:00:00",
                "replayable": _REPLAYABLE_VERSION,
                "body": body,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    stamped = client.cached_fetched_at("GET", url)
    assert stamped == datetime(2026, 10, 18, 7, 0, tzinfo=TOKYO)
    assert stamped is not None
    assert stamped.isoformat(timespec="seconds").endswith("+09:00")

    path.write_text(
        json.dumps(
            {
                "url": url,
                "fetched_at": "2026-10-18T07:00:00+09:00",
                "replayable": _REPLAYABLE_VERSION,
                "body": body,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert client.cached_fetched_at("GET", url) == datetime(2026, 10, 18, 7, 0, tzinfo=TOKYO)

    historical = HistoricalService(cache_dir=tmp_path / "trends", delay_seconds=0)
    service = RaceTrendService(historical=historical)
    try:
        report = service.get(surface="不明")
    finally:
        historical.close()
    assert report.computed_at.utcoffset() == timedelta(hours=9)
    assert report.computed_at.isoformat(timespec="seconds").endswith("+09:00")


def test_empty_result_odds_payload_is_not_cached(tmp_path: Path):
    calls: list[str] = []

    class _Transport:
        def __init__(self, body: str):
            self.body = body

        def get(self, url, params=None, headers=None, encoding=None):
            calls.append(self.body)
            return self.body

    empty = '{"status":"result","data":""}'
    client = CachingHTTPClient(
        tmp_path / "empty", transport=_Transport(empty), min_request_interval=0
    )
    client.get(ODDS, params={"race_id": "202505010101", "type": "1"})
    client.get(ODDS, params={"race_id": "202505010101", "type": "1"})
    assert calls == [empty, empty]

    calls.clear()
    blank = '{"status":"result","data":null}'
    again = CachingHTTPClient(
        tmp_path / "null", transport=_Transport(blank), min_request_interval=0
    )
    again.get(ODDS)
    again.get(ODDS)
    assert calls == [blank, blank]

    calls.clear()
    full = '{"status":"result","data":{"odds":{"1":{"1":["3.4","0","1"]}}}}'
    kept = CachingHTTPClient(tmp_path / "full", transport=_Transport(full), min_request_interval=0)
    kept.get(ODDS, params={"type": "1"})
    kept.get(ODDS, params={"type": "1"})
    assert calls == [full]

    seeded = CachingHTTPClient(
        tmp_path / "seed", transport=_Transport("should-fetch"), min_request_interval=0
    )
    path = seeded._path(seeded._cache_key("GET", ODDS, None, None))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "url": ODDS,
                "fetched_at": "2026-10-18T07:00:00+09:00",
                "replayable": _REPLAYABLE_VERSION,
                "body": '{"status":"result","data":{}}',
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert seeded.get(ODDS) == "should-fetch"


def test_past_jra_page_is_cached_from_the_request_date(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        page_cache,
        "tokyo_now",
        lambda: datetime(2026, 10, 18, 12, 0, tzinfo=TOKYO),
    )
    calls: list[str] = []

    class _Transport:
        def post(self, url, data=None, headers=None, encoding=None):
            calls.append(json.dumps(data, sort_keys=True))
            return "<html><head><title>払戻</title></head><body>100円</body></html>"

        def get(self, url, params=None, headers=None, encoding=None):
            calls.append(url)
            return "<html><head><title>出馬表</title></head><body>馬番</body></html>"

    client = CachingHTTPClient(tmp_path, transport=_Transport(), min_request_interval=0)
    past = {"cname": UNDATED, "race_id": PAST_RACE_ID}
    client.post(ACCESS_H, data=past)
    client.post(ACCESS_H, data=past)
    assert calls == [json.dumps(past, sort_keys=True)]

    calls.clear()
    today = {"cname": UNDATED, "race_id": TODAY_RACE_ID}
    client.post(ACCESS_H, data=today)
    client.post(ACCESS_H, data=today)
    assert len(calls) == 2

    calls.clear()
    netkeiba = {"cname": UNDATED, "race_id": NETKEIBA_RACE_ID}
    client.post(ACCESS_H, data=netkeiba)
    client.post(ACCESS_H, data=netkeiba)
    assert len(calls) == 2

    calls.clear()
    stated = {"cname": UNDATED, "race_date": "2025-11-20"}
    client.post(ACCESS_H, data=stated)
    client.post(ACCESS_H, data=stated)
    assert calls == [json.dumps(stated, sort_keys=True)]

    calls.clear()
    menu = {"cname": UNDATED}
    client.post(ACCESS_H, data=menu)
    client.post(ACCESS_H, data=menu)
    assert len(calls) == 2

    calls.clear()
    cname_today = {
        "cname": "pw01hde10052026040820261018/A4",
        "race_id": PAST_RACE_ID,
    }
    client.post(ACCESS_H, data=cname_today)
    client.post(ACCESS_H, data=cname_today)
    assert len(calls) == 2

    calls.clear()
    queried = f"https://www.jra.go.jp/JRADB/accessD.html?race_id={PAST_RACE_ID}"
    client.get(queried)
    client.get(queried)
    assert calls == [queried]

    calls.clear()

    class _Denied:
        def post(self, url, data=None, headers=None, encoding=None):
            calls.append("denied")
            return (
                "<html><head><title>403 Forbidden</title></head>"
                "<body><h1>Access Denied</h1></body></html>"
            )

    denied = CachingHTTPClient(tmp_path / "denied", transport=_Denied(), min_request_interval=0)
    denied.post(ACCESS_H, data=past)
    denied.post(ACCESS_H, data=past)
    assert calls == ["denied", "denied"]


def test_jsd_script_on_a_normal_page_is_not_a_challenge():
    html = (FIXTURES / "normal_page_with_jsd_script.html").read_text(encoding="utf-8")
    assert "/cdn-cgi/challenge-platform/scripts/jsd/main.js" in html
    assert page_block_reason(html) is None
    assert looks_like_forbidden_page(html) is False
    seen: list[str] = []

    def render(url: str) -> str:
        seen.append(url)
        return "<html><body>bypassed</body></html>"

    fetched = PageFallback(object(), site="jra", headless_render=render).load(
        url="https://www.jra.go.jp/JRADB/accessD.html",
        params=None,
        headers=None,
        encoding="cp932",
        fetched_at=FETCHED,
        should_fallback=lambda _html: False,
        still_incomplete=lambda _pages: False,
        html_body=html,
    )
    assert fetched.status == "ok"
    assert fetched.blocked_reason is None
    assert fetched.http_status is None
    assert fetched.pages
    assert seen == []

    bare = (
        "<html><head><title>Just a moment...</title></head>"
        "<body><script>cf_chl_opt</script></body></html>"
    )
    assert page_block_reason(bare) == "challenge"
    with_table = (
        "<html><head><title>出馬表</title></head><body>"
        "<script>cf_chl_opt</script><table><tr><td>"
        + ("馬番 " * 20)
        + "</td></tr></table></body></html>"
    )
    assert page_block_reason(with_table) is None


def _stopped_challenge(html: str, seen: list[str]):
    def render(url: str) -> str:
        seen.append(url)
        return "<html><body>bypassed</body></html>"

    return PageFallback(object(), site="jra", headless_render=render).load(
        url="https://www.jra.go.jp/JRADB/accessD.html",
        params=None,
        headers=None,
        encoding="cp932",
        fetched_at=FETCHED,
        should_fallback=lambda _html: True,
        still_incomplete=lambda _pages: True,
        html_body=html,
    )


def test_window_cf_chl_opt_and_challenge_markers_stop_without_headless_or_cache(tmp_path: Path):
    story = "story " * 40
    short = (
        "<html><head><title>レース</title></head>"
        "<body><script>window._cf_chl_opt = {};</script></body></html>"
    )
    doubled = (
        "<html><head><title>レース</title></head>"
        "<body><script>var __cf_chl_opt = {};</script></body></html>"
    )
    running = (
        "<html><head><title>出馬表</title></head><body>"
        f'<div id="cf-challenge-running"></div>{story}</body></html>'
    )
    klass = (
        "<html><head><title>出馬表</title></head><body>"
        f'<div class="cf-browser-verification"></div>{story}</body></html>'
    )
    attention = (
        "<html><head><title>Attention Required! | Cloudflare</title></head>"
        f"<body><p>Sorry, you have been blocked.</p>{story}</body></html>"
    )
    seen: list[str] = []
    for html in (short, doubled, running, klass, attention):
        assert page_block_reason(html) == "challenge"
        fetched = _stopped_challenge(html, seen)
        assert fetched.status == "blocked"
        assert fetched.blocked_reason == "challenge"
        assert fetched.http_status is None
        assert fetched.login_required is False
        assert fetched.warning == "jra returned a challenge"
        assert fetched.pages == []
    assert seen == []

    with_table = (
        "<html><head><title>出馬表</title></head><body>"
        "<script>window._cf_chl_opt = {};</script><table><tr><td>"
        + ("馬番 " * 20)
        + "</td></tr></table></body></html>"
    )
    assert page_block_reason(with_table) is None
    glued = (
        "<html><head><title>レース</title></head>"
        "<body><p>mycf_chl_opt is a horse note.</p></body></html>"
    )
    assert page_block_reason(glued) is None

    class _Transport:
        def __init__(self, body: str):
            self.body = body
            self.calls = 0

        def get(self, url, params=None, headers=None, encoding=None):
            self.calls += 1
            return self.body

    for index, body in enumerate((short, attention)):
        transport = _Transport(body)
        client = CachingHTTPClient(
            tmp_path / str(index),
            transport=transport,
            min_request_interval=0,
        )
        url = "https://www.jra.go.jp/JRADB/accessD.html"
        assert client.get(url) == body
        assert client.get(url) == body
        assert transport.calls == 2
    assert list(tmp_path.rglob("*.json")) == []


def test_odds_wait_stays_until_visible_cells_are_filled():
    node = shutil.which("node")
    assert node
    probe = """
const ready = eval(process.argv[1]);
const cases = JSON.parse(process.argv[2]);
for (const item of cases) {
  global.document = {
    querySelectorAll(sel) {
      const texts = sel.startsWith("span[id") ? item.cells : item.headings;
      return texts.map((text) => ({
        textContent: text,
        getClientRects: () => [{ width: 1, height: 1 }],
      }));
    },
  };
  if (Boolean(ready()) !== item.expect) {
    console.error(JSON.stringify({ item, got: ready() }));
    process.exit(1);
  }
}
"""
    cases = [
        {"cells": ["1.2", "---.-"], "headings": ["確定オッズ"], "expect": False},
        {"cells": ["---.-", "---.-"], "headings": ["最終オッズ"], "expect": False},
        {"cells": [], "headings": ["確定オッズ"], "expect": False},
        {"cells": ["1.2", ""], "headings": [], "expect": False},
        {"cells": ["1.2", "4.5"], "headings": [], "expect": True},
        {"cells": ["1.2", "----"], "headings": ["確定オッズ"], "expect": True},
        {"cells": ["----", "----"], "headings": [], "expect": True},
    ]
    completed = subprocess.run(
        [node, "-e", probe, ODDS_CELL_READY_JS, json.dumps(cases)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def _dom_json(html: str) -> str:
    """Element tree the odds-ready script can query, including layout boxes."""

    def node(el) -> dict:
        children = []
        own: list[str] = []
        for child in el.children:
            if isinstance(child, NavigableString):
                own.append(str(child))
            elif getattr(child, "name", None):
                children.append(node(child))
        classes = el.get("class") or []
        return {
            "tag": el.name,
            "id": el.get("id") or "",
            "className": " ".join(classes) if isinstance(classes, list) else str(classes),
            "style": el.get("style") or "",
            "own": "".join(own),
            "children": children,
        }

    soup = BeautifulSoup(html, "lxml")
    root = soup.body if soup.body is not None else soup
    return json.dumps(node(root))


def _odds_cells_ready(html: str) -> bool:
    node = shutil.which("node")
    assert node
    probe = """
const ready = eval(process.argv[1]);
const root = JSON.parse(process.argv[2]);
function link(el, parent) {
  el.parent = parent;
  el.children.forEach((child) => link(child, el));
}
link(root, null);
function textContent(el) {
  return el.own + el.children.map(textContent).join("");
}
function styleHides(style) {
  const compact = String(style || "").replace(/\\s+/g, "").toLowerCase();
  return compact.includes("display:none") || compact.includes("visibility:hidden");
}
function hidden(el) {
  for (let cur = el; cur; cur = cur.parent) {
    if (styleHides(cur.style)) return true;
  }
  return false;
}
function hasBox(el) {
  if (hidden(el)) return false;
  if (el.own.replace(/\\s+/g, "")) return true;
  if (el.children.some(hasBox)) return true;
  // A wrapper whose children are all hidden collapses. A visible leaf,
  // including an empty odds cell, still has a box.
  return el.children.length === 0;
}
function matches(el, sel) {
  const attr = sel.match(/^(\\w+)\\[id\\^="([^"]+)"\\]$/);
  if (attr) return el.tag === attr[1] && el.id.startsWith(attr[2]);
  const cls = sel.match(/^(\\w+)\\.([\\w-]+)$/);
  if (cls) {
    return el.tag === cls[1] && el.className.split(/\\s+/).includes(cls[2]);
  }
  return false;
}
function walk(el, visit) {
  visit(el);
  el.children.forEach((child) => walk(child, visit));
}
function element(el) {
  return {
    textContent: textContent(el),
    getClientRects: () => (hasBox(el) ? [{ width: 1, height: 1 }] : []),
  };
}
global.document = {
  querySelectorAll(selector) {
    const parts = selector.split(",").map((part) => part.trim());
    const found = [];
    walk(root, (el) => {
      if (parts.some((part) => matches(el, part))) found.push(element(el));
    });
    return found;
  },
};
process.stdout.write(ready() ? "true" : "false");
"""
    completed = subprocess.run(
        [node, "-e", probe, ODDS_CELL_READY_JS, _dom_json(html)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip() == "true"


def test_hidden_oddsmin_spans_do_not_block_readiness():
    html = (FIXTURES / "odds_hidden_oddsmin.html").read_text(encoding="utf-8")
    assert 'id="oddsmin-4-0102" style="display: none;"' in html
    assert html.count('id="oddsmin-') >= 8
    assert _odds_cells_ready(html) is True
    still_loading = html.replace(">12.4<", ">---.-<", 1)
    assert _odds_cells_ready(still_loading) is False
    visible_empty = html.replace(
        '<span id="odds-4-0103">8.1</span>',
        '<span id="odds-4-0103"></span>',
        1,
    )
    assert _odds_cells_ready(visible_empty) is False


def test_my_challenge_form_is_not_a_challenge_token():
    story = "story " * 40
    aliased = (
        "<html><head><title>出馬表</title></head><body>"
        f'<form id="my-challenge-form"></form><div class="my-challenge-form"></div>{story}'
        "</body></html>"
    )
    assert page_block_reason(aliased) is None
    assert looks_like_forbidden_page(aliased) is False
    exact = (
        "<html><head><title>出馬表</title></head><body>"
        f'<form name="challenge-form"></form>{story}</body></html>'
    )
    assert page_block_reason(exact) == "challenge"
    classes = (
        "<html><head><title>出馬表</title></head><body>"
        f'<div class="panel cf-browser-verification"></div>{story}</body></html>'
    )
    assert page_block_reason(classes) == "challenge"


def test_prediction_fixtures_stay_tracked():
    root = Path(__file__).resolve().parents[2]
    fixture = Path("backend/tests/fixtures/predictions/example_record.json")
    assert (root / fixture).is_file()

    def ignored(path: str) -> bool:
        completed = subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", "--", path],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode in {0, 1}, completed.stderr
        return completed.returncode == 0

    assert ignored(fixture.as_posix()) is False
    assert ignored("backend/tests/fixtures/predictions/another_record.json") is False
    assert ignored("backend/tests/fixtures/predictions/20261018.jsonl.lock") is True
    assert ignored("backend/predictions/out.json") is True
    assert ignored("predictions/out.json") is True


def test_race_date_metadata_does_not_change_the_jra_form_body(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        page_cache,
        "tokyo_now",
        lambda: datetime(2026, 10, 18, 12, 0, tzinfo=TOKYO),
    )
    bodies: list[dict] = []

    class _Transport:
        def post(self, url, data=None, headers=None, encoding=None):
            bodies.append(dict(data or {}))
            return "<html><head><title>払戻</title></head><body>100円</body></html>"

        def get(self, url, params=None, headers=None, encoding=None):
            raise AssertionError(url)

    client = CachingHTTPClient(tmp_path, transport=_Transport(), min_request_interval=0)
    scraper = JRAScraper(http_client=client, headless_render="off", fetch_mode="html")  # type: ignore[arg-type]
    past = date(2025, 11, 20)
    first = scraper._post_jra(ACCESS_H, UNDATED, encoding="cp932", race_date=past)
    second = scraper._post_jra(ACCESS_H, UNDATED, encoding="cp932", race_date=past)
    assert first == second
    assert bodies == [{"cname": UNDATED}]

    bodies.clear()
    scraper._post_jra(ACCESS_H, UNDATED, encoding="cp932", race_date=date(2026, 10, 18))
    scraper._post_jra(ACCESS_H, UNDATED, encoding="cp932", race_date=date(2026, 10, 18))
    assert bodies == [{"cname": UNDATED}, {"cname": UNDATED}]
