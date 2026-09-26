"""Netkeiba win-odds client.

単勝は次の JSON を使う。

    GET https://race.netkeiba.com/api/api_get_jra_odds.html?race_id={id}&type=1&action=init&compress=0

``action=init`` はブラウザの初回取得と同じ。これを付けない発売中レースは
``data`` が空文字になりやすい。ヘッダをブラウザ相当にしても、空のときは空のまま。
その場合は quotes を空にし、API の reason（例: ``result odds empty``）をそのまま返す。
数値は作らない。

リクエスト Session にはブラウザ相当の UA / Accept / X-Requested-With と、
オッズページへの Referer を載せる。
単勝の馬ごとオッズは ``data.odds["1"]``。各馬は ``[オッズ, 変動, 人気]``。
``status == "yoso"`` のときだけ kind=yoso / source=netkeiba_yoso。
``middle`` / ``result`` で数値があるときは kind=live。
"""

import json
import logging
from datetime import datetime
from typing import Any

from backend.models.odds import OddsKind, OddsQuote, OddsSource, OddsStatus, RaceOdds
from backend.scrapers.base_scraper import BaseScraper
from backend.utils.http_client import HTTPClient

logger = logging.getLogger(__name__)

WIN_ODDS_KEY = "1"
_LIVE_STATUSES = frozenset({"result", "middle"})
_PLACEHOLDERS = frozenset({"", "-", "--", "---", "---.-", "**", "*", "----"})
_MARKET_UPDATED_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
)


class NetkeibaOddsScraper(BaseScraper):
    """Fetch JRA win odds published by netkeiba."""

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
        fetched = fetched_at or datetime.now()
        url = self.build_url(self.ODDS_PATH)
        # action=init はブラウザの初回取得。無いと発売中は data が空になりやすい。
        params = {"race_id": race_id, "type": "1", "action": "init", "compress": "0"}
        headers = {
            **self.BROWSER_HEADERS,
            "Referer": f"{self.BASE_URL}/odds/index.html?type=b1&race_id={race_id}",
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
            logger.error("Failed to fetch win odds for %s: %s", race_id, e)
            return _unavailable(race_id, fetched, "fetch failed")

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
    if not isinstance(payload, dict):
        return _unavailable(race_id, fetched_at, "invalid odds payload")

    reason = _clean_reason(payload.get("reason"))
    raw_status = payload.get("status")
    status_name = raw_status.strip().lower() if isinstance(raw_status, str) else ""
    data = payload.get("data")
    if not isinstance(data, dict):
        return _unavailable(race_id, fetched_at, reason or "odds unavailable")

    odds_root = data.get("odds")
    win_map = odds_root.get(WIN_ODDS_KEY) if isinstance(odds_root, dict) else None
    if not isinstance(win_map, dict) or not win_map:
        return _unavailable(race_id, fetched_at, reason or "odds unavailable")

    kind, source, snapshot_status = _classify(status_name)
    if kind is None or source is None or snapshot_status is None:
        return _unavailable(race_id, fetched_at, reason or f"unsupported odds status: {status_name}")

    market_updated_at = _parse_market_updated_at(data.get("official_datetime"))
    quotes = _quotes_from_win_map(
        win_map,
        race_id=race_id,
        source=source,
        kind=kind,
        fetched_at=fetched_at,
        market_updated_at=market_updated_at,
    )
    if not quotes:
        return _unavailable(race_id, fetched_at, reason or "odds unavailable")

    return RaceOdds(
        race_id=race_id,
        bet_types=["win"],
        prefer="live",
        status=snapshot_status,
        reason=None,
        quotes=quotes,
        fetched_at=fetched_at,
    )


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


def _unavailable(race_id: str, fetched_at: datetime, reason: str | None) -> RaceOdds:
    """Empty snapshot. No placeholder odds."""
    return RaceOdds(
        race_id=race_id,
        bet_types=["win"],
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
