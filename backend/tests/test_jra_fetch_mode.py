"""JRA fetch_mode uses a browser session without changing the HTML path."""

from datetime import date, datetime

from backend.scrapers.jra_payout import (
    JRA_PAYOUT_FORBIDDEN,
    OfficialPayoutKey,
    OfficialRefund,
    cross_check_payouts,
)
from backend.scrapers.jra_scraper import JRAScraper
from backend.scrapers.netkeiba_scraper import _merge_refunds
from backend.scrapers.page_fallback import (
    HEADLESS_UNAVAILABLE,
    AccessBlocked,
    HeadlessUnavailableError,
)
from tests.test_jra_race_detail import PAGES


class _Inner:
    def __init__(self) -> None:
        self.posts: list[str] = []

    def post(
        self, url: str, data: dict[str, str] | None = None, headers=None, encoding=None
    ) -> str:
        del url, headers, encoding
        cname = (data or {})["cname"]
        self.posts.append(cname)
        return PAGES[cname]

    def close(self) -> None:
        return None


class _Pages:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def post(
        self, url: str, data: dict[str, str] | None = None, headers=None, encoding=None
    ) -> str:
        del url, headers, encoding
        cname = (data or {})["cname"]
        self.calls.append(cname)
        return PAGES[cname]

    def get(self, url: str, params=None, headers=None, encoding=None) -> str:
        del url, params, headers, encoding
        raise AssertionError("headless get was not part of the card walk")


class _Blocked:
    def post(self, url: str, data=None, headers=None, encoding=None) -> str:
        del url, data, headers, encoding
        raise AccessBlocked(403, "returned 403")


class _Missing:
    def post(self, url: str, data=None, headers=None, encoding=None) -> str:
        del url, data, headers, encoding
        raise HeadlessUnavailableError(HEADLESS_UNAVAILABLE)


class _Unused:
    def post(self, url: str, data=None, headers=None, encoding=None) -> str:
        del url, data, headers, encoding
        raise AssertionError("html mode must not open the browser")


def test_headless_card_uses_the_session_and_labels_headless() -> None:
    inner = _Inner()
    session = _Pages()
    scraper = JRAScraper(http_client=inner, fetch_mode="headless", headless_session=session)  # type: ignore[arg-type]

    card = scraper.get_race_card("202606040811")

    assert card is not None
    assert inner.posts == []
    assert session.calls == [
        "pw01dli00/F3",
        "pw01drl00062026040820260926/27",
        "pw01dde0106202604081120260926/2A",
    ]
    assert card.entries[0].source == "headless"
    assert card.race.post_time_source == "headless"
    assert card.warning is None


def test_headless_403_does_not_call_http() -> None:
    inner = _Inner()
    scraper = JRAScraper(http_client=inner, fetch_mode="headless", headless_session=_Blocked())  # type: ignore[arg-type]

    assert scraper.get_race_card("202606040811") is None
    assert inner.posts == []


def test_missing_browser_falls_back_and_warns() -> None:
    inner = _Inner()
    scraper = JRAScraper(http_client=inner, fetch_mode="headless", headless_session=_Missing())  # type: ignore[arg-type]

    card = scraper.get_race_card("202606040811")

    assert card is not None
    assert inner.posts
    assert card.entries[0].source == "jra_html"
    assert card.race.post_time_source == "jra_html"
    assert card.warning == HEADLESS_UNAVAILABLE


def test_html_mode_does_not_call_the_session() -> None:
    inner = _Inner()
    scraper = JRAScraper(http_client=inner, fetch_mode="html", headless_session=_Unused())  # type: ignore[arg-type]

    card = scraper.get_race_card("202606040811")

    assert card is not None
    assert inner.posts
    assert card.entries[0].source == "jra_html"
    assert card.warning is None


def test_odds_cname_prefers_the_race_button_over_the_menu() -> None:
    html = (
        "<a onclick=\"doAction('/JRADB/accessO.html','pw15oli00/6D');\">オッズ</a>"
        "<a onclick=\"doAction('/JRADB/accessO.html','pw151ou1008202604050120261012Z/67');\">"
        "最終</a>"
    )
    single = (
        "<a onclick=\"doAction('/JRADB/accessO.html','pw01ode0105202501010120250105/AB');\">"
        "オッズ</a>"
    )

    assert JRAScraper._odds_cname(html) == "pw151ou1008202604050120261012Z/67"
    assert JRAScraper._odds_cname(single) == "pw01ode0105202501010120250105/AB"


def test_payout_403_from_the_browser_does_not_fall_back() -> None:
    calls: list[str] = []

    class _HTTP:
        def post(self, url: str, data=None, encoding=None, headers=None) -> str:
            del url, data, encoding, headers
            calls.append("http")
            return "<html></html>"

    key = OfficialPayoutKey(
        venue_code="08",
        year=2026,
        kai=4,
        meeting_day=5,
        race_date=date(2026, 10, 12),
        race_number=1,
    )
    note, refunds, source = cross_check_payouts(
        _HTTP(),
        key,
        [],
        fetch_mode="headless",
        session=_Blocked(),
    )

    assert note == JRA_PAYOUT_FORBIDDEN
    assert refunds is None
    assert source == "jra_html"
    assert calls == []


def test_refund_only_headless_rows_keep_that_source() -> None:
    merged, warning = _merge_refunds(
        None,
        [OfficialRefund(horse_numbers=(2,), bet_types=("単勝",), bracket_numbers=None)],
        datetime(2026, 10, 12, 16, 0),
        jra_source="headless",
    )

    assert warning is None
    assert merged is not None
    assert merged[0].source == "headless"
    assert merged[0].horse_numbers == [2]
