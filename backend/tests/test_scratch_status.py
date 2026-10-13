"""Scratch and exclusion marks on the race card and on every odds quote.

One fixture race has both a scratch (取消 / 出走取消) and an exclusion
(除外 / 競走除外). A 403 or a login wall stops the chain. Headless is
optional and this file skips the real browser when it is not installed.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from bs4 import BeautifulSoup

from backend.cli import _format_odds_quote, cmd_odds, cmd_race_card
from backend.models.odds import NonRunner, OddsQuote, RaceOdds
from backend.models.race import Race, RaceCard, RaceEntry
from backend.scrapers.jra_scraper import JRAScraper
from backend.scrapers.netkeiba_odds_scraper import (
    parse_netkeiba_sanrenpuku_odds,
    parse_netkeiba_umaren_odds,
    parse_netkeiba_wide_odds,
    parse_netkeiba_win_odds,
)
from backend.scrapers.netkeiba_scraper import NetkeibaScraper
from backend.scrapers.page_fallback import headless_browser_installed, render_headless
from backend.scrapers.runner_status import (
    HEADLESS_UNAVAILABLE,
    JRA_ODDS_FORBIDDEN,
    JRA_ODDS_LOGIN,
    SCRATCH_STATUS_MISSING,
)
from backend.services.race_service import RaceService

TOKYO = timezone(timedelta(hours=9))
FETCHED = datetime(2026, 10, 12, 9, 30, tzinfo=TOKYO)
RACE_ID = "202606040811"
SHUTUBA_URL = "https://race.netkeiba.com/race/shutuba.html"
STATUS_URL = "https://race.netkeiba.com/api/shutuba_status.json"
JRA_STATUS_URL = "https://www.jra.go.jp/api/shutuba_status.json"
ODDS_STATUS_URL = "https://www.jra.go.jp/api/odds_status.json"

INDEX_HTML = """
<html><body>
<a onclick="doAction('/JRADB/accessD.html','pw01dli00/F3');">出馬表</a>
<a onclick="doAction('/JRADB/accessD.html','pw01drl00062026040820260926/27');">4回中山8日</a>
</body></html>
"""

LIST_HTML = """
<html><body>
<a href="/JRADB/accessD.html?CNAME=pw01dde0106202604081120260926%2F2A">11R</a>
</body></html>
"""


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    error = requests.HTTPError(f"http {status}")
    error.response = response
    return error


def _jra_row(number: int, name: str, *, mark: str = "", odds: str = "5.0") -> str:
    """One accessD starter row. ``mark`` is a cell whose text is the printed label."""
    bracket = ((number - 1) % 8) + 1
    mark_cell = f"<td>{mark}</td>" if mark else ""
    return f"""
    <tr>
      <td class="waku"><img alt="枠{bracket}白" src="/waku/{bracket}.png"></td>
      <td class="num">{number}</td>
      <td class="horse">
        <div class="name"><a>{name}</a></div>
        <div class="odds"><span class="num"><strong>{odds}</strong></span></div>
        <p class="trainer"><a>調教師</a></p>
      </td>
      <td class="jockey"><p class="age">牡4</p><p class="jockey">騎手</p></td>
      {mark_cell}
    </tr>
    """


def _jra_card_html(*rows: str, script: str = "", extra: str = "") -> str:
    body_rows = "\n".join(rows)
    script_tag = f'<script type="application/json">{script}</script>' if script else ""
    return f"""
    <html><body>
    <div class="race_header">
      <div class="cell date">2026年9月26日（土曜） 4回中山8日</div>
      <div class="cell time">発走時刻：<strong>15時30分</strong></div>
      <div class="race_number"><img alt="11レース" src="/race_num_11.png"></div>
      <span class="race_name">秋風ステークス</span>
      <div class="type"><div class="cell course">コース：1,600<span class="unit">メートル</span>
        <span class="detail">（芝・右）</span></div></div>
    </div>
    {extra}
    <table class="basic">
      <tr><th>枠</th><th>馬番</th><th>馬名</th><th>騎手</th></tr>
      {body_rows}
    </table>
    {script_tag}
    </body></html>
    """


JRA_MARKED = _jra_card_html(
    _jra_row(1, "エフォートレス", odds="11.8"),
    _jra_row(2, "サウンドムーブ", mark="取消", odds="----"),
    _jra_row(4, "キタサン", odds="----"),
    _jra_row(5, "ゴールド", mark="除外", odds="----"),
)

RUNNERS_JSON = json.dumps(
    {
        "runners": [
            {
                "horse_number": 1,
                "status": "entered",
                "horse_name": "エフォートレス",
                "bracket_number": 1,
            },
            {
                "horse_number": 2,
                "status_label": "出走取消",
                "horse_name": "サウンドムーブ",
                "bracket_number": 2,
            },
            {
                "horse_number": 5,
                "status_label": "競走除外",
                "horse_name": "ゴールド",
                "bracket_number": 5,
            },
            {"horse_number": 9, "status": "scratched", "status_label": "取消"},
        ]
    }
)


def _netkeiba_row(number: int, name: str, horse_id: str, mark: str = "", css: str = "") -> str:
    mark_cell = f'<td class="{css}">{mark}</td>' if mark or css else ""
    return f"""
    <tr>
      <td>{((number - 1) % 8) + 1}</td>
      <td>{number}</td>
      <td></td>
      <td><a href="/horse/{horse_id}">{name}</a></td>
      {mark_cell}
    </tr>
    """


def _netkeiba_html(*rows: str, script: str = "", script_url: str = "") -> str:
    url_line = f"<script>url: '{script_url}'</script>" if script_url else ""
    payload = f'<script type="application/json">{script}</script>' if script else ""
    body_rows = "\n".join(rows)
    return f"""
    <html><body>
      <h1 class="RaceName">秋風ステークス</h1>
      <div class="RaceData01">15:30発走 / 芝1600m</div>
      <div class="RaceData02"><span>4回</span><span>中山</span><span>8日目</span></div>
      {url_line}
      <table class="Shutuba_Table">
        <tr><th>枠</th><th>馬番</th><th>印</th><th>馬名</th><th>区分</th></tr>
        {body_rows}
      </table>
      {payload}
    </body></html>
    """


NETKEIBA_MARKED = _netkeiba_html(
    _netkeiba_row(1, "エフォートレス", "111"),
    _netkeiba_row(2, "サウンドムーブ", "222", "出走取消"),
    _netkeiba_row(4, "キタサン", "444", "未定", css="cancel"),
    _netkeiba_row(5, "ゴールド", "555", "競走除外"),
    _netkeiba_row(6, "ブランク", "666", css="cancel"),
)


class _Gets:
    """GET map keyed by URL, ignoring the query string."""

    def __init__(self, gets: dict | None = None, errors: dict | None = None):
        self.gets = gets or {}
        self.errors = errors or {}
        self.urls: list[str] = []
        self.encodings: list[str | None] = []

    def get(self, url, params=None, headers=None, encoding=None):
        self.urls.append(url)
        self.encodings.append(encoding)
        if url in self.errors:
            raise self.errors[url]
        return self.gets.get(url, "")


class _Posts:
    def __init__(self, pages: dict[str, str], *, odds_error: Exception | None = None):
        self.pages = pages
        self.odds_error = odds_error
        self.calls: list[tuple[str, str]] = []

    def post(self, url, data=None, headers=None, encoding=None):
        cname = (data or {}).get("cname", "")
        self.calls.append((url, cname))
        if self.odds_error is not None and url == JRAScraper.ACCESS_O_URL:
            raise self.odds_error
        if cname not in self.pages and url in self.pages:
            return self.pages[url]
        return self.pages[cname]

    def get(self, url, params=None, headers=None, encoding=None):
        raise AssertionError(f"unexpected GET {url}")


def _jra(pages: dict[str, str], **kwargs) -> JRAScraper:
    scraper = JRAScraper(**kwargs)
    scraper.http_client.post = _Posts(pages).post  # type: ignore[method-assign]
    return scraper


def _by_number(card: RaceCard) -> dict[int, RaceEntry]:
    return {entry.horse_number: entry for entry in card.entries}


def test_jra_card_keeps_scratch_and_exclusion_labels():
    pages = {
        "pw01dli00/F3": INDEX_HTML,
        "pw01drl00062026040820260926/27": LIST_HTML,
        "pw01dde0106202604081120260926/2A": JRA_MARKED,
    }
    scraper = _jra(pages, headless_render="off")
    card = scraper.get_race_card(RACE_ID)

    assert card is not None
    assert card.warning is None
    horses = _by_number(card)
    assert set(horses) == {1, 2, 4, 5}
    assert horses[1].status == "entered"
    assert horses[1].status_label is None
    assert horses[1].odds == 11.8
    assert horses[1].source == "jra_html"
    assert horses[1].fetched_at is not None
    assert horses[2].status == "scratched"
    assert horses[2].status_label == "取消"
    assert horses[2].odds is None
    assert horses[4].status == "entered"
    assert horses[4].status_label is None
    assert horses[4].odds is None
    assert horses[5].status == "excluded"
    assert horses[5].status_label == "除外"
    assert horses[5].source == "jra_html"
    assert {entry.source for entry in card.entries} == {"jra_html"}


def test_jra_embedded_json_supplies_status_without_parsing_the_script_as_html():
    shell = _jra_card_html(script=RUNNERS_JSON)
    pages = {
        "pw01dli00/F3": INDEX_HTML,
        "pw01drl00062026040820260926/27": LIST_HTML,
        "pw01dde0106202604081120260926/2A": shell,
    }
    scraper = _jra(pages, headless_render="off")
    card = scraper.get_race_card(RACE_ID)

    assert card is not None
    assert card.warning is None
    horses = _by_number(card)
    assert set(horses) == {1, 2, 5}
    assert horses[1].status == "entered"
    assert horses[1].status_label is None
    assert horses[2].status == "scratched"
    assert horses[2].status_label == "出走取消"
    assert horses[5].status == "excluded"
    assert horses[5].status_label == "競走除外"
    assert {entry.source for entry in card.entries} == {"jra_js"}
    assert 9 not in horses


def test_jra_same_site_status_url_is_fetched_and_other_hosts_are_not():
    shell = _jra_card_html(
        extra=(
            "<script>url: '/api/shutuba_status.json', "
            "url: 'https://evil.example/shutuba', "
            "url: '/login/shutuba'</script>"
        )
    )
    pages = {
        "pw01dli00/F3": INDEX_HTML,
        "pw01drl00062026040820260926/27": LIST_HTML,
        "pw01dde0106202604081120260926/2A": shell,
    }
    http = _Gets(gets={JRA_STATUS_URL: RUNNERS_JSON})
    scraper = JRAScraper(headless_render="off")
    posts = _Posts(pages)
    scraper.http_client.post = posts.post  # type: ignore[method-assign]
    scraper.http_client.get = http.get  # type: ignore[method-assign]

    card = scraper.get_race_card(RACE_ID)

    assert card is not None
    horses = _by_number(card)
    assert horses[2].status == "scratched"
    assert horses[2].status_label == "出走取消"
    assert horses[5].status == "excluded"
    assert horses[5].source == "jra_js"
    assert http.urls == [JRA_STATUS_URL]


def test_jra_403_stops_before_headless_and_does_not_parse_the_body():
    calls: list[str] = []

    def headless(url: str) -> str:
        calls.append(url)
        return JRA_MARKED

    scraper = JRAScraper(headless_render=headless)

    def fail_post(url, data=None, headers=None, encoding=None):
        raise _http_error(403)

    scraper.http_client.post = fail_post  # type: ignore[method-assign]
    assert scraper.get_race_card(RACE_ID) is None
    assert calls == []


def test_jra_same_site_403_stops_before_headless():
    shell = _jra_card_html(extra="<script>url: '/api/shutuba_status.json'</script>")
    pages = {
        "pw01dli00/F3": INDEX_HTML,
        "pw01drl00062026040820260926/27": LIST_HTML,
        "pw01dde0106202604081120260926/2A": shell,
    }
    calls: list[str] = []

    def headless(url: str) -> str:
        calls.append(url)
        return JRA_MARKED

    http = _Gets(errors={JRA_STATUS_URL: _http_error(403)})
    scraper = JRAScraper(headless_render=headless)
    scraper.http_client.post = _Posts(pages).post  # type: ignore[method-assign]
    scraper.http_client.get = http.get  # type: ignore[method-assign]

    card = scraper.get_race_card(RACE_ID)

    assert calls == []
    assert http.urls == [JRA_STATUS_URL]
    assert card is not None
    assert card.entries == []
    assert card.warning == "JRA shutuba returned 403"


def test_jra_login_wall_is_not_parsed():
    login = """
    <html><head><title>ログイン</title></head><body>
      <input type="password" name="password">
      <table class="basic">
        <tr>
          <td class="waku"><img alt="枠1白"></td>
          <td class="num">2</td>
          <td class="horse"><div class="name">偽馬</div><p class="trainer"><a>調教師</a></p></td>
          <td class="jockey"><p class="jockey">騎手</p></td>
          <td>取消</td>
        </tr>
      </table>
    </body></html>
    """
    pages = {
        "pw01dli00/F3": INDEX_HTML,
        "pw01drl00062026040820260926/27": LIST_HTML,
        "pw01dde0106202604081120260926/2A": login,
    }
    calls: list[str] = []
    scraper = _jra(pages, headless_render=lambda url: calls.append(url) or JRA_MARKED)
    assert scraper.get_race_card(RACE_ID) is None
    assert calls == []


def test_missing_headless_browser_warns_and_does_not_invent_a_scratch(monkeypatch):
    monkeypatch.setattr(
        "backend.scrapers.page_fallback.headless_browser_installed", lambda: False
    )
    shell = _jra_card_html()
    pages = {
        "pw01dli00/F3": INDEX_HTML,
        "pw01drl00062026040820260926/27": LIST_HTML,
        "pw01dde0106202604081120260926/2A": shell,
    }
    scraper = _jra(pages, headless_render="auto")
    card = scraper.get_race_card(RACE_ID)

    assert card is not None
    assert card.entries == []
    assert card.warning == HEADLESS_UNAVAILABLE
    assert "取消" not in (card.warning or "")


def test_headless_render_fills_scratch_and_exclusion():
    shell = _jra_card_html()
    pages = {
        "pw01dli00/F3": INDEX_HTML,
        "pw01drl00062026040820260926/27": LIST_HTML,
        "pw01dde0106202604081120260926/2A": shell,
    }
    seen: list[str] = []

    def headless(url: str) -> str:
        seen.append(url)
        return JRA_MARKED

    scraper = _jra(pages, headless_render=headless)
    card = scraper.get_race_card(RACE_ID)

    assert card is not None
    assert seen
    horses = _by_number(card)
    assert horses[2].status == "scratched"
    assert horses[2].status_label == "取消"
    assert horses[5].status == "excluded"
    assert horses[5].status_label == "除外"
    assert {entry.source for entry in card.entries} == {"headless"}
    assert card.warning is None


def test_netkeiba_card_keeps_printed_labels_and_unknown():
    http = _Gets(gets={SHUTUBA_URL: NETKEIBA_MARKED})
    scraper = NetkeibaScraper(http_client=http, headless_render="off")  # type: ignore[arg-type]
    card = scraper.get_race_card(RACE_ID)

    assert card is not None
    assert card.warning is None
    assert http.urls == [SHUTUBA_URL]
    assert http.encodings == ["euc-jp"]
    horses = _by_number(card)
    assert horses[1].status == "entered"
    assert horses[1].status_label is None
    assert horses[1].horse_id == "111"
    assert horses[1].source == "netkeiba_html"
    assert horses[2].status == "scratched"
    assert horses[2].status_label == "出走取消"
    assert horses[4].status == "unknown"
    assert horses[4].status_label == "未定"
    assert horses[5].status == "excluded"
    assert horses[5].status_label == "競走除外"
    assert horses[6].status == "entered"
    assert horses[6].status_label is None


def test_netkeiba_embedded_json_is_a_later_page():
    shell = _netkeiba_html(script=RUNNERS_JSON)
    http = _Gets(gets={SHUTUBA_URL: shell})
    scraper = NetkeibaScraper(http_client=http, headless_render="off")  # type: ignore[arg-type]
    card = scraper.get_race_card(RACE_ID)

    assert card is not None
    horses = _by_number(card)
    assert horses[2].status_label == "出走取消"
    assert horses[5].status == "excluded"
    assert horses[5].status_label == "競走除外"
    assert {entry.source for entry in card.entries} == {"netkeiba_js"}
    assert 9 not in horses


def test_netkeiba_403_and_login_stop_before_headless():
    calls: list[str] = []

    def headless(url: str) -> str:
        calls.append(url)
        return NETKEIBA_MARKED

    blocked = NetkeibaScraper(
        http_client=_Gets(errors={SHUTUBA_URL: _http_error(403)}),  # type: ignore[arg-type]
        headless_render=headless,
    )
    assert blocked.get_race_card(RACE_ID) is None

    login = """
    <html><head><title>ログイン</title></head><body>
      <input type="password">
      <h1 class="RaceName">秋風ステークス</h1>
      <table class="Shutuba_Table"><tr><td>1</td><td>2</td><td></td><td>偽馬</td><td>取消</td></tr></table>
    </body></html>
    """
    walled = NetkeibaScraper(
        http_client=_Gets(gets={SHUTUBA_URL: login}),  # type: ignore[arg-type]
        headless_render=headless,
    )
    assert walled.get_race_card(RACE_ID) is None
    assert calls == []


def test_netkeiba_same_site_403_does_not_read_that_body():
    shell = _netkeiba_html(script_url="/api/shutuba_status.json")
    secret = json.dumps(
        {
            "runners": [
                {
                    "horse_number": 8,
                    "status_label": "取消",
                    "horse_name": "ダミー",
                    "bracket_number": 8,
                }
            ]
        }
    )
    calls: list[str] = []
    http = _Gets(
        gets={SHUTUBA_URL: shell},
        errors={STATUS_URL: _http_error(403)},
    )
    http.gets[STATUS_URL] = secret
    scraper = NetkeibaScraper(
        http_client=http,  # type: ignore[arg-type]
        headless_render=lambda url: calls.append(url) or NETKEIBA_MARKED,
    )
    card = scraper.get_race_card(RACE_ID)

    assert calls == []
    assert STATUS_URL in http.urls
    assert card is not None
    assert all(entry.horse_number != 8 for entry in card.entries)
    assert card.warning == "netkeiba shutuba returned 403"


def test_merge_prefers_jra_status_and_keeps_netkeiba_ids():
    when = datetime(2026, 10, 12, 8, 0, tzinfo=TOKYO)
    netkeiba = RaceCard(
        race=Race(
            race_id=RACE_ID,
            race_name="秋風",
            race_number=11,
            race_date=datetime(2026, 9, 26).date(),
            venue="中山",
            distance=1600,
            track_type="芝",
        ),
        entries=[
            RaceEntry(
                bracket_number=1,
                horse_number=1,
                horse_name="エフォートレス",
                horse_id="111",
                status="entered",
                source="netkeiba_html",
                fetched_at=when,
            ),
            RaceEntry(
                bracket_number=2,
                horse_number=2,
                horse_name="サウンドムーブ",
                horse_id="222",
                status="entered",
                source="netkeiba_html",
                fetched_at=when,
            ),
        ],
    )
    jra = RaceCard(
        race=netkeiba.race.model_copy(update={"race_name": "秋風ステークス"}),
        entries=[
            RaceEntry(
                bracket_number=2,
                horse_number=2,
                horse_name="サウンドムーブ",
                status="scratched",
                status_label="取消",
                source="jra_html",
                fetched_at=FETCHED,
            ),
            RaceEntry(
                bracket_number=5,
                horse_number=5,
                horse_name="ゴールド",
                status="excluded",
                status_label="除外",
                source="jra_html",
                fetched_at=FETCHED,
            ),
        ],
    )
    merged = RaceService()._merge_race_cards(netkeiba, jra)

    assert merged is not None
    assert merged.warning is None
    horses = _by_number(merged)
    assert horses[1].status == "entered"
    assert horses[1].horse_id == "111"
    assert horses[1].source == "netkeiba_html"
    assert horses[2].status == "scratched"
    assert horses[2].status_label == "取消"
    assert horses[2].source == "jra_html"
    assert horses[2].fetched_at == FETCHED
    assert horses[2].horse_id == "222"
    assert horses[5].status == "excluded"
    assert horses[5].status_label == "除外"


def test_merge_warns_when_status_is_missing():
    race = Race(
        race_id=RACE_ID,
        race_name="秋風",
        race_number=11,
        race_date=datetime(2026, 9, 26).date(),
        venue="中山",
        distance=1600,
        track_type="芝",
    )
    entry = RaceEntry(bracket_number=1, horse_number=1, horse_name="エフォートレス")
    merged = RaceService()._merge_race_cards(
        RaceCard(race=race, entries=[entry]),
        RaceCard(race=race, entries=[entry]),
    )
    assert merged is not None
    assert merged.entries[0].status is None
    assert merged.warning == SCRATCH_STATUS_MISSING


def _odds_payload(key: str, odds_map: dict) -> dict:
    return {
        "status": "middle",
        "data": {
            "official_datetime": "2026-10-12 09:00:00",
            "odds": {key: odds_map},
        },
    }


def test_win_cells_keep_dashes_and_scratch_labels_with_null_odds():
    snapshot = parse_netkeiba_win_odds(
        _odds_payload(
            "1",
            {
                "01": ["3.2", "0", "1"],
                "02": ["取消", "", ""],
                "04": ["----", "", ""],
                "05": ["除外", "", ""],
                "06": ["---.-", "", ""],
                "07": ["", "", ""],
            },
        ),
        RACE_ID,
        fetched_at=FETCHED,
    )
    by_horse = {quote.combination: quote for quote in snapshot.quotes}
    assert set(by_horse) == {"1", "2", "4", "5"}
    assert by_horse["1"].odds == 3.2
    assert by_horse["1"].withheld is False
    assert by_horse["1"].non_runner is False
    assert by_horse["2"].odds is None
    assert by_horse["2"].withheld is True
    assert by_horse["2"].non_runner is True
    assert by_horse["4"].odds is None
    assert by_horse["4"].withheld is True
    assert by_horse["4"].non_runner is False
    assert by_horse["5"].odds is None
    assert by_horse["5"].non_runner is True
    runners = {runner.horse_number: runner for runner in snapshot.non_runners or []}
    assert set(runners) == {2, 5}
    assert runners[2].status == "scratched"
    assert runners[2].status_label == "取消"
    assert runners[2].source == "netkeiba_js"
    assert runners[2].fetched_at == FETCHED
    assert runners[5].status == "excluded"
    assert runners[5].status_label == "除外"
    assert snapshot.non_runners is not None


def test_combination_pages_keep_printed_blanks_and_drop_missing_rows():
    wide = parse_netkeiba_wide_odds(
        _odds_payload(
            "5",
            {
                "0102": ["----", "8.0", "1"],
                "0103": ["2.1", "3.4", "2"],
                "0205": ["取消", "12.0", "3"],
            },
        ),
        RACE_ID,
        fetched_at=FETCHED,
    )
    wide_by = {quote.combination: quote for quote in wide.quotes}
    assert set(wide_by) == {"1-2", "1-3", "2-5"}
    assert wide_by["1-2"].odds_min is None
    assert wide_by["1-2"].odds_max is None
    assert wide_by["1-2"].withheld is True
    assert wide_by["1-2"].non_runner is False
    assert wide_by["1-3"].odds_min == 2.1
    assert wide_by["1-3"].odds_max == 3.4
    assert wide_by["2-5"].odds_min is None
    assert wide_by["2-5"].odds_max is None
    assert wide_by["2-5"].withheld is True
    assert wide_by["2-5"].non_runner is True
    assert wide.non_runners == []

    umaren = parse_netkeiba_umaren_odds(
        _odds_payload(
            "4",
            {
                "0102": ["15.2", "0", "1"],
                "0203": ["----", "", ""],
                "0204": ["出走取消", "", ""],
            },
        ),
        RACE_ID,
        fetched_at=FETCHED,
    )
    umaren_by = {quote.combination: quote for quote in umaren.quotes}
    assert "2-5" not in umaren_by
    assert umaren_by["1-2"].odds == 15.2
    assert umaren_by["1-2"].withheld is False
    assert umaren_by["2-3"].odds is None
    assert umaren_by["2-3"].withheld is True
    assert umaren_by["2-4"].odds is None
    assert umaren_by["2-4"].non_runner is True
    assert umaren_by["2-4"].withheld is True

    trio = parse_netkeiba_sanrenpuku_odds(
        _odds_payload(
            "7",
            {
                "010203": ["40.0", "0", "1"],
                "010205": ["競走除外", "", ""],
                "010305": ["----", "", ""],
            },
        ),
        RACE_ID,
        fetched_at=FETCHED,
    )
    trio_by = {quote.combination: quote for quote in trio.quotes}
    assert "1-2-4" not in trio_by
    assert trio_by["1-2-3"].odds == 40.0
    assert trio_by["1-2-5"].odds is None
    assert trio_by["1-2-5"].withheld is True
    assert trio_by["1-2-5"].non_runner is True
    assert trio_by["1-3-5"].odds is None
    assert trio_by["1-3-5"].withheld is True
    assert trio_by["1-3-5"].non_runner is False
    assert trio.non_runners == []


def _market(key: str, odds_map: dict) -> str:
    return json.dumps(_odds_payload(key, odds_map))


WIN_API = _market(
    "1",
    {
        "01": ["2.5", "0", "1"],
        "02": ["9.9", "0", "2"],
        "03": ["取消", "", ""],
    },
)
UMAREN_API = _market(
    "4",
    {
        "0102": ["8.5", "0", "1"],
        "0103": ["4.0", "0", "2"],
        "0203": ["12.0", "0", "3"],
    },
)
WIDE_API = _market(
    "5",
    {
        "0102": ["1.5", "2.5", "1"],
        "0103": ["3.0", "4.0", "2"],
        "0203": ["5.0", "6.0", "3"],
    },
)
TRIO_API = _market(
    "7",
    {
        "010203": ["20.0", "0", "1"],
        "010305": ["30.0", "0", "2"],
        "020304": ["取消", "", ""],
        "010304": ["----", "", ""],
    },
)
API_BY_TYPE = {"1": WIN_API, "4": UMAREN_API, "5": WIDE_API, "7": TRIO_API}


class _OddsHTTP:
    def __init__(self):
        self.types: list[str] = []

    def get(self, url, params=None, headers=None, encoding=None):
        bet = (params or {}).get("type", "")
        self.types.append(bet)
        return API_BY_TYPE[bet]

    def close(self):
        return None


ODDS_HTML = """
<html><body>
<table>
  <tr><td class="num">1</td><td class="odds">1.1</td></tr>
  <tr><td class="num">2</td><td class="odds">取消</td></tr>
  <tr><td class="num">5</td><td class="odds">除外</td></tr>
  <tr data-combination="1-2"><td>1-2</td><td>99.9</td></tr>
  <tr data-combination="2-3"><td>2-3</td><td>----</td></tr>
  <tr data-combination="2-3-4"><td>2-3-4</td><td>取消</td></tr>
</table>
</body></html>
"""

SHUTUBA_WITH_ODDS = JRA_MARKED.replace(
    "</body>",
    "<a onclick=\"doAction('/JRADB/accessO.html','pw01ode0106202604081120260926/AB');\">オッズ</a></body>",
)


def _service_with_jra(pages: dict[str, str], *, odds_error: Exception | None = None):
    from backend.scrapers.netkeiba_odds_scraper import NetkeibaOddsScraper

    headless_calls: list[str] = []
    jra = JRAScraper(headless_render=lambda url: headless_calls.append(url) or ODDS_HTML)
    posts = _Posts(pages, odds_error=odds_error)
    jra.http_client.post = posts.post  # type: ignore[method-assign]

    def reject_card(race_id: str):
        raise AssertionError(f"odds lookup must not load the race card for {race_id}")

    jra.get_race_card = reject_card  # type: ignore[method-assign]
    service = RaceService()
    service._odds_scraper = NetkeibaOddsScraper(http_client=_OddsHTTP())  # type: ignore[arg-type]
    service._jra_scraper = jra
    return service, posts, headless_calls


def test_jra_odds_marks_replace_netkeiba_and_keep_printed_prices():
    pages = {
        "pw01dli00/F3": INDEX_HTML,
        "pw01drl00062026040820260926/27": LIST_HTML,
        "pw01dde0106202604081120260926/2A": SHUTUBA_WITH_ODDS,
        "pw01ode0106202604081120260926/AB": ODDS_HTML,
    }
    service, posts, headless_calls = _service_with_jra(pages)
    snapshot = service.get_race_odds(
        RACE_ID,
        bet_types=["win", "wide", "umaren", "sanrenpuku"],
        prefer="live",
    )

    assert headless_calls == []
    assert JRAScraper.ACCESS_O_URL in {url for url, _cname in posts.calls}
    assert snapshot.warning is None
    runners = {runner.horse_number: runner for runner in snapshot.non_runners or []}
    assert set(runners) == {2, 5}
    assert runners[2].status == "scratched"
    assert runners[2].status_label == "取消"
    assert runners[2].source == "jra_html"
    assert runners[5].status == "excluded"
    assert runners[5].status_label == "除外"
    quotes = {(quote.bet_type, quote.combination): quote for quote in snapshot.quotes}
    win2 = quotes[("win", "2")]
    assert win2.odds is None
    assert win2.withheld is True
    assert win2.non_runner is True
    assert quotes[("win", "1")].odds == 2.5
    win3 = quotes[("win", "3")]
    assert win3.odds is None
    assert win3.withheld is True
    assert win3.non_runner is False
    assert ("win", "5") not in quotes
    assert quotes[("umaren", "1-2")].odds == 8.5
    assert quotes[("umaren", "1-2")].non_runner is True
    assert quotes[("umaren", "1-2")].withheld is False
    assert quotes[("umaren", "2-3")].odds is None
    assert quotes[("umaren", "2-3")].withheld is True
    assert quotes[("umaren", "2-3")].non_runner is True
    assert ("umaren", "2-4") not in quotes
    assert quotes[("wide", "1-2")].odds_min == 1.5
    assert quotes[("wide", "1-2")].odds_max == 2.5
    assert quotes[("wide", "1-2")].non_runner is True
    assert quotes[("wide", "2-3")].odds_min is None
    assert quotes[("wide", "2-3")].odds_max is None
    assert quotes[("wide", "2-3")].withheld is True
    assert quotes[("sanrenpuku", "1-2-3")].odds == 20.0
    assert quotes[("sanrenpuku", "1-2-3")].non_runner is True
    assert quotes[("sanrenpuku", "1-3-5")].odds == 30.0
    assert quotes[("sanrenpuku", "1-3-5")].non_runner is True
    assert quotes[("sanrenpuku", "2-3-4")].odds is None
    assert quotes[("sanrenpuku", "2-3-4")].withheld is True
    assert quotes[("sanrenpuku", "1-3-4")].odds is None
    assert quotes[("sanrenpuku", "1-3-4")].withheld is True
    assert quotes[("sanrenpuku", "1-3-4")].non_runner is False
    assert ("sanrenpuku", "1-2-4") not in quotes


def test_jra_odds_403_keeps_netkeiba_marks_and_skips_headless():
    pages = {
        "pw01dli00/F3": INDEX_HTML,
        "pw01drl00062026040820260926/27": LIST_HTML,
        "pw01dde0106202604081120260926/2A": SHUTUBA_WITH_ODDS,
    }
    service, _posts, headless_calls = _service_with_jra(pages, odds_error=_http_error(403))
    snapshot = service.get_race_odds(RACE_ID, bet_types="win", prefer="live")

    assert headless_calls == []
    assert snapshot.warning == JRA_ODDS_FORBIDDEN
    runners = {runner.horse_number: runner for runner in snapshot.non_runners or []}
    assert set(runners) == {3}
    assert runners[3].status == "scratched"
    assert runners[3].status_label == "取消"
    assert runners[3].source == "netkeiba_js"
    win2 = next(quote for quote in snapshot.quotes if quote.combination == "2")
    assert win2.odds == 9.9
    assert win2.non_runner is False
    win3 = next(quote for quote in snapshot.quotes if quote.combination == "3")
    assert win3.odds is None
    assert win3.withheld is True
    assert win3.non_runner is True


def test_jra_odds_login_wall_is_not_parsed():
    login = """
    <html><head><title>ログイン</title></head><body>
      <input type="password">
      <table><tr><td class="num">2</td><td class="odds">取消</td></tr></table>
    </body></html>
    """
    pages = {
        "pw01dli00/F3": INDEX_HTML,
        "pw01drl00062026040820260926/27": LIST_HTML,
        "pw01dde0106202604081120260926/2A": SHUTUBA_WITH_ODDS,
        "pw01ode0106202604081120260926/AB": login,
    }
    calls: list[str] = []
    scraper = JRAScraper(headless_render=lambda url: calls.append(url) or ODDS_HTML)
    scraper.http_client.post = _Posts(pages).post  # type: ignore[method-assign]
    page = scraper.odds_scratches(RACE_ID, fetched_at=FETCHED)

    assert calls == []
    assert page.blocked is True
    assert page.runners is None
    assert page.warning == JRA_ODDS_LOGIN
    assert page.withheld == set()


def test_jra_odds_json_page_marks_runners_after_a_shell():
    payload = json.dumps(
        {
            "runners": [
                {"horse_number": 2, "status_label": "取消"},
                {"horse_number": 5, "status": "excluded", "status_label": "除外"},
            ]
        }
    )
    shell = f"""
    <html><body>
      <table><tr><th>馬番</th><th>単勝</th></tr></table>
      <script type="application/json">{payload}</script>
    </body></html>
    """
    pages = {
        "pw01dli00/F3": INDEX_HTML,
        "pw01drl00062026040820260926/27": LIST_HTML,
        "pw01dde0106202604081120260926/2A": SHUTUBA_WITH_ODDS,
        "pw01ode0106202604081120260926/AB": shell,
    }
    scraper = JRAScraper(headless_render="off")
    scraper.http_client.post = _Posts(pages).post  # type: ignore[method-assign]
    page = scraper.odds_scratches(RACE_ID, fetched_at=FETCHED)

    assert page.warning is None
    runners = {runner.horse_number: runner for runner in page.runners or []}
    assert runners[2].status == "scratched"
    assert runners[2].status_label == "取消"
    assert runners[2].source == "jra_js"
    assert runners[2].fetched_at == FETCHED
    assert runners[5].status == "excluded"
    assert runners[5].source == "jra_js"
    assert page.withheld >= {"2", "5"}


def test_cli_prints_labels_and_withheld_prices():
    quote = OddsQuote(
        race_id=RACE_ID,
        bet_type="win",
        combination="2",
        odds=None,
        source="netkeiba_live",
        kind="live",
        fetched_at=FETCHED,
        non_runner=True,
        withheld=True,
    )
    assert _format_odds_quote(quote) == "2番 ---- [netkeiba_live / live] 取消・除外"
    kept = quote.model_copy(update={"odds": 8.5, "withheld": False, "non_runner": True})
    assert "8.5" in _format_odds_quote(kept)
    assert "取消・除外" in _format_odds_quote(kept)


class _CardService:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_race_card(self, race_id: str) -> RaceCard:
        return RaceCard(
            race=Race(
                race_id=race_id,
                race_name="秋風ステークス",
                race_number=11,
                race_date=datetime(2026, 9, 26).date(),
                venue="中山",
                distance=1600,
                track_type="芝",
            ),
            entries=[
                RaceEntry(
                    bracket_number=2,
                    horse_number=2,
                    horse_name="サウンドムーブ",
                    sex="牡",
                    age=4,
                    jockey_name="騎手",
                    status="scratched",
                    status_label="取消",
                    source="jra_html",
                    fetched_at=FETCHED,
                )
            ],
            warning=None,
        )

    def get_race_odds(self, race_id: str, bet_types="win", prefer="live") -> RaceOdds:
        return RaceOdds(
            race_id=race_id,
            bet_types=["win"],
            prefer="live",
            status="live",
            encoding="utf-8",
            race_phase="pre",
            quotes=[
                OddsQuote(
                    race_id=race_id,
                    bet_type="win",
                    combination="2",
                    odds=None,
                    source="netkeiba_live",
                    kind="live",
                    fetched_at=FETCHED,
                    non_runner=True,
                    withheld=True,
                )
            ],
            non_runners=[],
            fetched_at=FETCHED,
        )


def test_cli_card_and_odds_text(monkeypatch, capsys):
    monkeypatch.setattr("backend.cli.RaceService", _CardService)
    cmd_race_card(type("Args", (), {"race_id": RACE_ID, "json": False})())
    card_out = capsys.readouterr().out
    assert "サウンドムーブ" in card_out
    assert "取消" in card_out

    snapshot = _CardService().get_race_odds(RACE_ID)
    snapshot = snapshot.model_copy(
        update={
            "non_runners": [
                NonRunner(
                    horse_number=2,
                    status="scratched",
                    status_label="取消",
                    source="jra_html",
                    fetched_at=FETCHED,
                )
            ]
        }
    )

    class _OddsService(_CardService):
        def get_race_odds(self, race_id: str, bet_types="win", prefer="live") -> RaceOdds:
            return snapshot

    monkeypatch.setattr("backend.cli.RaceService", _OddsService)
    cmd_odds(type("Args", (), {"race_id": RACE_ID, "json": False, "bet_types": "win", "prefer": "live"})())
    odds_out = capsys.readouterr().out
    assert "取消・除外: 2番取消" in odds_out
    assert "2番 ----" in odds_out


@pytest.mark.skipif(
    not headless_browser_installed(),
    reason="headless browser is not installed; no CI workflow installs Playwright",
)
def test_installed_headless_browser_renders_scratch_marks(tmp_path: Path):
    page = tmp_path / "shutuba.html"
    page.write_text(NETKEIBA_MARKED, encoding="utf-8")
    rendered = render_headless(page.as_uri())
    assert "出走取消" in rendered
    assert "競走除外" in rendered
    scraper = NetkeibaScraper(headless_render="off")
    entries = scraper._parse_race_entries(
        BeautifulSoup(rendered, "lxml"),
        source="headless",
        fetched_at=FETCHED,
    )
    horses = {entry.horse_number: entry for entry in entries}
    assert horses[2].status == "scratched"
    assert horses[2].status_label == "出走取消"
    assert horses[2].source == "headless"
    assert horses[5].status == "excluded"
    assert horses[5].status_label == "競走除外"
