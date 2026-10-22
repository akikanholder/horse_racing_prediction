"""Cross-check netkeiba payouts against the public JRA payout list.

The menu cname is the one printed on the JRA keiba page (``doAction`` for
払戻金). Checksums are copied from that page. A 403 or a login wall stops the
walk. Missing official figures are a warning, not a filled-in price.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import NamedTuple

from bs4 import BeautifulSoup, Tag

from backend.scrapers.jra_headless import HEADLESS_RENDER_FAILED, shared_session
from backend.scrapers.page_fallback import (
    HEADLESS_UNAVAILABLE,
    AccessBlocked,
    HeadlessUnavailableError,
)

logger = logging.getLogger(__name__)

JRA_PAYOUT_UNREACHABLE = "JRA official payout was not reachable"
JRA_PAYOUT_FORBIDDEN = "JRA payout returned 403"
JRA_PAYOUT_MISMATCH = "JRA payout mismatch"
JRA_REFUND_MISMATCH = "JRA refund mismatch"
JRA_POPULARITY_NOTE = "JRA popularity"

ACCESS_H_URL = "https://www.jra.go.jp/JRADB/accessH.html"
PAYOUT_INDEX_CNAME = "pw01hli00/03"
ACCESS_H_ENCODING = "cp932"

_MEETING_CNAME = re.compile(
    r"pw01hde\d{2}(?P<venue>\d{2})(?P<year>\d{4})(?P<kai>\d{2})"
    r"(?P<day>\d{2})(?P<ymd>\d{8})/[0-9A-Fa-f]{2}"
)
_YEN = re.compile(r"(\d[\d,]*)円")
_POP = re.compile(r"(\d+)番?人気")


@dataclass(frozen=True)
class OfficialPayoutKey:
    """Fields the result page already knows, without reading them out of the race id."""

    venue_code: str
    year: int
    kai: int
    meeting_day: int
    race_date: date
    race_number: int


@dataclass(frozen=True)
class _Ticket:
    bet_type: str
    combination: str
    payout: int
    popularity: int | None


@dataclass(frozen=True)
class OfficialRefund:
    """返還 block on one race of the JRA payout page. Prices are not included."""

    horse_numbers: tuple[int, ...]
    bet_types: tuple[str, ...] | None
    bracket_numbers: tuple[int, ...] | None


class PopularityNote(NamedTuple):
    """One ticket whose 人気 is taken from JRA. The yen amount stays netkeiba's."""

    bet_type: str
    combination: str
    payout: int
    netkeiba: int | None
    official: int


class PayoutCheck(NamedTuple):
    """Official comparison. ``http_status`` is set only when the payout fetch was refused.

    ``phase`` is set when this payout page shows a 確定 / 確定前 label or a
    payout table for the race. ``popularity`` lists tickets whose 人気 is
    replaced by JRA. A popularity-only difference is not ``warning``.
    """

    warning: str | None
    refunds: list[OfficialRefund] | None
    source: str
    http_status: int | None = None
    login_required: bool = False
    phase: str | None = None
    popularity: tuple[PopularityNote, ...] = ()


def cross_check_payouts(
    http_client: object,
    key: OfficialPayoutKey | None,
    payouts: list[tuple[str, str, int, int | None]],
    *,
    fetch_mode: str = "html",
    session: object | None = None,
) -> PayoutCheck:
    """Return a payout warning, any 返還 block, and the JRA source token.

    ``payouts`` is the netkeiba list. This function does not invent prices.
    Several rows of one bet type are kept. A missing popularity on one side,
    and a popularity that differs while the combination and yen match, are not
    mismatches. JRA's popularity is returned for the caller to record. ``None``
    refunds means the page printed no 返還 block. The source is ``headless``
    only when the browser returned the documents. A 403 is not fetched again
    through HTTP. ``http_status`` is that refusal, not a yen amount that
    happens to contain 403. ``phase`` is confirmed when this page has the
    race's payout table, or the short 確定 / 確定前 label in that race block.
    """
    if key is None:
        return PayoutCheck(JRA_PAYOUT_UNREACHABLE, None, "jra_html")
    post = getattr(http_client, "post", None)
    if not callable(post):
        return PayoutCheck(JRA_PAYOUT_UNREACHABLE, None, "jra_html")
    poster, source_of, extra = _payout_poster(post, fetch_mode, session)
    try:
        index = _post(poster, PAYOUT_INDEX_CNAME)
    except Exception as exc:
        logger.warning("JRA payout index was not reachable: %s", exc)
        return _failure_check(exc, extra(), source_of())
    cname = _meeting_cname(index, key)
    if cname is None:
        return PayoutCheck(_join_note(JRA_PAYOUT_UNREACHABLE, extra()), None, source_of())
    try:
        day_html = _post(poster, cname)
    except Exception as exc:
        logger.warning("JRA payout day page was not reachable: %s", exc)
        return _failure_check(exc, extra(), source_of())
    refunds = _race_refunds(day_html, key.race_number)
    phase = _official_phase(day_html, key.race_number)
    if not payouts:
        return PayoutCheck(extra(), refunds, source_of(), phase=phase)
    official = _race_tickets(day_html, key.race_number)
    if official is None:
        return PayoutCheck(
            _join_note(JRA_PAYOUT_UNREACHABLE, extra()),
            refunds,
            source_of(),
            phase=phase,
        )
    warning, notes = _compare(payouts, official)
    return PayoutCheck(
        _join_note(warning, extra()),
        refunds,
        source_of(),
        phase=phase,
        popularity=notes,
    )


def _join_note(warning: str | None, extra: str | None) -> str | None:
    if extra is None:
        return warning
    if warning is None:
        return extra
    if extra in warning:
        return warning
    return f"{warning}; {extra}"


def _payout_poster(
    http_post: object,
    fetch_mode: str,
    session: object | None,
) -> tuple[object, object, object]:
    """Return a poster, a source callable, and a fallback-warning callable."""
    used: list[str] = []
    notes: list[str] = []

    def source_of() -> str:
        if used and all(item == "headless" for item in used):
            return "headless"
        return "jra_html"

    def extra() -> str | None:
        if not notes:
            return None
        return notes[0]

    def poster(url: str, data: dict[str, str] | None = None, encoding: str | None = None) -> str:
        if fetch_mode not in {"headless", "auto"}:
            used.append("html")
            return http_post(url, data=data, encoding=encoding)  # type: ignore[operator]
        browser = session
        if browser is None:
            try:
                browser = shared_session()
            except HeadlessUnavailableError:
                browser = None
        if browser is not None:
            try:
                body = browser.post(url, data=data, encoding=encoding)  # type: ignore[attr-defined]
            except AccessBlocked:
                raise
            except HeadlessUnavailableError:
                if fetch_mode == "headless" and HEADLESS_UNAVAILABLE not in notes:
                    notes.append(HEADLESS_UNAVAILABLE)
            except Exception as exc:
                logger.warning("Headless JRA payout fetch failed: %s", exc)
                if fetch_mode == "headless" and HEADLESS_RENDER_FAILED not in notes:
                    notes.append(HEADLESS_RENDER_FAILED)
            else:
                if not isinstance(body, str):
                    raise TypeError("Headless JRA payout response was not text")
                used.append("headless")
                return body
        used.append("html")
        return http_post(url, data=data, encoding=encoding)  # type: ignore[operator]

    return poster, source_of, extra


def _failure_warning(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(exc, "status", None)
    if status in {401, 403}:
        return JRA_PAYOUT_FORBIDDEN
    return JRA_PAYOUT_UNREACHABLE


def _failure_check(exc: BaseException, extra: str | None, source: str) -> PayoutCheck:
    """A refused payout fetch carries the status. Other failures stay unreachable."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(exc, "status", None)
    warning = _join_note(_failure_warning(exc), extra)
    if status == 403:
        return PayoutCheck(warning, None, source, 403, False)
    if status == 401:
        return PayoutCheck(warning, None, source, 401, True)
    return PayoutCheck(warning, None, source)


def _post(post: object, cname: str) -> str:
    html = post(  # type: ignore[operator]
        ACCESS_H_URL,
        data={"cname": cname},
        encoding=ACCESS_H_ENCODING,
    )
    if not isinstance(html, str):
        raise TypeError("JRA payout response was not text")
    if "パラメータエラー" in html:
        raise RuntimeError("JRA payout cname was rejected")
    return html


def _meeting_cname(html: str, key: OfficialPayoutKey) -> str | None:
    ymd = key.race_date.strftime("%Y%m%d")
    found: list[str] = []
    for match in _MEETING_CNAME.finditer(html):
        if (
            match.group("venue") == key.venue_code
            and int(match.group("year")) == key.year
            and int(match.group("kai")) == key.kai
            and int(match.group("day")) == key.meeting_day
            and match.group("ymd") == ymd
        ):
            found.append(match.group(0))
    unique = list(dict.fromkeys(found))
    if len(unique) != 1:
        return None
    return unique[0]


def _race_tickets(html: str, race_number: int) -> list[_Ticket] | None:
    block = _race_block(html, race_number)
    if block is None:
        return None
    soup = BeautifulSoup(block, "lxml")
    unit: Tag | None = None
    for candidate in soup.select(".refund_unit"):
        if not isinstance(candidate, Tag):
            continue
        classes = candidate.get("class") or []
        if "restoration" in classes:
            continue
        unit = candidate
        break
    if unit is None:
        return None
    tickets: list[_Ticket] = []
    for item in unit.find_all("li"):
        if not isinstance(item, Tag):
            continue
        label_node = item.find("dt")
        if not isinstance(label_node, Tag):
            continue
        label = "".join(label_node.get_text().split())
        if not label:
            continue
        for line in item.select(".line"):
            if not isinstance(line, Tag):
                continue
            number = line.select_one(".num")
            yen = line.select_one(".yen")
            if not isinstance(number, Tag) or not isinstance(yen, Tag):
                continue
            combination = "".join(number.get_text().split())
            amount = _yen(yen.get_text())
            if not combination or amount is None:
                continue
            pop_node = line.select_one(".pop")
            popularity = _popularity(pop_node.get_text()) if isinstance(pop_node, Tag) else None
            tickets.append(_Ticket(label, combination, amount, popularity))
    if not tickets:
        return None
    return tickets


def _race_block(html: str, race_number: int) -> str | None:
    marker = f"race_number_main/race_num_{race_number}.png"
    start = html.find(marker)
    if start < 0:
        return None
    nxt = html.find("race_number_main/race_num_", start + len(marker))
    return html[start:] if nxt < 0 else html[start:nxt]


def _race_refunds(html: str, race_number: int) -> list[OfficialRefund] | None:
    """返還 inside this race. The nav link and the payout ``refund_unit`` are not it."""
    block = _race_block(html, race_number)
    if block is None:
        return None
    soup = BeautifulSoup(block, "lxml")
    found: list[OfficialRefund] = []
    for unit in soup.select(".restoration"):
        if not isinstance(unit, Tag):
            continue
        refund = _refund_from_text(unit.get_text(" ", strip=True))
        if refund is not None:
            found.append(refund)
    if not found:
        return None
    return found


_REFUND_BETS = ("単勝", "複勝", "枠連", "馬連", "ワイド", "馬単", "3連複", "3連単")


def _refund_from_text(text: str) -> OfficialRefund | None:
    compact = re.sub(r"\s+", "", text).replace("三連複", "3連複").replace("三連単", "3連単")
    if "返還" not in compact:
        return None
    horses: list[int] = []
    if "返還馬番" in compact:
        head = compact.split("返還馬番", 1)[1].split("返還同枠", 1)[0]
        horses = [int(number) for number in re.findall(r"\d+", head)]
    brackets = [int(number) for number in re.findall(r"返還同枠(\d+)枠", compact)]
    bets = [
        name
        for index, name in sorted(
            (compact.find(name), name) for name in _REFUND_BETS if name in compact
        )
    ]
    if not horses and not brackets:
        return None
    return OfficialRefund(
        tuple(horses),
        tuple(bets) if bets else None,
        tuple(brackets) if brackets else None,
    )


def _yen(text: str) -> int | None:
    compact = text.replace(",", "").replace("，", "")
    match = _YEN.search(compact.replace(" ", ""))
    if match is None:
        digits = "".join(ch for ch in compact if ch.isdigit())
        if not digits:
            return None
        value = int(digits)
    else:
        value = int(match.group(1).replace(",", ""))
    if value <= 0:
        return None
    return value


def _popularity(text: str) -> int | None:
    match = _POP.search("".join(text.split()))
    if match is None:
        return None
    return int(match.group(1))


def _ticket_order(ticket: _Ticket) -> tuple[str, str, int, int]:
    popularity = -1 if ticket.popularity is None else ticket.popularity
    return (ticket.bet_type, ticket.combination, ticket.payout, popularity)


def _core(ticket: _Ticket) -> tuple[str, str, int]:
    return (ticket.bet_type, ticket.combination, ticket.payout)


def _exact_phase(text: str) -> str | None:
    compact = "".join(text.split())
    if compact in {"確定前", "結果確定前", "未確定", "暫定", "provisional"}:
        return "provisional"
    if compact in {"結果確定", "確定", "confirmed"}:
        return "confirmed"
    return None


def _official_phase(html: str, race_number: int) -> str | None:
    """Phase printed on this race of the payout page, else the payout table.

    ``レース結果`` and a top-three table are not a phase. A short 確定 / 確定前
    label wins. Otherwise a payout table with yen amounts is confirmed.
    """
    block = _race_block(html, race_number)
    if block is None:
        return None
    soup = BeautifulSoup(block, "lxml")
    for node in soup.find_all(True):
        if not isinstance(node, Tag):
            continue
        own = "".join(str(piece) for piece in node.find_all(string=True, recursive=False))
        phase = _exact_phase(own)
        if phase is not None:
            return phase
    if _race_tickets(html, race_number):
        return "confirmed"
    return None


def _take_same_core(ticket: _Ticket, official: list[_Ticket], used: list[bool]) -> int | None:
    """Index of an unused official row with the same bet, combination, and yen."""
    fallback: int | None = None
    for index, other in enumerate(official):
        if used[index] or _core(ticket) != _core(other):
            continue
        if ticket.popularity == other.popularity:
            used[index] = True
            return index
        if fallback is None:
            fallback = index
    if fallback is None:
        return None
    used[fallback] = True
    return fallback


def _compare(
    netkeiba: list[tuple[str, str, int, int | None]],
    official: list[_Ticket],
) -> tuple[str | None, tuple[PopularityNote, ...]]:
    """Mismatch text for combination or yen only, plus JRA popularities to record.

    Tickets that match on bet, combination, and yen are not listed, even when
    人気 differs. The warning names only the unpaired tickets.
    """
    left = [_Ticket(bet, combo, payout, pop) for bet, combo, payout, pop in netkeiba]
    right = list(official)
    used = [False] * len(right)
    only_net: list[_Ticket] = []
    notes: list[PopularityNote] = []
    for ticket in left:
        index = _take_same_core(ticket, right, used)
        if index is None:
            only_net.append(ticket)
            continue
        official_pop = right[index].popularity
        if official_pop is None or official_pop == ticket.popularity:
            continue
        notes.append(
            PopularityNote(
                ticket.bet_type,
                ticket.combination,
                ticket.payout,
                ticket.popularity,
                official_pop,
            )
        )
    only_jra = [ticket for index, ticket in enumerate(right) if not used[index]]
    only_net.sort(key=_ticket_order)
    only_jra.sort(key=_ticket_order)
    parts: list[str] = []
    if only_net:
        parts.append("netkeibaのみ: " + "、".join(_fmt(ticket) for ticket in only_net))
    if only_jra:
        parts.append("JRAのみ: " + "、".join(_fmt(ticket) for ticket in only_jra))
    warning = f"{JRA_PAYOUT_MISMATCH}: " + "; ".join(parts) if parts else None
    return warning, tuple(notes)


def format_popularity_note(notes: tuple[PopularityNote, ...] | list[PopularityNote]) -> str | None:
    """Light note for printed popularities that differ. A blank side is silent."""
    printed = [
        note
        for note in notes
        if note.netkeiba is not None and note.netkeiba != note.official
    ]
    if not printed:
        return None
    body = "、".join(
        (
            f"{note.bet_type} {note.combination} {note.payout}円 "
            f"netkeiba {note.netkeiba}人気, JRA {note.official}人気"
        )
        for note in printed
    )
    return f"{JRA_POPULARITY_NOTE}: {body}"


def _fmt(ticket: _Ticket) -> str:
    popularity = "" if ticket.popularity is None else f" {ticket.popularity}人気"
    return f"{ticket.bet_type} {ticket.combination} {ticket.payout}円{popularity}"
