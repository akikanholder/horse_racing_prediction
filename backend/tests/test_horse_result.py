"""Horse result page: near-run performances, not the race being predicted."""

from datetime import date, datetime
from pathlib import Path

import pytest
import requests
from bs4 import BeautifulSoup

from backend.cli import _format_performance
from backend.models.horse import HorsePerformance
from backend.scrapers.netkeiba_scraper import (
    HORSE_RESULT_CAPTCHA,
    HORSE_RESULT_EMPTY,
    HORSE_RESULT_FETCH_FAILED,
    HORSE_RESULT_FORBIDDEN,
    HORSE_RESULT_LOGIN,
    HORSE_RESULT_TABLE_MALFORMED,
    HORSE_RESULT_TABLE_NOT_FOUND,
    NetkeibaScraper,
    fewer_recent_runs_warning,
)
from backend.scrapers.page_fallback import HEADLESS_UNAVAILABLE, same_site_horse_result_urls

FIXTURES = Path(__file__).parent / "fixtures"
HORSE_ID = "2019105283"
FETCHED_AT = datetime(2026, 9, 27, 12, 0, 0)
PROFILE_URL = f"https://db.netkeiba.com/horse/{HORSE_ID}/"
RESULT_URL = f"https://db.netkeiba.com/horse/result/{HORSE_ID}/"

DECOY_TABLE = """
<table class="db_h_race_results nk_tb_common">
  <tr><th>日付</th><th>開催</th><th>レース名</th><th>着順</th><th>距離</th></tr>
  <tr>
    <td>1999/01/01</td><td>東京</td><td>デコイレース</td><td>1</td><td>芝1600</td>
  </tr>
</table>
"""


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class _FakeHTTP:
    """Returns fixture HTML. Result URLs are matched before the profile URL."""

    def __init__(
        self,
        profile: str | Exception,
        result: str | Exception,
        routes: dict[str, str | Exception] | None = None,
    ):
        self.profile = profile
        self.result = result
        self.routes = routes or {}
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, headers=None, encoding=None):
        self.calls.append((url, headers or {}))
        if url in self.routes:
            return self._unwrap(self.routes[url])
        if "/horse/result/" in url:
            return self._unwrap(self.result)
        if "/horse/" in url:
            return self._unwrap(self.profile)
        raise AssertionError(url)

    def close(self) -> None:
        return None

    @staticmethod
    def _unwrap(page: str | Exception) -> str:
        if isinstance(page, Exception):
            raise page
        return page


def _forbid_headless(url: str) -> str:
    raise AssertionError(f"headless should not run for {url}")


def _scraper(
    profile: str | Exception,
    result: str | Exception,
    routes: dict[str, str | Exception] | None = None,
    headless: object = _forbid_headless,
) -> tuple[NetkeibaScraper, _FakeHTTP]:
    client = _FakeHTTP(profile, result, routes)
    return NetkeibaScraper(http_client=client, headless_render=headless), client  # type: ignore[arg-type]


def _horse(
    profile: str | Exception,
    result: str | Exception,
    routes: dict[str, str | Exception] | None = None,
    headless: object = _forbid_headless,
):
    scraper, client = _scraper(profile, result, routes, headless)
    return scraper.get_horse_info(HORSE_ID, fetched_at=FETCHED_AT), client


def _parse(html: str) -> tuple[list[HorsePerformance], str | None]:
    scraper = NetkeibaScraper(http_client=_FakeHTTP("", ""))  # type: ignore[arg-type]
    return scraper._parse_horse_performances(BeautifulSoup(html, "lxml"))


def _row(distance: str, going: str, **overrides: str) -> str:
    cells = {
        "日付": "2020/08/01",
        "開催": "1阪神3",
        "天気": "晴",
        "レース名": "3歳未勝利",
        "頭数": "16",
        "馬番": "8",
        "オッズ": "5.4",
        "人気": "3",
        "着順": "2",
        "騎手": "横山武史",
        "斤量": "56",
        "距離": distance,
        "馬場": going,
        "タイム": "1:52.8",
        "着差": "0.3",
        "上り": "37.2",
        "馬体重": "498(-2)",
        "賞金": "200.0",
    }
    cells.update(overrides)
    headers = "".join(f"<th>{name}</th>" for name in cells)
    values = "".join(f"<td>{value}</td>" for value in cells.values())
    return (
        f'<table class="db_h_race_results nk_tb_common"><tr>{headers}</tr><tr>{values}</tr></table>'
    )


def test_result_page_fills_name_career_and_near_runs():
    profile = _load("netkeiba_horse_profile.html") + DECOY_TABLE
    horse, client = _horse(profile, _load("netkeiba_horse_result.html"))

    assert horse is not None
    assert horse.horse_name == "ドウデュース"
    assert horse.english_name == "Do Deuce"
    assert horse.sex == "牡"
    assert horse.coat_color == "鹿毛"
    assert horse.birth_date == date(2019, 5, 7)
    assert horse.birth_year == 2019
    assert horse.trainer_name == "友道康夫"
    assert horse.training_center == "栗東"
    assert horse.owner_name == "キーファーズ"
    assert horse.breeder_name == "ノーザンファーム"
    assert horse.breeder_location == "安平町"
    assert horse.total_races == 16
    assert horse.total_wins == 8
    assert horse.total_earnings == 175347
    assert horse.fetched_at == FETCHED_AT
    assert horse.runs_fetched == 3
    assert horse.login_required_for_more is None
    assert horse.source == "netkeiba_html"
    assert horse.warning == fewer_recent_runs_warning(9)
    assert [perf.race_name for perf in horse.performances] == [
        "有馬記念(GI)",
        "ジャパンC(GI)",
        "宝塚記念(GI)",
    ]
    assert "デコイレース" not in [perf.race_name for perf in horse.performances]

    urls = [url for url, _headers in client.calls]
    assert RESULT_URL in urls
    assert PROFILE_URL in urls
    result_headers = next(headers for url, headers in client.calls if url == RESULT_URL)
    assert "Chrome/140" in result_headers["User-Agent"]
    assert "text/html" in result_headers["Accept"]
    assert result_headers["Referer"] == PROFILE_URL


def test_japan_cup_row_uses_header_names_not_old_indexes():
    horse, _client = _horse(
        _load("netkeiba_horse_profile.html"),
        _load("netkeiba_horse_result.html"),
    )
    assert horse is not None
    japan_cup = horse.performances[1]
    assert japan_cup.race_date == date(2024, 11, 24)
    assert japan_cup.race_id == "202405050812"
    assert japan_cup.venue == "5東京8"
    assert japan_cup.distance == 2400
    assert japan_cup.track_type == "芝"
    assert japan_cup.track_condition == "良"
    assert japan_cup.weather == "晴"
    assert japan_cup.entry_count == 14
    assert japan_cup.horse_number == 3
    assert japan_cup.rank == 1
    assert japan_cup.odds == 2.3
    assert japan_cup.popularity == 1
    assert japan_cup.jockey_name == "武豊"
    assert japan_cup.weight == 58.0
    assert japan_cup.finish_time == "2:25.5"
    assert japan_cup.margin == "0.0"
    assert japan_cup.passing_order == "13-12-12-7"
    assert japan_cup.last_3f == 32.7
    assert japan_cup.horse_weight == 510
    assert japan_cup.horse_weight_diff == 6
    assert japan_cup.prize_money == 50298.2


def test_scratch_and_shifted_columns_do_not_invent_numbers():
    horse, _client = _horse(
        _load("netkeiba_horse_profile.html"),
        _load("netkeiba_horse_result.html"),
    )
    assert horse is not None
    scratch, _japan_cup, takarazuka = horse.performances

    assert scratch.race_date == date(2024, 12, 22)
    assert scratch.race_id == "202406050811"
    assert scratch.venue == "5中山8"
    assert scratch.rank is None
    assert scratch.odds is None
    assert scratch.popularity is None
    assert scratch.finish_time is None
    assert scratch.last_3f is None
    assert scratch.horse_weight is None
    assert scratch.horse_weight_diff is None
    assert scratch.prize_money is None
    assert scratch.distance == 2500
    assert scratch.track_condition == "良"
    assert scratch.jockey_name == "武豊"

    assert takarazuka.track_condition == "重"
    assert takarazuka.finish_time == "2:12.9"
    assert takarazuka.last_3f == 34.6
    assert takarazuka.horse_weight == 508
    assert takarazuka.horse_weight_diff == 0
    assert takarazuka.prize_money is None
    assert takarazuka.weather == "曇"
    assert takarazuka.rank == 6


def test_career_totals_are_not_counted_from_parsed_rows():
    horse, _client = _horse(
        _load("netkeiba_horse_profile.html"),
        _load("netkeiba_horse_result.html"),
    )
    assert horse is not None
    assert len(horse.performances) == 3
    assert horse.total_races == 16
    assert horse.total_wins == 8


def test_empty_page_returns_no_rows_and_a_warning():
    horse, _client = _horse(
        _load("netkeiba_horse_profile.html"),
        _load("netkeiba_horse_result_empty.html"),
    )
    assert horse is not None
    assert horse.performances == []
    assert horse.warning == HORSE_RESULT_TABLE_NOT_FOUND
    assert horse.horse_name == "ドウデュース"
    assert horse.total_races == 16
    assert horse.fetched_at == FETCHED_AT


def test_malformed_rows_do_not_invent_performances():
    horse, _client = _horse(
        _load("netkeiba_horse_profile.html"),
        _load("netkeiba_horse_result_malformed.html"),
    )
    assert horse is not None
    assert horse.performances == []
    assert horse.warning == HORSE_RESULT_EMPTY


def test_table_without_result_headers_is_malformed():
    html = '<table class="db_h_race_results"><tr><td>1</td><td>2.5</td><td>東京</td></tr></table>'
    performances, warning = _parse(html)
    assert performances == []
    assert warning == HORSE_RESULT_TABLE_MALFORMED


def test_result_fetch_failure_keeps_identity_and_warns():
    horse, _client = _horse(
        _load("netkeiba_horse_profile.html"),
        requests.RequestException("timeout"),
    )
    assert horse is not None
    assert horse.performances == []
    assert horse.warning == HORSE_RESULT_FETCH_FAILED
    assert horse.horse_name == "ドウデュース"
    assert horse.total_races == 16
    assert horse.total_wins == 8


def test_profile_failure_uses_result_page_name_and_does_not_count_rows():
    horse, _client = _horse(
        requests.RequestException("timeout"),
        _load("netkeiba_horse_result.html"),
    )
    assert horse is not None
    assert horse.horse_name == "ドウデュース"
    assert horse.sex == "牡"
    assert len(horse.performances) == 3
    assert horse.total_races is None
    assert horse.total_wins is None
    assert horse.total_earnings is None
    assert horse.runs_fetched == 3
    assert horse.source == "netkeiba_html"
    assert horse.warning == fewer_recent_runs_warning(9)


def test_both_fetches_failing_returns_none():
    horse, _client = _horse(
        requests.RequestException("down"),
        requests.RequestException("down"),
    )
    assert horse is None


def test_unrelated_html_returns_none():
    horse, _client = _horse("<html><body>nope</body></html>", "<html><body>nope</body></html>")
    assert horse is None


def test_missing_career_text_is_not_replaced_by_row_count():
    profile = """
    <div class="horse_title"><h1>無名</h1><p class="txt_01">牡</p></div>
    <table class="db_prof_table">
      <tr><th>通算成績</th><td>-</td></tr>
      <tr><th>獲得賞金 (中央)</th><td>-</td></tr>
    </table>
    """
    horse, _client = _horse(profile, _row("芝1600", "良"))
    assert horse is not None
    assert len(horse.performances) == 1
    assert horse.runs_fetched == 1
    assert horse.warning == fewer_recent_runs_warning(9)
    assert horse.total_races is None
    assert horse.total_wins is None
    assert horse.total_earnings is None


def test_get_horse_performances_reads_the_result_page():
    scraper, client = _scraper(
        _load("netkeiba_horse_profile.html"),
        _load("netkeiba_horse_result.html"),
    )
    fetched = scraper.get_horse_performances(HORSE_ID, limit=1, fetched_at=FETCHED_AT)
    assert fetched.runs_fetched == 1
    assert fetched.warning is None
    assert fetched.login_required_for_more is None
    assert fetched.source == "netkeiba_html"
    assert fetched.fetched_at == FETCHED_AT
    assert fetched.performances[0].race_name == "有馬記念(GI)"
    assert client.calls[0][0] == RESULT_URL


@pytest.mark.parametrize(
    ("distance", "going", "expected_distance", "expected_surface", "expected_going"),
    [
        ("芝2400", "良", 2400, "芝", "良"),
        ("ダ1800", "稍重", 1800, "ダート", "稍重"),
        ("ダート1200", "稍", 1200, "ダート", "稍重"),
        ("障3000", "不", 3000, "障害", "不良"),
        ("芝右2000", "不良", 2000, "芝", "不良"),
        ("", "", None, None, None),
        ("**", "**", None, None, None),
    ],
)
def test_surface_and_going(distance, going, expected_distance, expected_surface, expected_going):
    performances, warning = _parse(_row(distance, going))
    assert warning is None
    assert len(performances) == 1
    assert performances[0].distance == expected_distance
    assert performances[0].track_type == expected_surface
    assert performances[0].track_condition == expected_going


def test_blood_table_still_fills_sire_and_dam_when_present():
    profile = """
    <div class="horse_title"><h1>血統馬</h1><p class="txt_01">牝 栗毛</p></div>
    <table class="blood"><tr><td><a>キタサンブラック</a><a>マルシェローラ</a></td></tr></table>
    """
    horse, _client = _horse(profile, _load("netkeiba_horse_result_empty.html"))
    assert horse is not None
    assert horse.horse_name == "血統馬"
    assert horse.sex == "牝"
    assert horse.coat_color == "栗毛"
    assert horse.sire_name == "キタサンブラック"
    assert horse.dam_name == "マルシェローラ"
    assert horse.performances == []
    assert horse.warning == HORSE_RESULT_TABLE_NOT_FOUND


def test_text_view_omits_missing_distance():
    perf = HorsePerformance(
        race_date=date(2024, 11, 24),
        race_name="ジャパンC(GI)",
        venue="5東京8",
        distance=2400,
        track_type="芝",
        track_condition="良",
        rank=1,
    )
    assert _format_performance(perf) == "2024-11-24 5東京8 ジャパンC(GI) (芝2400m 良)"


@pytest.mark.integration
def test_live_do_deuce_result_page():
    """Live check. Do Deuce's profile page has no results table; the result page does."""
    with NetkeibaScraper() as scraper:
        horse = scraper.get_horse_info(HORSE_ID)

    assert horse is not None
    assert horse.horse_name == "ドウデュース"
    assert horse.sex == "牡"
    assert horse.total_races == 16
    assert horse.total_wins == 8
    assert horse.total_earnings == 175347
    assert horse.fetched_at is not None
    assert horse.runs_fetched == len(horse.performances)
    assert horse.login_required_for_more in {True, False, None}
    if horse.runs_fetched >= 9:
        assert horse.warning is None
    else:
        assert horse.warning is not None
        assert "fewer than 9 recent runs" in horse.warning
    assert horse.performances
    japan_cup = next(perf for perf in horse.performances if perf.race_id == "202405050812")
    assert japan_cup.race_name == "ジャパンC(GI)"
    assert japan_cup.rank == 1
    assert japan_cup.distance == 2400
    assert japan_cup.track_type == "芝"
    assert japan_cup.track_condition == "良"
    assert japan_cup.finish_time == "2:25.5"
    assert japan_cup.last_3f == 32.7
    assert japan_cup.odds == 2.3


AJAX_URL = "https://db.netkeiba.com/horse/ajax_result.html?horse_id=2019105283"
SHORT = fewer_recent_runs_warning(9)


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    error = requests.HTTPError(f"http {status}")
    error.response = response
    return error


def test_visible_runs_stop_when_the_page_says_more_need_login():
    horse, client = _horse(
        _load("netkeiba_horse_profile.html"),
        _load("netkeiba_horse_result_login_more.html"),
    )
    assert horse is not None
    assert [perf.race_name for perf in horse.performances] == [
        "有馬記念(GI)",
        "ジャパンC(GI)",
        "宝塚記念(GI)",
        "大阪杯(GI)",
    ]
    assert horse.runs_fetched == 4
    assert horse.login_required_for_more is True
    assert horse.source == "netkeiba_html"
    assert horse.fetched_at == FETCHED_AT
    assert horse.warning == SHORT
    japan_cup = horse.performances[1]
    assert japan_cup.finish_time == "2:25.5"
    assert japan_cup.last_3f == 32.7
    assert japan_cup.prize_money == 50298.2
    urls = [url for url, _headers in client.calls]
    assert AJAX_URL not in urls
    assert all("login" not in url for url in urls)


def test_embedded_json_adds_only_public_runs_already_on_the_page():
    horse, client = _horse(
        _load("netkeiba_horse_profile.html"),
        _load("netkeiba_horse_result_embedded.html"),
    )
    assert horse is not None
    assert horse.runs_fetched == 9
    assert horse.warning is None
    assert horse.login_required_for_more is False
    assert horse.source == "netkeiba_html+netkeiba_js"
    assert horse.fetched_at == FETCHED_AT
    assert [perf.race_id for perf in horse.performances].count("202406050811") == 1
    assert horse.performances[0].rank is None
    japan_cup = next(perf for perf in horse.performances if perf.race_id == "202305050812")
    assert japan_cup.race_name == "ジャパンC(GI)"
    assert japan_cup.venue == "5東京8"
    assert japan_cup.distance == 2400
    assert japan_cup.track_type == "芝"
    assert japan_cup.track_condition == "稍重"
    assert japan_cup.weather == "晴"
    assert japan_cup.entry_count == 18
    assert japan_cup.horse_number == 7
    assert japan_cup.rank == 1
    assert japan_cup.odds == 2.1
    assert japan_cup.popularity == 1
    assert japan_cup.jockey_name == "武豊"
    assert japan_cup.weight == 58.0
    assert japan_cup.finish_time == "2:21.0"
    assert japan_cup.margin == "0.1"
    assert japan_cup.passing_order == "2-2-2"
    assert japan_cup.last_3f == 33.1
    assert japan_cup.horse_weight == 504
    assert japan_cup.horse_weight_diff == 2
    assert japan_cup.prize_money == 50298.2
    urls = [url for url, _headers in client.calls]
    assert urls[:2] == [PROFILE_URL, RESULT_URL]
    assert AJAX_URL not in urls


def test_same_site_url_the_page_calls_fills_the_public_runs():
    horse, client = _horse(
        _load("netkeiba_horse_profile.html"),
        _load("netkeiba_horse_result_ajax.html"),
        routes={AJAX_URL: _load("netkeiba_horse_result_ajax.json")},
    )
    assert horse is not None
    assert horse.runs_fetched == 9
    assert horse.warning is None
    assert horse.login_required_for_more is None
    assert horse.source == "netkeiba_html+netkeiba_js"
    urls = [url for url, _headers in client.calls]
    assert AJAX_URL in urls
    assert all("login" not in url and "regist.netkeiba.com" not in url for url in urls)


def test_login_flag_in_json_does_not_request_the_locked_url():
    horse, client = _horse(
        _load("netkeiba_horse_profile.html"),
        _load("netkeiba_horse_result_login_json.html"),
        routes={AJAX_URL: _load("netkeiba_horse_result_ajax.json")},
    )
    assert horse is not None
    assert horse.runs_fetched == 2
    assert horse.login_required_for_more is True
    assert horse.warning == SHORT
    assert horse.source == "netkeiba_html"
    assert AJAX_URL not in [url for url, _headers in client.calls]


def test_later_login_wall_keeps_the_rows_already_visible():
    horse, _client = _horse(
        _load("netkeiba_horse_profile.html"),
        _load("netkeiba_horse_result_ajax.html"),
        routes={AJAX_URL: _load("netkeiba_horse_result_login_wall.html")},
    )
    assert horse is not None
    assert horse.runs_fetched == 2
    assert [perf.race_name for perf in horse.performances] == ["有馬記念(GI)", "ジャパンC(GI)"]
    assert horse.login_required_for_more is True
    assert horse.source == "netkeiba_html"
    assert horse.warning == SHORT


def test_later_403_keeps_visible_rows_and_does_not_call_them_login():
    horse, _client = _horse(
        _load("netkeiba_horse_profile.html"),
        _load("netkeiba_horse_result_ajax.html"),
        routes={AJAX_URL: _http_error(403)},
    )
    assert horse is not None
    assert horse.runs_fetched == 2
    assert horse.login_required_for_more is None
    assert horse.source == "netkeiba_html"
    assert horse.warning == f"{SHORT}; {HORSE_RESULT_FORBIDDEN}"


def test_result_403_returns_no_runs_and_does_not_try_a_fallback():
    horse, client = _horse(
        _load("netkeiba_horse_profile.html"),
        _http_error(403),
        routes={AJAX_URL: _load("netkeiba_horse_result_ajax.json")},
    )
    assert horse is not None
    assert horse.horse_name == "ドウデュース"
    assert horse.performances == []
    assert horse.runs_fetched == 0
    assert horse.source is None
    assert horse.login_required_for_more is None
    assert horse.warning == HORSE_RESULT_FORBIDDEN
    urls = [url for url, _headers in client.calls]
    assert urls[:2] == [PROFILE_URL, RESULT_URL]
    assert AJAX_URL not in urls


def test_login_wall_stops_before_the_result_url_the_page_calls():
    horse, client = _horse(
        _load("netkeiba_horse_profile.html"),
        _load("netkeiba_horse_result_login_wall.html"),
        routes={AJAX_URL: _load("netkeiba_horse_result_ajax.json")},
    )
    assert horse is not None
    assert horse.performances == []
    assert horse.runs_fetched == 0
    assert horse.source is None
    assert horse.login_required_for_more is True
    assert horse.warning == HORSE_RESULT_LOGIN
    assert AJAX_URL not in [url for url, _headers in client.calls]


def test_captcha_wall_is_not_parsed_or_followed():
    horse, client = _horse(
        _load("netkeiba_horse_profile.html"),
        _load("netkeiba_horse_result_captcha.html"),
        routes={AJAX_URL: _load("netkeiba_horse_result_ajax.json")},
    )
    assert horse is not None
    assert horse.performances == []
    assert horse.runs_fetched == 0
    assert horse.login_required_for_more is None
    assert horse.warning == HORSE_RESULT_CAPTCHA
    assert AJAX_URL not in [url for url, _headers in client.calls]


def test_explicit_complete_list_is_false_even_when_shorter_than_nine():
    horse, _client = _horse(
        requests.RequestException("profile down"),
        _load("netkeiba_horse_result_complete.html"),
    )
    assert horse is not None
    assert horse.horse_name == "少走馬"
    assert horse.runs_fetched == 2
    assert horse.login_required_for_more is False
    assert horse.source == "netkeiba_html"
    assert horse.warning == SHORT
    assert horse.performances[1].track_type == "ダート"
    assert horse.performances[1].track_condition == "稍重"


def test_header_login_link_is_not_a_request_for_more_runs():
    horse, _client = _horse(
        _load("netkeiba_horse_profile.html"),
        _load("netkeiba_horse_result_nine.html"),
    )
    assert horse is not None
    assert horse.runs_fetched == 9
    assert horse.login_required_for_more is None
    assert horse.source == "netkeiba_html"
    assert horse.warning is None
    assert horse.performances[-1].track_condition == "稍重"


def test_requested_five_does_not_warn_when_nine_are_public():
    scraper, _client = _scraper(
        _load("netkeiba_horse_profile.html"),
        _load("netkeiba_horse_result_nine.html"),
    )
    fetched = scraper.get_horse_performances(HORSE_ID, limit=5, fetched_at=FETCHED_AT)
    assert fetched.runs_fetched == 5
    assert [perf.race_name for perf in fetched.performances] == [
        "九月",
        "八月",
        "七月",
        "六月",
        "五月",
    ]
    assert fetched.warning is None
    assert fetched.login_required_for_more is None
    assert fetched.source == "netkeiba_html"
    assert fetched.fetched_at == FETCHED_AT


def test_headless_render_fills_a_shell_that_has_not_loaded():
    seen: list[str] = []

    def _render(url: str) -> str:
        seen.append(url)
        return _load("netkeiba_horse_result.html")

    horse, _client = _horse(
        _load("netkeiba_horse_profile.html"),
        _load("netkeiba_horse_result_shell.html"),
        headless=_render,
    )
    assert seen == [RESULT_URL]
    assert horse is not None
    assert horse.horse_name == "ドウデュース"
    assert horse.runs_fetched == 3
    assert horse.source == "headless"
    assert horse.login_required_for_more is None
    assert horse.warning == SHORT
    assert horse.performances[1].finish_time == "2:25.5"
    assert horse.performances[1].last_3f == 32.7


def test_missing_headless_browser_warns_and_keeps_the_shell_empty(monkeypatch):
    monkeypatch.setattr(
        "backend.scrapers.page_fallback.headless_browser_installed",
        lambda: False,
    )
    horse, _client = _horse(
        _load("netkeiba_horse_profile.html"),
        _load("netkeiba_horse_result_shell.html"),
        headless="auto",
    )
    assert horse is not None
    assert horse.performances == []
    assert horse.runs_fetched == 0
    assert horse.source is None
    assert horse.warning == f"{HORSE_RESULT_TABLE_NOT_FOUND}; {HEADLESS_UNAVAILABLE}"


def test_netkeiba_credentials_in_the_environment_are_not_sent(monkeypatch):
    monkeypatch.setenv("NETKEIBA_LOGIN_ID", "someone")
    monkeypatch.setenv("NETKEIBA_PASSWORD", "secret")
    monkeypatch.setenv("NETKEIBA_COOKIE", "session=abc")
    horse, client = _horse(
        _load("netkeiba_horse_profile.html"),
        _load("netkeiba_horse_result.html"),
    )
    assert horse is not None
    headers = next(headers for url, headers in client.calls if url == RESULT_URL)
    assert "Cookie" not in headers
    assert "Authorization" not in headers
    joined = " ".join(headers.values())
    assert "someone" not in joined
    assert "secret" not in joined
    assert "session=abc" not in joined


def test_horse_result_url_finder_skips_login_and_other_hosts():
    urls = same_site_horse_result_urls(
        _load("netkeiba_horse_result_ajax.html"),
        RESULT_URL,
    )
    assert urls == [AJAX_URL]


def test_performance_rows_do_not_grow_a_source_field():
    assert "source" not in HorsePerformance.model_fields
    assert "fetched_at" not in HorsePerformance.model_fields


def test_race_list_copies_the_public_run_metadata():
    from datetime import date as date_cls

    from backend.models.horse import Pedigree, PedigreeHorse
    from backend.models.race import Race, RaceCard, RaceEntry
    from backend.models.running_style import RunningStyle
    from backend.models.training import TRAINING_NOT_ON_KEIBABOOK, unavailable_training
    from backend.scrapers.day_of_bias import TRACK_BIAS_NOT_PUBLISHED, DayOfBiasPublication
    from backend.scrapers.netkeiba_scraper import PublicHorseRuns
    from backend.services.race_service import RaceService

    performances = [
        HorsePerformance(
            race_date=date_cls(2024, 1, 1),
            race_name="未勝利",
            venue="中山",
            passing_order="1-1",
            entry_count=12,
            distance=1200,
            track_type="芝",
        )
    ]

    class _Scraper:
        def get_race_card(self, race_id: str) -> RaceCard:
            return RaceCard(
                race=Race(
                    race_id=race_id,
                    race_name="テスト",
                    race_number=1,
                    race_date=date_cls(2026, 9, 28),
                    venue="中山",
                    distance=1200,
                    track_type="芝",
                ),
                entries=[
                    RaceEntry(
                        bracket_number=1,
                        horse_number=1,
                        horse_name="エイ",
                        horse_id="h1",
                        sex="牡",
                        age=4,
                    )
                ],
            )

        def get_horse_performances(self, horse_id: str, limit: int = 9):
            assert horse_id == "h1"
            assert limit == 5
            return PublicHorseRuns(
                performances=performances,
                runs_fetched=1,
                login_required_for_more=True,
                source="netkeiba_html",
                fetched_at=FETCHED_AT,
                warning=fewer_recent_runs_warning(5),
            )

        def get_pedigree(self, horse_id: str) -> Pedigree:
            return Pedigree(
                sire=PedigreeHorse(name="父", horse_id="s1"),
                dam=PedigreeHorse(name="母", horse_id="d1"),
                source="netkeiba_ped",
            )

    class _Training:
        def training_for_race(self, race_id: str, race_name: str, horse_names: list[str]):
            return {name: unavailable_training(TRAINING_NOT_ON_KEIBABOOK) for name in horse_names}

        def close(self) -> None:
            return None

    class _Bias:
        def publication_for(self, race: Race) -> DayOfBiasPublication:
            return DayOfBiasPublication(
                inside_outside=None,
                pace_bias=None,
                source=None,
                warning=TRACK_BIAS_NOT_PUBLISHED,
                data_sufficiency="unavailable",
            )

    service = RaceService()
    service._netkeiba_scraper = _Scraper()  # type: ignore[assignment]
    service._training_scraper = _Training()  # type: ignore[assignment]
    service._day_of_bias = _Bias()  # type: ignore[assignment]
    horses = service.get_horses_for_race("202609280601")
    assert len(horses) == 1
    horse = horses[0]
    assert horse.runs_fetched == 1
    assert horse.login_required_for_more is True
    assert horse.source == "netkeiba_html"
    assert horse.fetched_at == FETCHED_AT
    assert horse.warning == fewer_recent_runs_warning(5)
    assert len(horse.recent_performances) == 1
    style = horse.running_style
    assert isinstance(style, RunningStyle)
    assert style.sample_size == 1
