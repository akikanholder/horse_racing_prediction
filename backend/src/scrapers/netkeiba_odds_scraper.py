"""Netkeiba win, wide, umaren, and sanrenpuku odds client.

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
掲載上限 ``9,999.9`` で2列目が ``0`` または負の変動のときは、両端がその上限の点範囲。
``2.9`` / ``0.0`` のような片側だけの行は従来どおり捨てる。
馬連は ``data.odds["4"]``。キーはワイドと同じ馬番2桁の連結で、
値は単勝と同じ ``[オッズ, 変動, 人気]``。組み合わせは ``3-7``（小さい順）。
変動は範囲ではないので ``odds_min`` / ``odds_max`` には入れない。
価格が無い行は捨てる。``----`` と ``取消`` / ``除外`` は捨てず、価格は null のまま残す。
三連複は同じ API の type=7。Referer はオッズページの type=b7
（ナビ ``odds_navi_b7`` が 3連複）。

    GET https://race.netkeiba.com/api/api_get_jra_odds.html?race_id={id}&type=7&action=init&compress=0

三連複は ``data.odds["7"]``。キーは ``010205`` のような馬番2桁×3の連結で、
値は単勝・馬連と同じ ``[オッズ, 変動, 人気]``。組み合わせは ``1-2-5``（小さい順）。
変動は範囲ではないので ``odds_min`` / ``odds_max`` には入れない。
価格が無い行は捨てる。
``status == "yoso"`` のときだけ kind=yoso / source=netkeiba_yoso。
``middle`` / ``result`` で数値があるときは kind=live。
文字コードは ``encoding=utf-8``。馬成績ページの EUC-JP では読まない。
``race_phase`` は API の status から決める。``yoso`` と ``middle`` は ``pre``
（レース前）、``result`` は ``post``（レース後の確定）。
HTTP 400 や空の確定オッズは quotes を空にし、reason を返す。価格は作らない。
"""

import json
import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import requests

from backend.models.odds import (
    ODDS_ENCODING,
    BetType,
    NonRunner,
    OddsKind,
    OddsPhase,
    OddsQuote,
    OddsSource,
    OddsStatus,
    RaceOdds,
    RacePhase,
)
from backend.scrapers.base_scraper import BaseScraper
from backend.scrapers.page_fallback import (
    looks_like_forbidden_page,
    looks_like_login_wall,
    page_block_reason,
)
from backend.scrapers.runner_status import (
    PriceCell,
    RangeCell,
    non_runners_from_cells,
    read_popularity,
    read_price_cell,
    read_range_cell,
    records_from_json,
)
from backend.utils.http_client import HTTPClient

logger = logging.getLogger(__name__)

WIN_ODDS_KEY = "1"
WIDE_ODDS_KEY = "5"
UMAREN_ODDS_KEY = "4"
SANRENPUKU_ODDS_KEY = "7"
PLACE_ODDS_KEY = "2"
_LIVE_STATUSES = frozenset({"result", "middle"})
# 前日暫定。API の status 名。middle に「暫定」が付くときも同じ局面。
_PROVISIONAL_STATUSES = frozenset({"provisional", "tentative", "zenjitsu", "kari", "temp"})
_ODDS_ATTEMPTS = 3
_ODDS_BACKOFF_SECONDS = 0.5
_TRANSIENT_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})
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
    "sanrenpuku": ("7", "b7", SANRENPUKU_ODDS_KEY),
    # 複勝は過去の確定オッズだけが読む。発売中の odds コマンドは要求しない。
    "place": ("2", "b2", PLACE_ODDS_KEY),
}


class NetkeibaOddsScraper(BaseScraper):
    """Fetch JRA win, wide, umaren, and sanrenpuku odds published by netkeiba."""

    BASE_URL = "https://race.netkeiba.com"
    ENCODING = ODDS_ENCODING
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

    def __init__(
        self,
        http_client: HTTPClient | None = None,
        *,
        max_attempts: int = _ODDS_ATTEMPTS,
        backoff_seconds: float = _ODDS_BACKOFF_SECONDS,
        sleeper: Callable[[float], None] | None = None,
    ):
        """Use a Session that already carries browser-like odds headers.

        Transient failures (undecided HTTP 400, 5xx, timeout, connection
        error) are retried ``max_attempts`` times with exponential backoff.
        401, 403, a captcha, and a challenge are not retried. The odds client
        does not also let urllib3 retry those statuses.
        """
        if http_client is None:
            http_client = HTTPClient(headers=self.BROWSER_HEADERS, max_retries=0)
        super().__init__(http_client)
        self._max_attempts = max(1, max_attempts)
        self._backoff_seconds = backoff_seconds
        self._sleeper = sleeper or time.sleep

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

    def get_sanrenpuku_odds(self, race_id: str, *, fetched_at: datetime | None = None) -> RaceOdds:
        """Fetch sanrenpuku (trio) odds for one race.

        Args:
            race_id: netkeiba race id
            fetched_at: Timestamp stored on every quote. Defaults to now.

        Returns:
            Live quotes, yoso quotes, or an empty unavailable snapshot
        """
        return self._get_odds(race_id, "sanrenpuku", fetched_at=fetched_at)

    def get_place_odds(self, race_id: str, *, fetched_at: datetime | None = None) -> RaceOdds:
        """Fetch place odds. Historical final odds are the only caller.

        The live odds command does not request place. A pre-race payload is
        still parsed here; the caller drops it unless race_phase is post.
        """
        return self._get_odds(race_id, "place", fetched_at=fetched_at)

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
            body = self._get_odds_body(url, params, headers, bet_type, race_id)
        except requests.HTTPError as e:
            # 400 の本文に数値が混ざっていても価格にはしない。
            logger.error("Failed to fetch %s odds for %s: %s", bet_type, race_id, e)
            return _unavailable_from_http_error(e, race_id, fetched, bet_type)
        except Exception as e:
            logger.error("Failed to fetch %s odds for %s: %s", bet_type, race_id, e)
            return _unavailable(race_id, fetched, "fetch failed", bet_type)
        if body is None:
            return _unavailable(race_id, fetched, "fetch failed", bet_type)
        refusal = _refusal_reason(body)
        if refusal is not None:
            return _unavailable(race_id, fetched, refusal, bet_type)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            logger.error("Failed to fetch %s odds for %s: %s", bet_type, race_id, e)
            return _unavailable(race_id, fetched, "malformed odds payload", bet_type)

        if bet_type == "wide":
            return parse_netkeiba_wide_odds(payload, race_id, fetched_at=fetched)
        if bet_type == "umaren":
            return parse_netkeiba_umaren_odds(payload, race_id, fetched_at=fetched)
        if bet_type == "sanrenpuku":
            return parse_netkeiba_sanrenpuku_odds(payload, race_id, fetched_at=fetched)
        if bet_type == "place":
            return parse_netkeiba_place_odds(payload, race_id, fetched_at=fetched)
        return parse_netkeiba_win_odds(payload, race_id, fetched_at=fetched)

    def _get_odds_body(
        self,
        url: str,
        params: dict[str, str],
        headers: dict[str, str],
        bet_type: BetType,
        race_id: str,
    ) -> str | None:
        """GET the odds API. Retry only transient failures.

        A 400 whose JSON already names ``status`` or ``reason`` is the API's
        answer and is not retried. 401, 403, captcha, and a challenge stop
        on the first response.
        """
        last: Exception | None = None
        for attempt in range(self._max_attempts):
            if attempt:
                delay = self._backoff_seconds * (2 ** (attempt - 1))
                logger.warning(
                    "Retrying %s odds for %s in %.2fs after a transient error (%s/%s)",
                    bet_type,
                    race_id,
                    delay,
                    attempt + 1,
                    self._max_attempts,
                )
                self._sleeper(delay)
            try:
                return self.http_client.get(
                    url,
                    params=params,
                    headers=headers,
                    encoding=self.ENCODING,
                )
            except requests.HTTPError as exc:
                last = exc
                if not _transient_odds_error(exc):
                    raise
            except (requests.Timeout, requests.ConnectionError) as exc:
                last = exc
            except requests.RequestException as exc:
                last = exc
                if not _transient_odds_error(exc):
                    raise
        if last is not None:
            raise last
        return None


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
    (
        win_map,
        kind,
        source,
        snapshot_status,
        market_updated_at,
        reason,
        race_phase,
        odds_phase,
        is_official_market,
    ) = opened
    quotes, runners = _quotes_from_win_map(
        win_map,
        race_id=race_id,
        source=source,
        kind=kind,
        fetched_at=fetched_at,
        market_updated_at=market_updated_at,
    )
    runners = _merge_runner_lists(runners, _runners_in_payload(payload, fetched_at))
    if not quotes:
        empty = _unavailable(
            race_id,
            fetched_at,
            reason or "odds unavailable",
            "win",
            race_phase=race_phase,
            odds_phase=odds_phase,
            is_official_market=is_official_market,
        )
        return empty.model_copy(update={"non_runners": runners})

    return RaceOdds(
        race_id=race_id,
        bet_types=["win"],
        prefer="live",
        status=snapshot_status,
        reason=None,
        encoding=ODDS_ENCODING,
        race_phase=race_phase,
        odds_phase=odds_phase,
        is_official_market=is_official_market,
        quotes=quotes,
        non_runners=runners,
        fetched_at=fetched_at,
    )


def parse_netkeiba_place_odds(
    payload: object,
    race_id: str,
    *,
    fetched_at: datetime,
) -> RaceOdds:
    """Turn an api_get_jra_odds type=2 response into place quotes.

    A list is a range (``odds_min`` / ``odds_max``), the same shape as wide.
    A single number stays in ``odds``. One end of a range drops the row.
    Callers that need final odds keep this only when ``race_phase`` is post.
    """
    opened = _read_market(
        payload,
        race_id,
        fetched_at,
        odds_key=PLACE_ODDS_KEY,
        bet_type="place",
    )
    if isinstance(opened, RaceOdds):
        return opened
    place_map, kind, source, snapshot_status, market_updated_at, reason, race_phase, odds_phase, is_official_market = opened
    quotes, runners = _quotes_from_place_map(
        place_map,
        race_id=race_id,
        source=source,
        kind=kind,
        fetched_at=fetched_at,
        market_updated_at=market_updated_at,
    )
    runners = _merge_runner_lists(runners, _runners_in_payload(payload, fetched_at))
    if not quotes:
        empty = _unavailable(
            race_id,
            fetched_at,
            reason or "odds unavailable",
            "place",
            race_phase=race_phase,
            odds_phase=odds_phase,
            is_official_market=is_official_market,
        )
        return empty.model_copy(update={"non_runners": runners})
    return RaceOdds(
        race_id=race_id,
        bet_types=["place"],
        prefer="live",
        status=snapshot_status,
        reason=None,
        encoding=ODDS_ENCODING,
        race_phase=race_phase,
        odds_phase=odds_phase,
        is_official_market=is_official_market,
        quotes=quotes,
        non_runners=runners,
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
    wide_map, kind, source, snapshot_status, market_updated_at, reason, race_phase, odds_phase, is_official_market = opened
    quotes, runners = _quotes_from_wide_map(
        wide_map,
        race_id=race_id,
        source=source,
        kind=kind,
        fetched_at=fetched_at,
        market_updated_at=market_updated_at,
    )
    runners = _merge_runner_lists(runners, _runners_in_payload(payload, fetched_at))
    if not quotes:
        empty = _unavailable(
            race_id,
            fetched_at,
            reason or "odds unavailable",
            "wide",
            race_phase=race_phase,
            odds_phase=odds_phase,
            is_official_market=is_official_market,
        )
        return empty.model_copy(update={"non_runners": runners})

    return RaceOdds(
        race_id=race_id,
        bet_types=["wide"],
        prefer="live",
        status=snapshot_status,
        reason=None,
        encoding=ODDS_ENCODING,
        race_phase=race_phase,
        odds_phase=odds_phase,
        is_official_market=is_official_market,
        quotes=quotes,
        non_runners=runners,
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
    umaren_map, kind, source, snapshot_status, market_updated_at, reason, race_phase, odds_phase, is_official_market = opened
    quotes, runners = _quotes_from_umaren_map(
        umaren_map,
        race_id=race_id,
        source=source,
        kind=kind,
        fetched_at=fetched_at,
        market_updated_at=market_updated_at,
    )
    runners = _merge_runner_lists(runners, _runners_in_payload(payload, fetched_at))
    if not quotes:
        empty = _unavailable(
            race_id,
            fetched_at,
            reason or "odds unavailable",
            "umaren",
            race_phase=race_phase,
            odds_phase=odds_phase,
            is_official_market=is_official_market,
        )
        return empty.model_copy(update={"non_runners": runners})

    return RaceOdds(
        race_id=race_id,
        bet_types=["umaren"],
        prefer="live",
        status=snapshot_status,
        reason=None,
        encoding=ODDS_ENCODING,
        race_phase=race_phase,
        odds_phase=odds_phase,
        is_official_market=is_official_market,
        quotes=quotes,
        non_runners=runners,
        fetched_at=fetched_at,
    )


def parse_netkeiba_sanrenpuku_odds(
    payload: object,
    race_id: str,
    *,
    fetched_at: datetime,
) -> RaceOdds:
    """Turn an api_get_jra_odds type=7 response into sanrenpuku quotes.

    Each trio is stored as ``low-mid-high``, such as ``1-2-5``. The published
    price is the first field of ``[odds, change, popularity]``, the same shape
    as umaren. The change field is not a range, so ``odds_min`` and
    ``odds_max`` stay empty. A missing or placeholder price drops that trio.
    """
    opened = _read_market(
        payload,
        race_id,
        fetched_at,
        odds_key=SANRENPUKU_ODDS_KEY,
        bet_type="sanrenpuku",
    )
    if isinstance(opened, RaceOdds):
        return opened
    trio_map, kind, source, snapshot_status, market_updated_at, reason, race_phase, odds_phase, is_official_market = opened
    quotes, runners = _quotes_from_sanrenpuku_map(
        trio_map,
        race_id=race_id,
        source=source,
        kind=kind,
        fetched_at=fetched_at,
        market_updated_at=market_updated_at,
    )
    runners = _merge_runner_lists(runners, _runners_in_payload(payload, fetched_at))
    if not quotes:
        empty = _unavailable(
            race_id,
            fetched_at,
            reason or "odds unavailable",
            "sanrenpuku",
            race_phase=race_phase,
            odds_phase=odds_phase,
            is_official_market=is_official_market,
        )
        return empty.model_copy(update={"non_runners": runners})

    return RaceOdds(
        race_id=race_id,
        bet_types=["sanrenpuku"],
        prefer="live",
        status=snapshot_status,
        reason=None,
        encoding=ODDS_ENCODING,
        race_phase=race_phase,
        odds_phase=odds_phase,
        is_official_market=is_official_market,
        quotes=quotes,
        non_runners=runners,
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
    tuple[
        dict[Any, Any],
        OddsKind,
        OddsSource,
        OddsStatus,
        datetime | None,
        str | None,
        RacePhase | None,
        OddsPhase | None,
        bool | None,
    ]
    | RaceOdds
):
    """Open the odds map, or return an empty snapshot. Does not invent prices."""
    if not isinstance(payload, dict):
        return _unavailable(race_id, fetched_at, "invalid odds payload", bet_type)

    reason = _clean_reason(payload.get("reason"))
    raw_status = payload.get("status")
    status_name = raw_status.strip().lower() if isinstance(raw_status, str) else ""
    phase_args = _phase_args(status_name, payload)
    data = payload.get("data")
    if not isinstance(data, dict):
        return _unavailable(
            race_id,
            fetched_at,
            reason or "odds unavailable",
            bet_type,
            **phase_args,
        )

    odds_root = data.get("odds")
    odds_map = odds_root.get(odds_key) if isinstance(odds_root, dict) else None
    if not isinstance(odds_map, dict) or not odds_map:
        return _unavailable(
            race_id,
            fetched_at,
            reason or "odds unavailable",
            bet_type,
            **phase_args,
        )

    kind, source, snapshot_status = _classify(status_name)
    if kind is None or source is None or snapshot_status is None:
        return _unavailable(
            race_id,
            fetched_at,
            reason or f"unsupported odds status: {status_name}",
            bet_type,
            **phase_args,
        )

    market_updated_at = _parse_market_updated_at(data.get("official_datetime"))
    return (
        odds_map,
        kind,
        source,
        snapshot_status,
        market_updated_at,
        reason,
        phase_args["race_phase"],
        phase_args["odds_phase"],
        phase_args["is_official_market"],
    )


def _classify(
    status_name: str,
) -> tuple[OddsKind | None, OddsSource | None, OddsStatus | None]:
    """Map the API status onto quote kind without relabeling yoso as live.

    A provisional pre-sale board is still ``kind=live``. ``odds_phase`` says
    ``provisional`` and ``is_official_market`` is false. ``kind`` is not rewritten.
    """
    if status_name == "yoso":
        return "yoso", "netkeiba_yoso", "yoso"
    if status_name in _LIVE_STATUSES or status_name in _PROVISIONAL_STATUSES:
        return "live", "netkeiba_live", "live"
    return None, None, None


def _phase_from_status(status_name: str) -> RacePhase | None:
    """Map netkeiba status onto race-before (pre) or race-after (post).

    ``yoso``, ``middle``, and a provisional pre-sale status are before the race.
    ``result`` is the final market after the race.
    """
    if status_name in {"yoso", "middle"} or status_name in _PROVISIONAL_STATUSES:
        return "pre"
    if status_name == "result":
        return "post"
    return None


_PHASE_LABEL_KEYS = (
    "reason",
    "message",
    "label",
    "state",
    "phase",
    "odds_status",
    "odds_type",
    "sale_status",
    "sale_type",
    "odds_kbn",
)


def _phase_label(payload: object) -> str:
    """Short labels the odds API uses for the board. Prices are not labels."""
    if not isinstance(payload, dict):
        return ""
    parts: list[str] = []

    def take(obj: dict[str, Any]) -> None:
        for key in _PHASE_LABEL_KEYS:
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
        for key in ("provisional", "is_provisional", "tentative"):
            if obj.get(key) is True:
                parts.append("暫定")

    take(payload)
    data = payload.get("data")
    if isinstance(data, dict):
        take(data)
    return " ".join(parts)


def _sale_phase(status_name: str, payload: object) -> tuple[OddsPhase | None, bool | None]:
    """forecast / provisional / live / final, and whether that board is official.

    ``is_official_market`` is true only for 発売中 and 確定. 予想 and 前日暫定
    are false. An unnamed failure is null. Prices are not filled from this flag.
    """
    label = _phase_label(payload)
    named_provisional = "暫定" in label
    if status_name == "yoso":
        return "forecast", False
    if status_name == "result":
        return "final", True
    if status_name in _PROVISIONAL_STATUSES or (
        status_name in {"middle", "live"} and named_provisional
    ):
        return "provisional", False
    if status_name in {"middle", "live"}:
        return "live", True
    if named_provisional:
        return "provisional", False
    return None, None


def _phase_args(status_name: str, payload: object) -> dict[str, Any]:
    odds_phase, official = _sale_phase(status_name, payload)
    return {
        "race_phase": _phase_from_status(status_name),
        "odds_phase": odds_phase,
        "is_official_market": official,
    }


def _quotes_from_win_map(
    win_map: dict[Any, Any],
    *,
    race_id: str,
    source: OddsSource,
    kind: OddsKind,
    fetched_at: datetime,
    market_updated_at: datetime | None,
) -> tuple[list[OddsQuote], list[NonRunner]]:
    """Build one quote per horse. Combination is the horse number as a string.

    A cell printed as ``----`` or ``取消`` stays, with a null price.
    An empty cell is still dropped.
    """
    quotes: list[OddsQuote] = []
    seen: set[str] = set()
    priced: list[tuple[str, PriceCell]] = []
    for key in sorted(win_map, key=_sort_key):
        combination = _combination(key)
        if combination is None or combination in seen:
            continue
        cell = read_price_cell(win_map[key])
        if cell is None:
            continue
        seen.add(combination)
        priced.append((combination, cell))
        quotes.append(
            OddsQuote(
                race_id=race_id,
                bet_type="win",
                combination=combination,
                odds=cell.odds,
                source=source,
                kind=kind,
                fetched_at=fetched_at,
                market_updated_at=market_updated_at,
                non_runner=cell.non_runner,
                withheld=cell.withheld,
            )
        )
    runners = non_runners_from_cells(priced, source="netkeiba_js", fetched_at=fetched_at)
    return quotes, runners


def _quotes_from_place_map(
    place_map: dict[Any, Any],
    *,
    race_id: str,
    source: OddsSource,
    kind: OddsKind,
    fetched_at: datetime,
    market_updated_at: datetime | None,
) -> tuple[list[OddsQuote], list[NonRunner]]:
    """One place quote per horse. A list is a range. A scalar is a single price."""
    quotes: list[OddsQuote] = []
    seen: set[str] = set()
    priced: list[tuple[str, PriceCell]] = []
    for key in sorted(place_map, key=_sort_key):
        combination = _combination(key)
        if combination is None or combination in seen:
            continue
        raw = place_map[key]
        if isinstance(raw, list | tuple) and len(raw) >= 2 and not _place_list_is_single(raw):
            span = read_range_cell(raw)
            if span is None:
                continue
            seen.add(combination)
            quotes.append(
                OddsQuote(
                    race_id=race_id,
                    bet_type="place",
                    combination=combination,
                    odds=None,
                    odds_min=span.odds_min,
                    odds_max=span.odds_max,
                    source=source,
                    kind=kind,
                    fetched_at=fetched_at,
                    market_updated_at=market_updated_at,
                    non_runner=span.non_runner,
                    withheld=span.withheld,
                )
            )
            continue
        cell = read_price_cell(raw)
        if cell is None:
            continue
        seen.add(combination)
        priced.append((combination, cell))
        quotes.append(
            OddsQuote(
                race_id=race_id,
                bet_type="place",
                combination=combination,
                odds=None if cell.withheld else cell.odds,
                source=source,
                kind=kind,
                fetched_at=fetched_at,
                market_updated_at=market_updated_at,
                non_runner=cell.non_runner,
                withheld=cell.withheld,
            )
        )
    runners = non_runners_from_cells(priced, source="netkeiba_js", fetched_at=fetched_at)
    return quotes, runners


def _place_list_is_single(raw: list | tuple) -> bool:
    """Win-shaped ``[odds, change, popularity]`` is not a place range.

    A place range is two prices. A change of ``0`` or ``0.0`` next to one
    price is the win/umaren shape and is read as a single price.
    """
    if len(raw) < 2:
        return True
    second = raw[1]
    if isinstance(second, str) and second.strip() in {"", "0", "0.0", "0.00"}:
        return True
    if isinstance(second, int | float) and not isinstance(second, bool) and float(second) == 0:
        return True
    return False


def _quotes_from_wide_map(
    wide_map: dict[Any, Any],
    *,
    race_id: str,
    source: OddsSource,
    kind: OddsKind,
    fetched_at: datetime,
    market_updated_at: datetime | None,
) -> tuple[list[OddsQuote], list[NonRunner]]:
    """Build one quote per unordered pair. Combination is lower-higher.

    ``----`` or ``取消`` keeps the pair and nulls both ends. One missing end
    still drops the pair. The present end is not copied into a range.
    The published ceiling ``9,999.9`` with a non-positive second field is
    both ends.
    """
    candidates: list[tuple[tuple[int, int, int, str], str, RangeCell, int | None]] = []
    for key, raw in wide_map.items():
        pair = _wide_pair(key)
        odds_range = read_range_cell(raw)
        if pair is None or odds_range is None or not isinstance(key, str):
            continue
        combination = f"{pair[0]}-{pair[1]}"
        candidates.append((_pair_sort(key, pair), combination, odds_range, read_popularity(raw)))

    quotes: list[OddsQuote] = []
    seen: set[str] = set()
    for _sort, combination, odds_range, popularity in sorted(candidates):
        if combination in seen:
            continue
        seen.add(combination)
        quotes.append(
            OddsQuote(
                race_id=race_id,
                bet_type="wide",
                combination=combination,
                odds_min=odds_range.odds_min,
                odds_max=odds_range.odds_max,
                popularity=popularity,
                source=source,
                kind=kind,
                fetched_at=fetched_at,
                market_updated_at=market_updated_at,
                non_runner=odds_range.non_runner,
                withheld=odds_range.withheld,
            )
        )
    return quotes, []


def _quotes_from_umaren_map(
    umaren_map: dict[Any, Any],
    *,
    race_id: str,
    source: OddsSource,
    kind: OddsKind,
    fetched_at: datetime,
    market_updated_at: datetime | None,
) -> tuple[list[OddsQuote], list[NonRunner]]:
    """Build one quote per unordered pair. Price is a single odds value.

    ``----`` or ``取消`` keeps the pair with a null price. An empty cell is dropped.
    """
    candidates: list[tuple[tuple[int, int, int, str], str, PriceCell, int | None]] = []
    for key, raw in umaren_map.items():
        pair = _wide_pair(key)
        cell = read_price_cell(raw)
        if pair is None or cell is None or not isinstance(key, str):
            continue
        combination = f"{pair[0]}-{pair[1]}"
        candidates.append((_pair_sort(key, pair), combination, cell, read_popularity(raw)))

    quotes: list[OddsQuote] = []
    seen: set[str] = set()
    for _sort, combination, cell, popularity in sorted(candidates):
        if combination in seen:
            continue
        seen.add(combination)
        quotes.append(
            OddsQuote(
                race_id=race_id,
                bet_type="umaren",
                combination=combination,
                odds=cell.odds,
                popularity=popularity,
                source=source,
                kind=kind,
                fetched_at=fetched_at,
                market_updated_at=market_updated_at,
                non_runner=cell.non_runner,
                withheld=cell.withheld,
            )
        )
    return quotes, []


def _quotes_from_sanrenpuku_map(
    trio_map: dict[Any, Any],
    *,
    race_id: str,
    source: OddsSource,
    kind: OddsKind,
    fetched_at: datetime,
    market_updated_at: datetime | None,
) -> tuple[list[OddsQuote], list[NonRunner]]:
    """Build one quote per unordered trio. Price is a single odds value.

    ``----`` or ``取消`` keeps the trio with a null price. An empty cell is dropped.
    """
    candidates: list[tuple[tuple[int, int, int, int, str], str, PriceCell, int | None]] = []
    for key, raw in trio_map.items():
        trio = _trio(key)
        cell = read_price_cell(raw)
        if trio is None or cell is None or not isinstance(key, str):
            continue
        combination = f"{trio[0]}-{trio[1]}-{trio[2]}"
        candidates.append((_trio_sort(key, trio), combination, cell, read_popularity(raw)))

    quotes: list[OddsQuote] = []
    seen: set[str] = set()
    for _sort, combination, cell, popularity in sorted(candidates):
        if combination in seen:
            continue
        seen.add(combination)
        quotes.append(
            OddsQuote(
                race_id=race_id,
                bet_type="sanrenpuku",
                combination=combination,
                odds=cell.odds,
                popularity=popularity,
                source=source,
                kind=kind,
                fetched_at=fetched_at,
                market_updated_at=market_updated_at,
                non_runner=cell.non_runner,
                withheld=cell.withheld,
            )
        )
    return quotes, []


def _runners_in_payload(payload: object, fetched_at: datetime) -> list[NonRunner]:
    """Scratch list embedded beside the odds map. Odds numbers are not horses."""
    if not isinstance(payload, dict):
        return []
    runners: list[NonRunner] = []
    for record in records_from_json(json.dumps(payload)):
        if record.status not in {"scratched", "excluded", "unknown"}:
            continue
        runners.append(
            NonRunner(
                horse_number=record.horse_number,
                status=record.status,
                status_label=record.status_label,
                source="netkeiba_js",
                fetched_at=fetched_at,
            )
        )
    return runners


def _merge_runner_lists(primary: list[NonRunner], extra: list[NonRunner]) -> list[NonRunner]:
    """Keep the first mark for a horse number. Do not invent a second one."""
    by_number = {runner.horse_number: runner for runner in primary}
    for runner in extra:
        by_number.setdefault(runner.horse_number, runner)
    return [by_number[number] for number in sorted(by_number)]


def _unavailable(
    race_id: str,
    fetched_at: datetime,
    reason: str | None,
    bet_type: BetType = "win",
    *,
    race_phase: RacePhase | None = None,
    odds_phase: OddsPhase | None = None,
    is_official_market: bool | None = None,
) -> RaceOdds:
    """Empty snapshot. No placeholder odds."""
    return RaceOdds(
        race_id=race_id,
        bet_types=[bet_type],
        prefer="live",
        status="unavailable",
        reason=reason,
        encoding=ODDS_ENCODING,
        race_phase=race_phase,
        odds_phase=odds_phase,
        is_official_market=is_official_market,
        quotes=[],
        fetched_at=fetched_at,
    )


def _unavailable_from_http_error(
    exc: requests.HTTPError,
    race_id: str,
    fetched_at: datetime,
    bet_type: BetType,
) -> RaceOdds:
    """HTTP failures stay empty. Numbers in a 400 body are not quotes."""
    response = exc.response
    code = response.status_code if response is not None else None
    reason = f"http {code}" if isinstance(code, int) else "fetch failed"
    phase_args: dict[str, Any] = {
        "race_phase": None,
        "odds_phase": None,
        "is_official_market": None,
    }
    if response is not None:
        payload = _json_object_from_response(response)
        if payload is not None:
            api_reason = _clean_reason(payload.get("reason"))
            if api_reason and isinstance(code, int):
                reason = f"http {code}: {api_reason}"
            raw_status = payload.get("status")
            status_name = raw_status.strip().lower() if isinstance(raw_status, str) else ""
            phase_args = _phase_args(status_name, payload)
        elif _body_is_refusal(_response_text(response)):
            reason = _refusal_reason(_response_text(response)) or reason
    return _unavailable(race_id, fetched_at, reason, bet_type, **phase_args)


def _json_object_from_response(response: requests.Response) -> dict[Any, Any] | None:
    """Read an error body as UTF-8 JSON. Odds inside it are ignored by the caller."""
    response.encoding = ODDS_ENCODING
    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _clean_reason(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _response_text(response: requests.Response) -> str:
    """Error body as text. A missing encoding stays UTF-8. Odds are not read."""
    if not response.encoding:
        response.encoding = ODDS_ENCODING
    try:
        return response.text or ""
    except Exception:
        return ""


def _body_is_refusal(body: str) -> bool:
    """401/403 pages, captcha, and Cloudflare challenges are not transient."""
    if not body:
        return False
    if looks_like_login_wall(body) or looks_like_forbidden_page(body):
        return True
    return page_block_reason(body) is not None


def _refusal_reason(body: str) -> str | None:
    """A stable reason when the odds body is a refusal. Otherwise null."""
    if not body or not _body_is_refusal(body):
        return None
    if looks_like_login_wall(body):
        return "netkeiba odds requires login"
    reason = page_block_reason(body)
    if reason == "captcha":
        return "netkeiba odds returned a captcha"
    if reason == "challenge":
        return "netkeiba odds returned a challenge"
    if reason == "login":
        return "netkeiba odds requires login"
    return "netkeiba odds returned 403"


def _odds_api_decided(response: requests.Response) -> bool:
    """True when a 400 body is the odds API's own answer, not a transport glitch.

    A decided body names ``status`` or ``reason``. Numbers in it are not quotes
    and the call is not retried.
    """
    payload = _json_object_from_response(response)
    if payload is None:
        return False
    status = payload.get("status")
    if isinstance(status, str) and status.strip():
        return True
    reason = payload.get("reason")
    return isinstance(reason, str) and bool(reason.strip())


def _transient_odds_error(exc: Exception) -> bool:
    """Retry 5xx, timeouts, connection errors, and an undecided odds HTTP 400.

    Never retry 401, 403, a captcha, or a challenge.
    """
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if not isinstance(exc, requests.HTTPError):
        return False
    response = exc.response
    if response is None:
        return True
    body = _response_text(response)
    if _body_is_refusal(body):
        return False
    code = response.status_code
    if code in {401, 403}:
        return False
    if code in _TRANSIENT_HTTP or (isinstance(code, int) and code >= 500):
        return True
    if code == 400:
        return not _odds_api_decided(response)
    return False


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


def _trio(key: object) -> tuple[int, int, int] | None:
    """Read an unordered horse trio from ``010205`` or ``1-2-5``."""
    if not isinstance(key, str):
        return None
    text = key.strip()
    if len(text) == 6 and text.isdigit():
        numbers = [int(text[0:2]), int(text[2:4]), int(text[4:6])]
    else:
        parts = text.split("-")
        if len(parts) != 3 or any(not part.strip().isdigit() for part in parts):
            return None
        numbers = [int(part) for part in parts]
    if len(set(numbers)) != 3:
        return None
    if any(not 1 <= number <= 18 for number in numbers):
        return None
    low, mid, high = sorted(numbers)
    return low, mid, high


def _trio_sort(key: str, trio: tuple[int, int, int]) -> tuple[int, int, int, int, str]:
    """Sort by horses. An already ascending key wins over a permutation."""
    text = key.strip()
    canonical_digits = f"{trio[0]:02d}{trio[1]:02d}{trio[2]:02d}"
    canonical_dashed = f"{trio[0]}-{trio[1]}-{trio[2]}"
    ordered = 0 if text in {canonical_digits, canonical_dashed} else 1
    return (trio[0], trio[1], trio[2], ordered, text)


def _pair_sort(key: str, pair: tuple[int, int]) -> tuple[int, int, int, str]:
    """Sort by horses. An already lower-higher key wins over its reverse."""
    text = key.strip()
    ordered = 1
    if len(text) == 4 and text.isdigit() and (int(text[:2]), int(text[2:])) == pair:
        ordered = 0
    elif text == f"{pair[0]}-{pair[1]}":
        ordered = 0
    return (pair[0], pair[1], ordered, text)

