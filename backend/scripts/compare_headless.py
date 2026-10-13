#!/usr/bin/env python3
"""Compare JRA HTML fetches with a headless render of the same public pages.

Picks the latest completed meeting still on the official shutuba menu (one
venue, one day, every race). For each race it loads the race card, the
win-odds page linked from that card, and the result page linked from that
card. Track info is the current baba tab for that venue, fetched once.

The HTML path uses the normal HTTP client and the page cache. The headless
path opens Chromium with its own user agent, submits the same form the page
prints (``doAction`` / ``cname``), and reads embedded JSON plus the rendered
DOM. A 403, a login wall, or a captcha stops the run. Nothing is retried to
get around a refusal, and the User-Agent is not replaced.

Usage (from the repo root, after ``pip install -e "backend[headless]"`` and
``playwright install chromium``)::

    python backend/scripts/compare_headless.py
"""

from __future__ import annotations

import argparse
import html as html_lib
import logging
import os
import re
import statistics
import time
import tracemalloc
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TypeAlias
from urllib.parse import unquote, urlencode

from bs4 import BeautifulSoup

from backend.scrapers.final_odds import parse_confirmed_odds
from backend.scrapers.jra_payout import _race_tickets
from backend.scrapers.jra_scraper import (
    _ACCESS_O_ACTION,
    JRAScraper,
    _ParsedRaceCname,
)
from backend.scrapers.jra_track_scraper import JRATrackScraper
from backend.scrapers.page_cache import CachingHTTPClient
from backend.scrapers.page_fallback import (
    AccessBlocked,
    FetchedBody,
    embedded_json_payloads,
    looks_like_login_wall,
)
from backend.scrapers.runner_status import read_price_cell
from backend.scrapers.track_readings import discover_jra_baba_urls
from backend.utils.date_utils import tokyo_now

logger = logging.getLogger(__name__)

Json: TypeAlias = dict[str, "Json"] | list["Json"] | str | int | float | bool | None

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DOC = REPO_ROOT / "docs" / "headless-verification.md"
MIN_INTERVAL = 1.5
PAYOUT_TYPES = ("単勝", "ワイド", "馬連", "3連複")
_RESULT_LABELS = (
    ("出走取消", "scratched"),
    ("競走除外", "excluded"),
    ("競走中止", "did_not_finish"),
    ("中止", "did_not_finish"),
    ("取消", "scratched"),
    ("除外", "excluded"),
    ("失格", "disqualified"),
)


@dataclass
class FetchMeta:
    """Timing and refusal state for one method's documents."""

    seconds: float = 0.0
    rss_kb: int = 0
    tracemalloc_kb: int = 0
    requests: int = 0
    cache_hits: int = 0
    json_payloads: int = 0
    warning: str | None = None
    stopped: bool = False


@dataclass
class RaceFetch:
    """One race, both ways."""

    race_id: str
    race_number: int
    current: Json | None
    headless: Json | None
    current_meta: FetchMeta
    headless_meta: FetchMeta
    mismatches: list[str] = field(default_factory=list)


def _type_name(value: object) -> str:
    return type(value).__name__


def diff_values(left: Json, right: Json, path: str = "$") -> list[str]:
    """List value and type differences. Order of lists is part of the value."""
    if type(left) is not type(right):
        return [f"{path}: type {_type_name(left)} vs {_type_name(right)} ({left!r} vs {right!r})"]
    if isinstance(left, dict) and isinstance(right, dict):
        found: list[str] = []
        for key in sorted(set(left) | set(right)):
            if key not in left:
                found.append(f"{path}.{key}: missing on current")
            elif key not in right:
                found.append(f"{path}.{key}: missing on headless")
            else:
                found.extend(diff_values(left[key], right[key], f"{path}.{key}"))
        return found
    if isinstance(left, list) and isinstance(right, list):
        found = []
        if len(left) != len(right):
            found.append(f"{path}: length {len(left)} vs {len(right)}")
        for index, (item, other) in enumerate(zip(left, right, strict=False)):
            found.extend(diff_values(item, other, f"{path}[{index}]"))
        return found
    if left != right:
        return [f"{path}: {left!r} vs {right!r}"]
    return []


def _rss_tree_kb(root: int | None = None) -> int:
    """VmRSS of this process and its children, in KiB. 0 when /proc is absent."""
    root_pid = root if root is not None else os.getpid()
    rss: dict[int, int] = {}
    children: dict[int, list[int]] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return 0
    for name in names:
        if not name.isdigit():
            continue
        try:
            text = Path(f"/proc/{name}/status").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        pid = int(name)
        ppid = None
        own = 0
        for line in text.splitlines():
            if line.startswith("PPid:"):
                ppid = int(line.split()[1])
            elif line.startswith("VmRSS:"):
                own = int(line.split()[1])
        rss[pid] = own
        if ppid is not None:
            children.setdefault(ppid, []).append(pid)
    total = rss.get(root_pid, 0)
    stack = list(children.get(root_pid, []))
    seen = {root_pid}
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        total += rss.get(pid, 0)
        stack.extend(children.get(pid, []))
    return total


def _denied(status: int | None, body: str) -> str | None:
    """A refusal. Callers stop. They do not change identity and try again."""
    if status == 403:
        return "returned 403"
    if status == 401 or looks_like_login_wall(body):
        return "requires login"
    lowered = body.lower()
    if "captcha" in lowered or "access denied" in lowered:
        return "blocked by captcha"
    return None


def _json_in_html(body: str) -> list[str]:
    """JSON already sitting in the document, not scripts that merely mention braces."""
    return embedded_json_payloads(body)


class HtmlFetch:
    """Current path: HTTP GET/POST through the page cache. Headless is off."""

    def __init__(self, client: CachingHTTPClient):
        self.client = client

    def get(self, url: str, params: dict[str, str] | None = None) -> tuple[str, FetchMeta]:
        return self._read("GET", url, params=params, data=None, encoding="cp932")

    def post(self, url: str, cname: str) -> tuple[str, FetchMeta]:
        return self._read("POST", url, params=None, data={"cname": cname}, encoding="cp932")

    def _read(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None,
        data: dict[str, str] | None,
        encoding: str,
    ) -> tuple[str, FetchMeta]:
        meta = FetchMeta(requests=1)
        meta.cache_hits = (
            1
            if self.client.cached_fetched_at(method, url, params=params, data=data) is not None
            else 0
        )
        tracemalloc.start()
        started = time.perf_counter()
        try:
            if method == "POST":
                body = self.client.post(url, data=data, encoding=encoding)
            else:
                body = self.client.get(url, params=params, encoding=encoding)
        except Exception as exc:
            meta.seconds = time.perf_counter() - started
            meta.tracemalloc_kb = _traced_kb()
            meta.rss_kb = _rss_tree_kb()
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in {401, 403}:
                meta.stopped = True
                meta.warning = "returned 403" if status == 403 else "requires login"
                raise AccessBlocked(status, meta.warning) from exc
            meta.warning = f"fetch failed: {exc.__class__.__name__}"
            raise
        meta.seconds = time.perf_counter() - started
        meta.tracemalloc_kb = _traced_kb()
        meta.rss_kb = _rss_tree_kb()
        meta.json_payloads = len(_json_in_html(body))
        reason = _denied(None, body)
        if reason is not None:
            meta.stopped = True
            meta.warning = reason
            code = 403 if reason == "returned 403" else 401
            raise AccessBlocked(code, reason)
        if "パラメータエラー" in body:
            meta.warning = "JRA rejected the cname"
        return body, meta


def _traced_kb() -> int:
    if not tracemalloc.is_tracing():
        return 0
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return int(peak / 1024)


class HeadlessFetch:
    """One Chromium. Its own user agent. No stealth, no proxy, no retry."""

    def __init__(self, min_interval: float = MIN_INTERVAL):
        self.min_interval = min_interval
        self._last = 0.0
        self._playwright = None
        self._browser = None
        self.available = False
        self.unavailable_reason: str | None = None

    def __enter__(self) -> HeadlessFetch:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.unavailable_reason = "headless browser is not installed"
            return self
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
        except Exception as exc:
            self.unavailable_reason = (
                f"headless browser is not installed ({exc.__class__.__name__})"
            )
            self.close()
            return self
        self.available = True
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None
        self.available = False

    def get(self, url: str) -> tuple[str, FetchMeta]:
        def run(page: object) -> None:
            response = page.goto(url, wait_until="domcontentloaded", timeout=30_000)  # type: ignore[attr-defined]
            page._last_response = response  # type: ignore[attr-defined]

        return self._run(run)

    def post(self, url: str, cname: str) -> tuple[str, FetchMeta]:
        """Submit the public ``cname`` form. This is the page's own doAction."""

        def run(page: object) -> None:
            page.goto("about:blank")  # type: ignore[attr-defined]
            safe = html_lib.escape(cname, quote=True)
            page.set_content(  # type: ignore[attr-defined]
                f"<form id='f' method='POST' action='{html_lib.escape(url, quote=True)}'>"
                f"<input name='cname' value='{safe}'></form>"
            )
            page.locator("#f").wait_for(state="attached")  # type: ignore[attr-defined]
            # set_content is itself a navigation. Wait for the access URL or
            # the listener can resolve on the form document.
            with page.expect_navigation(  # type: ignore[attr-defined]
                wait_until="domcontentloaded",
                timeout=30_000,
                url=lambda target: "jra.go.jp/JRADB/access" in target,
            ) as navigation:
                page.locator("#f").evaluate("form => form.submit()")  # type: ignore[attr-defined]
            page._last_response = navigation.value  # type: ignore[attr-defined]

        return self._run(run)

    def _pause(self) -> None:
        elapsed = time.perf_counter() - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

    def _run(self, navigate: Callable[[object], None]) -> tuple[str, FetchMeta]:
        if not self.available or self._browser is None:
            meta = FetchMeta(warning=self.unavailable_reason or "headless browser is not installed")
            raise RuntimeError(meta.warning)
        self._pause()
        meta = FetchMeta(requests=1)
        tracemalloc.start()
        started = time.perf_counter()
        page = self._browser.new_page()
        try:
            navigate(page)
            response = getattr(page, "_last_response", None)
            status = response.status if response is not None else None
            body = str(page.content())
            payloads = page.evaluate(
                """() => {
                    const out = [];
                    for (const script of document.scripts) {
                        const type = (script.getAttribute('type') || '').toLowerCase();
                        const text = (script.textContent || '').trim();
                        if (!text) continue;
                        if (type.includes('json') || text.startsWith('{') || text.startsWith('[')) {
                            out.push(text);
                        }
                    }
                    return out;
                }"""
            )
            meta.json_payloads = len(payloads) if isinstance(payloads, list) else 0
        except AccessBlocked:
            raise
        except Exception as exc:
            meta.seconds = time.perf_counter() - started
            self._last = time.perf_counter()
            meta.tracemalloc_kb = _traced_kb()
            meta.rss_kb = _rss_tree_kb()
            message = str(exc).lower()
            if "403" in message or "401" in message:
                meta.stopped = True
                meta.warning = "returned 403" if "403" in message else "requires login"
                raise AccessBlocked(403 if "403" in message else 401, meta.warning) from exc
            meta.warning = f"headless render failed: {exc.__class__.__name__}"
            raise
        finally:
            page.close()
        meta.seconds = time.perf_counter() - started
        self._last = time.perf_counter()
        meta.tracemalloc_kb = _traced_kb()
        meta.rss_kb = _rss_tree_kb()
        reason = _denied(status, body)
        if reason is not None:
            meta.stopped = True
            meta.warning = reason
            code = 403 if reason == "returned 403" else 401
            raise AccessBlocked(code, reason)
        if "パラメータエラー" in body:
            meta.warning = "JRA rejected the cname"
        return body, meta


def _add(meta: FetchMeta, extra: FetchMeta) -> None:
    meta.seconds += extra.seconds
    meta.requests += extra.requests
    meta.cache_hits += extra.cache_hits
    meta.json_payloads += extra.json_payloads
    meta.rss_kb = max(meta.rss_kb, extra.rss_kb)
    meta.tracemalloc_kb = max(meta.tracemalloc_kb, extra.tracemalloc_kb)
    if extra.warning and not meta.warning:
        meta.warning = extra.warning


def race_id_for(race: _ParsedRaceCname) -> str:
    return f"{race.year}{race.venue_code}{race.kai:02d}{race.day_number:02d}{race.race_number:02d}"


def _race_key(race: _ParsedRaceCname) -> str:
    ymd = race.race_date.strftime("%Y%m%d")
    return (
        f"{race.venue_code}{race.year}{race.kai:02d}{race.day_number:02d}"
        f"{race.race_number:02d}{ymd}"
    )


def win_odds_cname(card_html: str, race: _ParsedRaceCname) -> str | None:
    """Win/place odds cname printed on this card.

    The first ``doAction`` on a live card is the odds menu (``pw15oli00``),
    not this race. The race button contains the meeting digits and ``151ou``.
    The checksum is copied from that button.
    """
    key = f"{race.race_number:02d}{race.race_date.strftime('%Y%m%d')}"
    found: list[str] = []
    for match in _ACCESS_O_ACTION.finditer(card_html):
        cname = unquote(match.group(2)).strip()
        if key in cname and "151ou" in cname:
            found.append(cname)
    unique = list(dict.fromkeys(found))
    if len(unique) == 1:
        return unique[0]
    finals = [cname for cname in unique if "Z/" in cname]
    if len(finals) == 1:
        return finals[0]
    return None


def result_cname(card_html: str, race: _ParsedRaceCname) -> str | None:
    """Result cname printed as an accessS link for this race. Checksum copied.

    Live links look like ``pw01sde10`` plus the venue, year, kai, day, race,
    and date. The ``10`` generation byte is not part of ``_race_key``.
    """
    key = _race_key(race)
    found: list[str] = []
    for match in re.finditer(r"accessS\.html\?CNAME=([^\"'\s>]+)", card_html, re.IGNORECASE):
        cname = unquote(match.group(1)).strip()
        if "pw01sde" in cname and key in cname:
            found.append(cname)
    unique = list(dict.fromkeys(found))
    if len(unique) == 1:
        return unique[0]
    return None


def snapshot(
    card_html: str, odds_html: str | None, result_html: str | None, race: _ParsedRaceCname
) -> Json:
    """Fields both methods must agree on. Source and fetched_at are not compared."""
    race_id = race_id_for(race)
    scraper = JRAScraper(headless_render="off")
    fetched = tokyo_now()
    card = scraper._parse_race_card(card_html, race_id, race, source="jra_html", fetched_at=fetched)
    entries: list[Json] = []
    if card is not None:
        for entry in sorted(card.entries, key=lambda item: item.horse_number):
            entries.append(
                {
                    "horse_number": entry.horse_number,
                    "horse_number_type": _type_name(entry.horse_number),
                    "horse_name": entry.horse_name,
                    "status": entry.status,
                    "status_type": _type_name(entry.status),
                    "status_label": entry.status_label,
                    "status_label_type": _type_name(entry.status_label),
                    "odds": entry.odds,
                    "odds_type": _type_name(entry.odds),
                }
            )
    scratches = [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("status") in {"scratched", "excluded"}
    ]
    return {
        "race_name": None if card is None else card.race.race_name,
        "race_number": race.race_number,
        "race_number_type": "int",
        "distance": None if card is None else card.race.distance,
        "distance_type": "NoneType" if card is None else _type_name(card.race.distance),
        "track_type": None if card is None else card.race.track_type,
        "post_time": None if card is None else card.race.post_time,
        "entries": entries,
        "scratches": scratches,
        "confirmed_win": _confirmed_win(odds_html, race_id, fetched),
        "result": _result_rows(result_html),
        "payouts": _payouts(result_html, race.race_number),
    }


def _confirmed_win(odds_html: str | None, race_id: str, fetched: datetime) -> Json:
    """Production parser output, plus the printed 最終オッズ win column.

    ``parse_confirmed_odds`` keeps a price only when the page says 確定オッズ.
    The official odds button for a finished race is labelled 最終オッズ. Both
    are recorded so a label gap is visible and the printed prices can still
    be compared.
    """
    if not odds_html or "パラメータエラー" in odds_html:
        return {
            "parser_confirmed": False,
            "parser_win": [],
            "printed_final_label": False,
            "printed_win": [],
        }
    from backend.scrapers.page_fallback import FetchedBody

    parsed = parse_confirmed_odds(
        [FetchedBody(odds_html, "jra_html", fetched, "https://www.jra.go.jp/JRADB/accessO.html")],
        race_id,
    )
    parser_win = []
    for quote in parsed.quotes:
        if quote.bet_type != "win":
            continue
        parser_win.append(
            {
                "combination": quote.combination,
                "odds": quote.odds,
                "odds_type": _type_name(quote.odds),
                "withheld": quote.withheld,
                "withheld_type": _type_name(quote.withheld),
                "non_runner": quote.non_runner,
                "non_runner_type": _type_name(quote.non_runner),
            }
        )
    text = re.sub(r"\s+", "", odds_html)
    printed: list[Json] = []
    soup = BeautifulSoup(odds_html, "lxml")
    table = soup.select_one("table.tanpuku")
    if table is not None:
        for row in table.find_all("tr"):
            number = row.select_one("td.num")
            price = row.select_one("td.odds_tan")
            if number is None or price is None:
                continue
            cell = read_price_cell(price.get_text(" ", strip=True))
            odds = None if cell is None else cell.odds
            printed.append(
                {
                    "horse_number": number.get_text(strip=True),
                    "horse_number_type": "str",
                    "odds": odds,
                    "odds_type": _type_name(odds),
                    "withheld": False if cell is None else cell.withheld,
                    "withheld_type": "bool",
                    "non_runner": False if cell is None else cell.non_runner,
                    "non_runner_type": "bool",
                    "status": None if cell is None else cell.status,
                    "status_type": "NoneType" if cell is None or cell.status is None else "str",
                }
            )
    return {
        "parser_confirmed": parsed.confirmed,
        "parser_confirmed_type": "bool",
        "parser_win": parser_win,
        "printed_final_label": "最終オッズ" in text,
        "printed_final_label_type": "bool",
        "printed_win": printed,
    }


def _result_rows(result_html: str | None) -> list[Json]:
    if not result_html or "パラメータエラー" in result_html:
        return []
    soup = BeautifulSoup(result_html, "lxml")
    table = None
    for candidate in soup.find_all("table"):
        header = candidate.get_text(" ", strip=True)
        if "着順" in header and "馬名" in header:
            table = candidate
            break
    if table is None:
        return []
    rows: list[Json] = []
    for tr in table.find_all("tr"):
        place = tr.select_one("td.place")
        number = tr.select_one("td.num")
        if place is None or number is None:
            continue
        digits = number.get_text(strip=True)
        if not digits.isdigit():
            continue
        place_text = re.sub(r"\s+", "", place.get_text(" ", strip=True))
        demoted = "降着" in place_text
        core = place_text.replace("降着", "")
        rank: int | None = None
        status: str | None = None
        label: str | None = None
        if core.isdigit():
            rank = int(core)
            status = "finished"
        else:
            for printed, code in _RESULT_LABELS:
                if core == printed:
                    status = code
                    label = printed
                    break
            if status is None and core:
                status = "unknown"
                label = core
        rows.append(
            {
                "horse_number": int(digits),
                "horse_number_type": "int",
                "rank": rank,
                "rank_type": _type_name(rank),
                "status": status,
                "status_type": _type_name(status),
                "status_label": label,
                "status_label_type": _type_name(label),
                "demoted": demoted,
                "demoted_type": "bool",
            }
        )
    return rows


def _payouts(result_html: str | None, race_number: int) -> list[Json]:
    if not result_html or "パラメータエラー" in result_html:
        return []
    tickets = _race_tickets(result_html, race_number) or []
    rows: list[Json] = []
    for ticket in tickets:
        if ticket.bet_type not in PAYOUT_TYPES:
            continue
        rows.append(
            {
                "bet_type": ticket.bet_type,
                "combination": ticket.combination,
                "payout": ticket.payout,
                "payout_type": _type_name(ticket.payout),
                "popularity": ticket.popularity,
                "popularity_type": _type_name(ticket.popularity),
            }
        )
    return rows


def track_view(info: object) -> Json:
    """Cushion, moisture, going, and weather. Source clocks are not compared."""
    from backend.models.track import TrackInfo

    if not isinstance(info, TrackInfo):
        raise TypeError("track info was not TrackInfo")

    def going(course: object) -> Json:
        if course is None:
            return None
        condition = getattr(course, "condition", None)
        value = getattr(condition, "value", None)
        text = value if isinstance(value, str) else (None if condition is None else str(condition))
        return {"condition": text, "condition_type": _type_name(text)}

    def moisture(course: object) -> Json:
        if course is None or getattr(course, "moisture", None) is None:
            return None
        reading = course.moisture
        return {
            "goal_front": reading.goal_front,
            "goal_front_type": _type_name(reading.goal_front),
            "fourth_corner": reading.fourth_corner,
            "fourth_corner_type": _type_name(reading.fourth_corner),
        }

    def cushion(course: object) -> Json:
        if course is None or getattr(course, "cushion", None) is None:
            return None
        reading = course.cushion
        return {
            "value": reading.value,
            "value_type": _type_name(reading.value),
            "grade": reading.grade,
            "grade_type": _type_name(reading.grade),
        }

    weather = info.weather.value if hasattr(info.weather, "value") else str(info.weather)
    date = info.date.date().isoformat() if info.date is not None else None
    return {
        "racecourse": info.racecourse,
        "date": date,
        "date_type": _type_name(date),
        "weather": weather,
        "weather_type": "str",
        "warning": info.warning,
        "turf_going": going(info.turf),
        "dirt_going": going(info.dirt),
        "cushion": cushion(info.turf),
        "turf_moisture": moisture(info.turf),
        "dirt_moisture": moisture(info.dirt),
    }


def discover_meeting(
    html_fetch: HtmlFetch, scraper: JRAScraper
) -> tuple[str, list[_ParsedRaceCname]]:
    """Latest day on the shutuba menu. Kyoto wins a tie on the same day."""
    index, _meta = html_fetch.post(JRAScraper.ACCESS_D_URL, JRAScraper.SHUTUBA_INDEX_CNAME)
    _races, meetings = scraper._scan_access_d(index)
    if not meetings:
        raise RuntimeError("shutuba menu listed no meetings")
    latest = max(meeting.race_date for meeting in meetings)
    same_day = [meeting for meeting in meetings if meeting.race_date == latest]
    kyoto = [meeting for meeting in same_day if meeting.venue_code == "08"]
    chosen = kyoto[0] if kyoto else sorted(same_day, key=lambda item: item.venue_code)[0]
    page, _meta = html_fetch.post(JRAScraper.ACCESS_D_URL, chosen.cname)
    races, _meetings = scraper._scan_access_d(page)
    day = [
        race
        for race in races
        if race.race_date == chosen.race_date and race.venue_code == chosen.venue_code
    ]
    day.sort(key=lambda race: race.race_number)
    if not day:
        raise RuntimeError("meeting page listed no races")
    venue = scraper._venue_name(chosen.venue_code)
    label = f"{chosen.race_date.isoformat()} {venue} {chosen.kai}回{chosen.day_number}日"
    return label, day


def fetch_race_html(html_fetch: HtmlFetch, race: _ParsedRaceCname) -> tuple[Json | None, FetchMeta]:
    meta = FetchMeta()
    card, card_meta = html_fetch.post(JRAScraper.ACCESS_D_URL, race.cname)
    _add(meta, card_meta)
    odds_name = win_odds_cname(card, race)
    result_name = result_cname(card, race)
    odds_html = None
    result_html = None
    if odds_name is None:
        meta.warning = "odds cname was not on the card"
    else:
        odds_html, odds_meta = html_fetch.post(JRAScraper.ACCESS_O_URL, odds_name)
        _add(meta, odds_meta)
    if result_name is None:
        meta.warning = meta.warning or "result cname was not on the card"
    else:
        url = "https://www.jra.go.jp/JRADB/accessS.html"
        result_html, result_meta = html_fetch.get(url, {"CNAME": result_name})
        _add(meta, result_meta)
    return snapshot(card, odds_html, result_html, race), meta


def fetch_race_headless(
    headless: HeadlessFetch, race: _ParsedRaceCname
) -> tuple[Json | None, FetchMeta]:
    meta = FetchMeta()
    card, card_meta = headless.post(JRAScraper.ACCESS_D_URL, race.cname)
    _add(meta, card_meta)
    odds_name = win_odds_cname(card, race)
    result_name = result_cname(card, race)
    odds_html = None
    result_html = None
    if odds_name is None:
        meta.warning = "odds cname was not on the card"
    else:
        odds_html, odds_meta = headless.post(JRAScraper.ACCESS_O_URL, odds_name)
        _add(meta, odds_meta)
    if result_name is None:
        meta.warning = meta.warning or "result cname was not on the card"
    else:
        url = "https://www.jra.go.jp/JRADB/accessS.html?" + urlencode({"CNAME": result_name})
        result_html, result_meta = headless.get(url)
        _add(meta, result_meta)
    return snapshot(card, odds_html, result_html, race), meta


def fetch_track_html(client: CachingHTTPClient) -> tuple[Json, FetchMeta]:
    meta = FetchMeta()
    scraper = JRATrackScraper(client, headless_render="off")
    tracemalloc.start()
    started = time.perf_counter()
    info = scraper.scrape("京都")
    meta.seconds = time.perf_counter() - started
    meta.tracemalloc_kb = _traced_kb()
    meta.rss_kb = _rss_tree_kb()
    meta.requests = 2
    meta.warning = info.warning
    return track_view(info), meta


def fetch_track_headless(
    headless: HeadlessFetch, client: CachingHTTPClient
) -> tuple[Json, FetchMeta]:
    """Render the baba tab, then the cushion fragment that tab's script names.

    The number is not in the tab HTML. The page's own script points at
    ``_data_cushion.html``. That URL is opened in the browser. The script
    file itself is read through the HTTP cache so the browser is not asked
    to wrap JavaScript in a document.
    """
    meta = FetchMeta()
    scraper = JRATrackScraper(headless_render="off")
    pages: list[tuple[str, str]] = []
    for url in JRATrackScraper.BABA_INDEX_URLS:
        body, page_meta = headless.get(url)
        _add(meta, page_meta)
        pages.append((url, body))
    chosen = None
    for url, body in pages:
        if scraper._title_venue(body) == "京都":
            chosen = (url, body)
            break
    if chosen is None:
        warnings = [body for _url, body in pages]
        return {
            "racecourse": None,
            "warning": scraper._miss_warning(warnings, False, not pages),
        }, meta
    url, body = chosen
    fetched = tokyo_now()
    documents = [FetchedBody(body, "headless", fetched, url)]
    if scraper._cushion_slot_empty(body):
        script_meta = FetchMeta()

        def fetch_script(script_url: str) -> str:
            text, extra = HtmlFetch(client).get(script_url)
            _add(script_meta, extra)
            return text

        fragments = discover_jra_baba_urls(body, url, fetch_script)
        _add(meta, script_meta)
        for fragment in fragments:
            fragment_body, fragment_meta = headless.get(fragment)
            _add(meta, fragment_meta)
            documents.append(FetchedBody(fragment_body, "headless", fetched, fragment))
    info = scraper._from_pages(documents, "京都")
    meta.warning = info.warning
    return track_view(info), meta


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def _fmt(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}"


def render_doc(
    *,
    label: str,
    races: list[RaceFetch],
    track_current: Json | None,
    track_headless: Json | None,
    track_current_meta: FetchMeta | None,
    track_headless_meta: FetchMeta | None,
    track_mismatches: list[str],
    stopped: str | None,
) -> str:
    current_times = [race.current_meta.seconds for race in races]
    headless_times = [race.headless_meta.seconds for race in races]
    live_current = [
        race.current_meta.seconds
        for race in races
        if race.current_meta.cache_hits < race.current_meta.requests
    ]
    mismatch_count = sum(len(race.mismatches) for race in races) + len(track_mismatches)
    lines = [
        "# Headless verification",
        "",
        f"Meeting: {label}.",
        "",
        "HTML is the current client (POST for accessD and accessO, GET for accessS),",
        "with the page cache. Headless is one Chromium process, the browser's own",
        "user agent, and the same cname the card prints. Embedded JSON scripts were",
        "counted. Race fields were read from the rendered DOM because those scripts",
        "did not carry the card, odds, result, or payouts. Delays are at least 1.5s",
        "between headless navigations. A cache hit does not wait. 403, login, and",
        "captcha stop the run.",
        "",
        "The production odds parser accepts a price only when the page says 確定オッズ.",
        "Finished JRA odds pages say 最終オッズ. `parser_confirmed` is that gate.",
        "`printed_win` is the 単勝 column on that page, parsed with `read_price_cell`",
        "(float, or null when the cell is 取消 or ----). Both sides use the same",
        "parser, so a label gap shows up as confirmed=false on both, not as a",
        "made-up price.",
        "",
        "The first `doAction` for オッズ on a live card is the odds menu. This",
        "comparison follows the race button whose cname contains that race's date",
        "and `151ou`. The checksum is the one printed on the button.",
        "",
        "## Timing",
        "",
        f"- HTML per race: median {_fmt(_median(current_times))}s, max {_fmt(max(current_times) if current_times else None)}s.",
        f"- Headless per race: median {_fmt(_median(headless_times))}s, max {_fmt(max(headless_times) if headless_times else None)}s.",
        "- HTML races that were not fully cached: "
        f"median {_fmt(_median(live_current))}, max {_fmt(max(live_current) if live_current else None)} "
        f"(n={len(live_current)}).",
        "",
        "Per-race time is wall clock for the card, the odds page, and the result",
        "page, including the polite wait on a cache miss or a headless navigation.",
        "Track is once per method and is not added to each race.",
        "",
        "## Mismatches",
        "",
        f"Mismatch count: {mismatch_count}.",
        "",
        "| race_id | current seconds | headless seconds | mismatches |",
        "|---|---:|---:|---:|",
    ]
    for race in races:
        lines.append(
            f"| {race.race_id} | {race.current_meta.seconds:.2f} | "
            f"{race.headless_meta.seconds:.2f} | {len(race.mismatches)} |"
        )
    if track_current_meta is not None and track_headless_meta is not None:
        lines.append(
            f"| track | {track_current_meta.seconds:.2f} | "
            f"{track_headless_meta.seconds:.2f} | {len(track_mismatches)} |"
        )
    lines.extend(["", "## Memory", ""])
    lines.append(
        "| race_id | current RSS KiB | headless RSS KiB | current traced KiB | headless traced KiB |"
    )
    lines.append("|---|---:|---:|---:|---:|")
    for race in races:
        lines.append(
            f"| {race.race_id} | {race.current_meta.rss_kb} | {race.headless_meta.rss_kb} | "
            f"{race.current_meta.tracemalloc_kb} | {race.headless_meta.tracemalloc_kb} |"
        )
    if track_current_meta is not None and track_headless_meta is not None:
        lines.append(
            f"| track | {track_current_meta.rss_kb} | {track_headless_meta.rss_kb} | "
            f"{track_current_meta.tracemalloc_kb} | {track_headless_meta.tracemalloc_kb} |"
        )
    lines.extend(["", "## JSON payloads", ""])
    total_json = sum(
        race.current_meta.json_payloads + race.headless_meta.json_payloads for race in races
    )
    lines.append(
        f"Embedded JSON payload count across both methods: {total_json}. "
        "A payload is a script whose type contains json, or whose body is a JSON array or object."
    )
    lines.extend(["", "## Notes", ""])
    for race in races:
        notes = []
        if race.current_meta.cache_hits:
            notes.append(
                f"HTML cache hits {race.current_meta.cache_hits}/{race.current_meta.requests}"
            )
        if race.current_meta.warning:
            notes.append(f"HTML warning: {race.current_meta.warning}")
        if race.headless_meta.warning:
            notes.append(f"headless warning: {race.headless_meta.warning}")
        if race.mismatches:
            shown = "; ".join(race.mismatches[:8])
            extra = len(race.mismatches) - 8
            if extra > 0:
                shown = f"{shown}; … {extra} more"
            notes.append(shown)
        if notes:
            lines.append(f"- {race.race_id}: {'. '.join(notes)}")
    if track_mismatches:
        lines.append(f"- track: {'; '.join(track_mismatches[:12])}")
    if track_current_meta is not None and track_current_meta.warning:
        lines.append(f"- track HTML warning: {track_current_meta.warning}")
    if track_headless_meta is not None and track_headless_meta.warning:
        lines.append(f"- track headless warning: {track_headless_meta.warning}")
    if stopped:
        lines.append(f"- Stopped: {stopped}")
    lines.append("")
    return "\n".join(lines)


def run(limit: int | None, doc_path: Path) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = CachingHTTPClient(min_request_interval=MIN_INTERVAL)
    scraper = JRAScraper(client, headless_render="off")
    html_fetch = HtmlFetch(client)
    label, races = discover_meeting(html_fetch, scraper)
    if limit is not None:
        races = races[:limit]
    logger.info("Meeting %s, %s races", label, len(races))
    rows: list[RaceFetch] = []
    stopped: str | None = None
    with HeadlessFetch() as headless:
        if not headless.available:
            stopped = headless.unavailable_reason
            logger.warning("%s", stopped)
        for race in races:
            race_id = race_id_for(race)
            logger.info("Race %s", race_id)
            try:
                current, current_meta = fetch_race_html(html_fetch, race)
            except AccessBlocked as blocked:
                stopped = f"HTML {race_id}: {blocked}"
                rows.append(
                    RaceFetch(
                        race_id,
                        race.race_number,
                        None,
                        None,
                        FetchMeta(stopped=True, warning=str(blocked)),
                        FetchMeta(),
                    )
                )
                break
            headless_snapshot = None
            headless_meta = FetchMeta(warning=headless.unavailable_reason)
            if headless.available and stopped is None:
                try:
                    headless_snapshot, headless_meta = fetch_race_headless(headless, race)
                except AccessBlocked as blocked:
                    stopped = f"headless {race_id}: {blocked}"
                    headless_meta = FetchMeta(stopped=True, warning=str(blocked))
            mismatches: list[str] = []
            if current is not None and headless_snapshot is not None:
                mismatches = diff_values(current, headless_snapshot)
            elif headless_snapshot is None and headless.available:
                mismatches = ["headless snapshot missing"]
            rows.append(
                RaceFetch(
                    race_id,
                    race.race_number,
                    current,
                    headless_snapshot,
                    current_meta,
                    headless_meta,
                    mismatches,
                )
            )
            logger.info(
                "%s html %.2fs headless %.2fs mismatches %s",
                race_id,
                current_meta.seconds,
                headless_meta.seconds,
                len(mismatches),
            )
            if stopped:
                break
        track_current = None
        track_headless = None
        track_current_meta = None
        track_headless_meta = None
        track_mismatches: list[str] = []
        if stopped is None:
            track_current, track_current_meta = fetch_track_html(client)
            if headless.available:
                try:
                    track_headless, track_headless_meta = fetch_track_headless(headless, client)
                except AccessBlocked as blocked:
                    stopped = f"headless track: {blocked}"
                    track_headless_meta = FetchMeta(stopped=True, warning=str(blocked))
            if track_current is not None and track_headless is not None:
                track_mismatches = diff_values(track_current, track_headless)
    text = render_doc(
        label=label,
        races=rows,
        track_current=track_current,
        track_headless=track_headless,
        track_current_meta=track_current_meta,
        track_headless_meta=track_headless_meta,
        track_mismatches=track_mismatches,
        stopped=stopped,
    )
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text(text, encoding="utf-8")
    logger.info("Wrote %s", doc_path)
    return 2 if stopped else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare JRA HTML and headless fetches")
    parser.add_argument("--limit", type=int, default=None, help="Only the first N races")
    parser.add_argument("--output", type=Path, default=DEFAULT_DOC)
    args = parser.parse_args()
    raise SystemExit(run(args.limit, args.output))


if __name__ == "__main__":
    main()
