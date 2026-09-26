"""Netkeiba win, wide, and umaren odds client.

単勝は次の JSON を使う。

    GET https://race.netkeiba.com/api/api_get_jra_odds.html?race_id={id}&type=1&action=init&compress=0

ワイドは同じ API の type=5。Referer はオッズページの type=b5。

    GET https://race.netkeiba.com/api/api_get_jra_odds.html?race_id={id}&type=5&action=init&compress=0

馬連は同じ API の type=4。Referer はオッズページの type=b4
（ナビ ``odds_navi_b4`` が馬連）。

    GET https://race.netkeiba.com/api/api_get_jra_odds.html?race_id={id}&type=4&action=init&compress=0

``action=init`` はブラウザの初回取得と同じ。これを付けない発売中レースは
``data`` が空文字になりやすい。ヘッダをブラウザ相当にしても、空のときは空のまま。
その場合は quotes を空にし、API の reason（例: ``result odds empty``）をそのまま返す。
数値は作らない。prefer=live が unavailable でも、ここでは yoso を取りにいかない。

リクエスト Session にはブラウザ相当の UA / Accept / X-Requested-With と、
オッズページへの Referer を載せる。
単勝の馬ごとオッズは ``data.odds["1"]``。各馬は ``[オッズ, 変動, 人気]``。
ワイドは ``data.odds["5"]``。キーは ``0307`` のような馬番2桁の連結で、
値は ``[下限, 上限, 人気]``。組み合わせは ``3-7``（小さい順）。
下限または上限が無い行は捨てる。片方の数値から範囲は作らない。
馬連は ``data.odds["4"]``。キーはワイドと同じ馬番2桁の連結で、
値は単勝と同じ ``[オッズ, 変動, 人気]``。組み合わせは ``3-7``（小さい順）。
変動は範囲ではないので ``odds_min`` / ``odds_max`` には入れない。
価格が無い行は捨てる。
``status == "yoso"`` のときだけ kind=yoso / source=netkeiba_yoso。
``middle`` / ``result`` で数値があるときは kind=live。
"""

import json
import logging
from datetime import datetime
from typing import Any

from backend.models.odds import (
    BetType,
    OddsKind,
    OddsQuote,
    OddsSource,
    OddsStatus,
    RaceOdds,
)
from backend.scrapers.base_scraper import BaseScraper
from backend.utils.http_client import HTTPClient

logger = logging.getLogger(__name__)

WIN_ODDS_KEY = "1"
WIDE_ODDS_KEY = "5"
UMAREN_ODDS_KEY = "4"
_LIVE_STATUSES = frozenset({"result", "middle"})
_PLACEHOLDERS = frozenset({"", "-", "--", "---", "---.-", "**", "*", "----"})
_MARKET_UPDATED_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
)
# api type param, odds-page Referer type, odds map key
_ODDS_REQUESTS: dict[BetType, tuple[str, str, str]] = {
    "win": ("1", "b1", WIN_ODDS_KEY),
    "wide": ("5", "b5", WIDE_ODDS_KEY),
    "umaren": ("4", "b4", UMAREN_ODDS_KEY),
}


class NetkeibaOddsScraper(BaseScraper):
    """Fetch JRA win, wide, and umaren odds published by netkeiba."""

    BASE_URL = "https://race.netkeiba.com"
    ENCODING = "utf-8"
    ODDS_PATH = "/api/api_get_jra_odds.html"
    BROWSER_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "X-Requested-With": "XMLHttpRequest",
    }

    def __init__(self, http_client: HTTPClient | None = None):
        """Use a Session that already carries browser-like odds headers."""
        if http_client is None:
            http_client = HTTPClient(headers=self.BROWSER_HEADERS)
        super().__init__(http_client)

    def get_win_odds(self, race_id: str, *, fetched_at: datetime | None = None) -> RaceOdds:
        """Fetch win odds for one race.

        Args:
            race_id: netkeiba race id
            fetched_at: Timestamp stored on every quote. Defaults to now.

        Returns:
            Live quotes, yoso quotes, or an empty unavailable snapshot
        """
        return self._get_odds(race_id, "win", fetched_at=fetched_at)

    def get_wide_odds(self, race_id: str, *, fetched_at: datetime | None = None) -> RaceOdds:
        """Fetch wide odds for one race.

        Args:
            race_id: netkeiba race id
            fetched_at: Timestamp stored on every quote. Defaults to now.

        Returns:
            Live quotes, yoso quotes, or an empty unavailable snapshot
        """
        return self._get_odds(race_id, "wide", fetched_at=fetched_at)

    def get_umaren_odds(self, race_id: str, *, fetched_at: datetime | None = None) -> RaceOdds:
        """Fetch umaren (quinella) odds for one race.

        Args:
            race_id: netkeiba race id
            fetched_at: Timestamp stored on every quote. Defaults to now.

        Returns:
            Live quotes, yoso quotes, or an empty unavailable snapshot
        """
        return self._get_odds(race_id, "umaren", fetched_at=fetched_at)

    def _get_odds(
        self,
        race_id: str,
        bet_type: BetType,
        *,
        fetched_at: datetime | None,
    ) -> RaceOdds:
        fetched = fetched_at or datetime.now()
        api_type, page_type, _odds_key = _ODDS_REQUESTS[bet_type]
        url = self.build_url(self.ODDS_PATH)
        # action=init はブラウザの初回取得。無いと発売中は data が空になりやすい。
        params = {
            "race_id": race_id,
            "type": api_type,
            "action": "init",
            "compress": "0",
        }
        headers = {
            **self.BROWSER_HEADERS,
            "Referer": f"{self.BASE_URL}/odds/index.html?type={page_type}&race_id={race_id}",
        }
        try:
            # compress=0 で JSON。HTML 用の euc-jp は使わない。
            body = self.http_client.get(
                url,
                params=params,
                headers=headers,
                encoding=self.ENCODING,
            )
            payload = json.loads(body)
        except Exception as e:
            logger.error("Failed to fetch %s odds for %s: %s", bet_type, race_id, e)
            return _unavailable(race_id, fetched, "fetch failed", bet_type)

        if bet_type == "wide":
            return parse_netkeiba_wide_odds(payload, race_id, fetched_at=fetched)
        if bet_type == "umaren":
            return parse_netkeiba_umaren_odds(payload, race_id, fetched_at=fetched)
        return parse_netkeiba_win_odds(payload, race_id, fetched_at=fetched)


def parse_netkeiba_win_odds(
    payload: object,
    race_id: str,
    *,
    fetched_at: datetime,
) -> RaceOdds:
    """Turn an api_get_jra_odds response into win quotes.

    ``status == "yoso"`` stays yoso. ``result`` and ``middle`` with numbers
    are live market odds. An empty ``data`` string becomes an empty quote
    list and keeps the API reason, such as ``result odds empty``.
    """
    opened = _read_market(
        payload,
        race_id,
        fetched_at,
        odds_key=WIN_ODDS_KEY,
        bet_type="win",
    )
    if isinstance(opened, RaceOdds):
        return opened
    win_map, kind, source, snapshot_status, market_updated_at, reason = opened
    quotes = _quotes_from_win_map(
        win_map,
        race_id=race_id,
        source=source,
        kind=kind,
        fetched_at=fetched_at,
        market_updated_at=market_updated_at,
    )
    if not quotes:
        return _unavailable(race_id, fetched_at, reason or "odds unavailable", "win")

    return RaceOdds(
        race_id=race_id,
        bet_types=["win"],
        prefer="live",
        status=snapshot_status,
        reason=None,
        quotes=quotes,
        fetched_at=fetched_at,
    )


def parse_netkeiba_wide_odds(
    payload: object,
    race_id: str,
    *,
    fetched_at: datetime,
) -> RaceOdds:
    """Turn an api_get_jra_odds type=5 response into wide quotes.

    Each pair is stored as ``lower-higher``. Both ends of the range are
    required. A missing or placeholder end drops that pair instead of
    copying the other price.
    """
    opened = _read_market(
        payload,
        race_id,
        fetched_at,
        odds_key=WIDE_ODDS_KEY,
        bet_type="wide",
    )
    if isinstance(opened, RaceOdds):
        return opened
    wide_map, kind, source, snapshot_status, market_updated_at, reason = opened
    quotes = _quotes_from_wide_map(
        wide_map,
        race_id=race_id,
        source=source,
        kind=kind,
        fetched_at=fetched_at,
        market_updated_at=market_updated_at,
    )
    if not quotes:
        return _unavailable(race_id, fetched_at, reason or "odds unavailable", "wide")

    return RaceOdds(
        race_id=race_id,
        bet_types=["wide"],
        prefer="live",
        status=snapshot_status,
        reason=None,
        quotes=quotes,
        fetched_at=fetched_at,
    )


def parse_netkeiba_umaren_odds(
    payload: object,
    race_id: str,
    *,
    fetched_at: datetime,
) -> RaceOdds:
    """Turn an api_get_jra_odds type=4 response into umaren quotes.

    Each pair is stored as ``lower-higher``. The published price is the first
    field of ``[odds, change, popularity]``, the same shape as win. The change
    field is not a range, so ``odds_min`` and ``odds_max`` stay empty.
    A missing or placeholder price drops that pair.
    """
    opened = _read_market(
        payload,
        race_id,
        fetched_at,
        odds_key=UMAREN_ODDS_KEY,
        bet_type="umaren",
    )
    if isinstance(opened, RaceOdds):
        return opened
    umaren_map, kind, source, snapshot_status, market_updated_at, reason = opened
    quotes = _quotes_from_umaren_map(
        umaren_map,
        race_id=race_id,
        source=source,
        kind=kind,
        fetched_at=fetched_at,
        market_updated_at=market_updated_at,
    )
    if not quotes:
        return _unavailable(race_id, fetched_at, reason or "odds unavailable", "umaren")

    return RaceOdds(
        race_id=race_id,
        bet_types=["umaren"],
        prefer="live",
        status=snapshot_status,
        reason=None,
        quotes=quotes,
        fetched_at=fetched_at,
    )


def _read_market(
    payload: object,
    race_id: str,
    fetched_at: datetime,
    *,
    odds_key: str,
    bet_type: BetType,
) -> (
    tuple[dict[Any, Any], OddsKind, OddsSource, OddsStatus, datetime | None, str | None] | RaceOdds
):
    """Open the odds map, or return an empty snapshot. Does not invent prices."""
    if not isinstance(payload, dict):
        return _unavailable(race_id, fetched_at, "invalid odds payload", bet_type)

    reason = _clean_reason(payload.get("reason"))
    raw_status = payload.get("status")
    status_name = raw_status.strip().lower() if isinstance(raw_status, str) else ""
    data = payload.get("data")
    if not isinstance(data, dict):
        return _unavailable(race_id, fetched_at, reason or "odds unavailable", bet_type)

    odds_root = data.get("odds")
    odds_map = odds_root.get(odds_key) if isinstance(odds_root, dict) else None
    if not isinstance(odds_map, dict) or not odds_map:
        return _unavailable(race_id, fetched_at, reason or "odds unavailable", bet_type)

    kind, source, snapshot_status = _classify(status_name)
    if kind is None or source is None or snapshot_status is None:
        return _unavailable(
            race_id,
            fetched_at,
            reason or f"unsupported odds status: {status_name}",
            bet_type,
        )

    market_updated_at = _parse_market_updated_at(data.get("official_datetime"))
    return odds_map, kind, source, snapshot_status, market_updated_at, reason


def _classify(
    status_name: str,
) -> tuple[OddsKind | None, OddsSource | None, OddsStatus | None]:
    """Map the API status onto quote kind without relabeling yoso as live."""
    if status_name == "yoso":
        return "yoso", "netkeiba_yoso", "yoso"
    if status_name in _LIVE_STATUSES:
        return "live", "netkeiba_live", "live"
    return None, None, None


def _quotes_from_win_map(
    win_map: dict[Any, Any],
    *,
    race_id: str,
    source: OddsSource,
    kind: OddsKind,
    fetched_at: datetime,
    market_updated_at: datetime | None,
) -> list[OddsQuote]:
    """Build one quote per horse. Combination is the horse number as a string."""
    quotes: list[OddsQuote] = []
    seen: set[str] = set()
    for key in sorted(win_map, key=_sort_key):
        combination = _combination(key)
        if combination is None or combination in seen:
            continue
        odds_value = _positive_odds(win_map[key])
        if odds_value is None:
            continue
        seen.add(combination)
        quotes.append(
            OddsQuote(
                race_id=race_id,
                bet_type="win",
                combination=combination,
                odds=odds_value,
                source=source,
                kind=kind,
                fetched_at=fetched_at,
                market_updated_at=market_updated_at,
            )
        )
    return quotes


def _quotes_from_wide_map(
    wide_map: dict[Any, Any],
    *,
    race_id: str,
    source: OddsSource,
    kind: OddsKind,
    fetched_at: datetime,
    market_updated_at: datetime | None,
) -> list[OddsQuote]:
    """Build one quote per unordered pair. Combination is lower-higher."""
    candidates: list[tuple[tuple[int, int, int, str], str, float, float]] = []
    for key, raw in wide_map.items():
        pair = _wide_pair(key)
        odds_range = _odds_range(raw)
        if pair is None or odds_range is None or not isinstance(key, str):
            continue
        combination = f"{pair[0]}-{pair[1]}"
        odds_min, odds_max = odds_range
        candidates.append((_pair_sort(key, pair), combination, odds_min, odds_max))

    quotes: list[OddsQuote] = []
    seen: set[str] = set()
    for _sort, combination, odds_min, odds_max in sorted(candidates):
        if combination in seen:
            continue
        seen.add(combination)
        quotes.append(
            OddsQuote(
                race_id=race_id,
                bet_type="wide",
                combination=combination,
                odds_min=odds_min,
                odds_max=odds_max,
                source=source,
                kind=kind,
                fetched_at=fetched_at,
                market_updated_at=market_updated_at,
            )
        )
    return quotes


def _quotes_from_umaren_map(
    umaren_map: dict[Any, Any],
    *,
    race_id: str,
    source: OddsSource,
    kind: OddsKind,
    fetched_at: datetime,
    market_updated_at: datetime | None,
) -> list[OddsQuote]:
    """Build one quote per unordered pair. Price is a single odds value."""
    candidates: list[tuple[tuple[int, int, int, str], str, float]] = []
    for key, raw in umaren_map.items():
        pair = _wide_pair(key)
        odds_value = _positive_odds(raw)
        if pair is None or odds_value is None or not isinstance(key, str):
            continue
        combination = f"{pair[0]}-{pair[1]}"
        candidates.append((_pair_sort(key, pair), combination, odds_value))

    quotes: list[OddsQuote] = []
    seen: set[str] = set()
    for _sort, combination, odds_value in sorted(candidates):
        if combination in seen:
            continue
        seen.add(combination)
        quotes.append(
            OddsQuote(
                race_id=race_id,
                bet_type="umaren",
                combination=combination,
                odds=odds_value,
                source=source,
                kind=kind,
                fetched_at=fetched_at,
                market_updated_at=market_updated_at,
            )
        )
    return quotes


def _unavailable(
    race_id: str,
    fetched_at: datetime,
    reason: str | None,
    bet_type: BetType = "win",
) -> RaceOdds:
    """Empty snapshot. No placeholder odds."""
    return RaceOdds(
        race_id=race_id,
        bet_types=[bet_type],
        prefer="live",
        status="unavailable",
        reason=reason,
        quotes=[],
        fetched_at=fetched_at,
    )


def _clean_reason(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _parse_market_updated_at(value: object) -> datetime | None:
    """Parse official_datetime when the payload exposes an update time."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    for fmt in _MARKET_UPDATED_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _combination(key: object) -> str | None:
    """Horse number, without the API's zero padding."""
    if isinstance(key, bool):
        return None
    if isinstance(key, int):
        number = key
    elif isinstance(key, str) and key.strip().isdigit():
        number = int(key.strip())
    else:
        return None
    if not 1 <= number <= 18:
        return None
    return str(number)


def _sort_key(key: object) -> tuple[int, str]:
    combination = _combination(key)
    if combination is None:
        return (10_000, str(key))
    return (int(combination), combination)


def _wide_pair(key: object) -> tuple[int, int] | None:
    """Read an unordered horse pair from ``0307`` or ``3-7``."""
    if not isinstance(key, str):
        return None
    text = key.strip()
    if len(text) == 4 and text.isdigit():
        left, right = int(text[:2]), int(text[2:])
    else:
        left_text, sep, right_text = text.partition("-")
        if sep != "-" or "-" in right_text:
            return None
        if not left_text.strip().isdigit() or not right_text.strip().isdigit():
            return None
        left, right = int(left_text), int(right_text)
    if left == right:
        return None
    if left > right:
        left, right = right, left
    if not (1 <= left <= 18 and 1 <= right <= 18):
        return None
    return left, right


def _pair_sort(key: str, pair: tuple[int, int]) -> tuple[int, int, int, str]:
    """Sort by horses. An already lower-higher key wins over its reverse."""
    text = key.strip()
    ordered = 1
    if len(text) == 4 and text.isdigit() and (int(text[:2]), int(text[2:])) == pair:
        ordered = 0
    elif text == f"{pair[0]}-{pair[1]}":
        ordered = 0
    return (pair[0], pair[1], ordered, text)


def _odds_range(value: object) -> tuple[float, float] | None:
    """Read ``[odds_min, odds_max, popularity]``. Both ends are required."""
    if not isinstance(value, list | tuple) or len(value) < 2:
        return None
    low = _positive_odds(value[0])
    high = _positive_odds(value[1])
    if low is None or high is None:
        return None
    if low > high:
        low, high = high, low
    return low, high


def _positive_odds(value: object) -> float | None:
    """Read the win price from ``[odds, change, popularity]`` or a scalar."""
    if isinstance(value, list | tuple):
        if not value:
            return None
        value = value[0]
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if text in _PLACEHOLDERS:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    elif isinstance(value, int | float) and not isinstance(value, bool):
        number = float(value)
    else:
        return None
    if number <= 0:
        return None
    return number
