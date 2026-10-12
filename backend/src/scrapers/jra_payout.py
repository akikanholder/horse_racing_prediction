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

from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

JRA_PAYOUT_UNREACHABLE = "JRA official payout was not reachable"
JRA_PAYOUT_FORBIDDEN = "JRA payout returned 403"
JRA_PAYOUT_MISMATCH = "JRA payout mismatch"

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


def cross_check_payouts(
    http_client: object,
    key: OfficialPayoutKey | None,
    payouts: list[tuple[str, str, int, int | None]],
) -> str | None:
    """Return a warning when official payouts are missing or disagree.

    ``payouts`` is the netkeiba list. This function does not invent replacements.
    """
    if key is None or not payouts:
        return None
    post = getattr(http_client, "post", None)
    if not callable(post):
        return JRA_PAYOUT_UNREACHABLE
    try:
        index = _post(post, PAYOUT_INDEX_CNAME)
    except Exception as exc:
        logger.warning("JRA payout index was not reachable: %s", exc)
        return _failure_warning(exc)
    cname = _meeting_cname(index, key)
    if cname is None:
        return JRA_PAYOUT_UNREACHABLE
    try:
        day_html = _post(post, cname)
    except Exception as exc:
        logger.warning("JRA payout day page was not reachable: %s", exc)
        return _failure_warning(exc)
    official = _race_tickets(day_html, key.race_number)
    if official is None:
        return JRA_PAYOUT_UNREACHABLE
    return _mismatch(payouts, official)


def _failure_warning(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status in {401, 403}:
        return JRA_PAYOUT_FORBIDDEN
    return JRA_PAYOUT_UNREACHABLE


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
    marker = f"race_number_main/race_num_{race_number}.png"
    start = html.find(marker)
    if start < 0:
        return None
    nxt = html.find("race_number_main/race_num_", start + len(marker))
    block = html[start:] if nxt < 0 else html[start:nxt]
    soup = BeautifulSoup(block, "lxml")
    unit = soup.select_one(".refund_unit")
    if not isinstance(unit, Tag):
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


def _mismatch(
    netkeiba: list[tuple[str, str, int, int | None]],
    official: list[_Ticket],
) -> str | None:
    left_sorted = sorted(
        (_Ticket(bet, combo, payout, pop) for bet, combo, payout, pop in netkeiba),
        key=_ticket_order,
    )
    right = sorted(official, key=_ticket_order)
    if left_sorted == right:
        return None
    only_net = [ticket for ticket in left_sorted if ticket not in right]
    only_jra = [ticket for ticket in right if ticket not in left_sorted]
    parts: list[str] = []
    if only_net:
        parts.append("netkeibaのみ: " + "、".join(_fmt(ticket) for ticket in only_net))
    if only_jra:
        parts.append("JRAのみ: " + "、".join(_fmt(ticket) for ticket in only_jra))
    if not parts:
        return JRA_PAYOUT_MISMATCH
    return f"{JRA_PAYOUT_MISMATCH}: " + "; ".join(parts)


def _fmt(ticket: _Ticket) -> str:
    popularity = "" if ticket.popularity is None else f" {ticket.popularity}人気"
    return f"{ticket.bet_type} {ticket.combination} {ticket.payout}円{popularity}"
