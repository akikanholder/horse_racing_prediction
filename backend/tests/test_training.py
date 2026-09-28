"""調教。ページに無い時計は null。オッズとレース名からは作らない。"""

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
import requests

from backend.models.training import (
    SOURCE_KEIBABOOK_CRIREKI,
    SOURCE_KEIBABOOK_SMART,
    SOURCE_SPEEDKEIBA,
    TRAINING_FETCH_FAILED,
    TRAINING_HISTORY_EMPTY,
    TRAINING_HISTORY_REQUIRES_LOGIN,
    TRAINING_HORSE_UNAVAILABLE,
    TRAINING_LOGIN_FAILED,
    TRAINING_NOT_ON_EITHER,
    TRAINING_NOT_ON_KEIBABOOK,
    TRAINING_TABLE_MALFORMED,
    TRAINING_TABLE_NOT_FOUND,
    TRAINING_THIN,
    TRAINING_UMACD_UNMAPPED,
    unavailable_training,
)
from backend.scrapers.day_of_bias import not_loaded_publication
from backend.scrapers.training_scraper import (
    ENV_KEIBABOOK_COOKIE,
    ENV_KEIBABOOK_LOGIN_ID,
    ENV_KEIBABOOK_PASSWORD,
    KEIBABOOK_CRIREKI,
    KEIBABOOK_CYOKYO,
    KEIBABOOK_HORSE_SEARCH,
    KEIBABOOK_LOGIN,
    KEIBABOOK_LOGINCHECK,
    KEIBABOOK_SYUTUBA,
    SPEEDKEIBA_INDEX,
    KeibabookCredentials,
    TrainingScraper,
    UmacdCandidate,
    credentials_from_env,
    logincheck_authenticated,
    match_speedkeiba_article,
    parse_keibabook_crireki,
    parse_keibabook_cyokyo,
    parse_keibabook_horse_search,
    parse_keibabook_syutuba,
    parse_speedkeiba_article,
    parse_speedkeiba_index,
    select_umacd,
)
from backend.services.race_service import RaceService


class _OfflineBias:
    """Keep the training service test off the accessD network."""

    def publication_for(self, race):
        return not_loaded_publication()


FIXTURES = Path(__file__).parent / "fixtures"
RACE_ID = "202606050101"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _dump(training) -> str:
    return json.dumps(training.model_dump(mode="json"), ensure_ascii=False)


class _Pages:
    def __init__(self, pages: dict[str, str | Exception]):
        self.pages = pages
        self.calls: list[str] = []

    def get(self, url, params=None, headers=None, encoding=None):
        self.calls.append(url)
        if url not in self.pages:
            raise AssertionError(url)
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        return page

    def close(self) -> None:
        return None


def test_keibabook_page_reads_clocks_rank_and_leaves_symbols_null():
    page = parse_keibabook_cyokyo(_load("keibabook_cyokyo.html"))
    assert page.page_warning is None
    horse = page.by_name["サルヴァトーレ"]
    assert horse.source == SOURCE_KEIBABOOK_SMART
    assert horse.data_sufficiency == "sufficient"
    assert horse.warning is None
    assert horse.summary is not None
    assert horse.summary.comment == "乗り込み入念"
    assert horse.summary.arrow == "→"
    assert horse.summary.average is None

    symbol, dated, gapped, slope, final = horse.workouts
    assert symbol.date is None
    assert symbol.date_label == "■"
    assert symbol.long_gap is None
    assert symbol.course == "美Ｗ"
    assert symbol.condition == "良"
    assert symbol.distance == "5F"
    assert symbol.time == "66.6"
    assert symbol.rank_in_group == 8
    assert [split.label for split in symbol.splits] == ["5F", "半哩", "3F", "1F"]

    assert dated.date == date(2026, 5, 29)
    assert dated.long_gap is False
    assert dated.partner_note == "リスペクトライト（三歳未勝）末強めの内0.9秒追走0.4秒先着"
    assert dated.time == "66.9"
    assert "0.9" not in dated.time
    assert all(split.time != "0.9" for split in dated.splits)

    assert gapped.date == date(2025, 12, 1)
    assert gapped.long_gap is True
    assert gapped.condition == "重"

    assert slope.course == "美坂"
    assert slope.distance == "4F"
    assert slope.time == "59.3"
    assert slope.slope_reps == 1
    assert slope.rank_in_group is None
    assert [split.label for split in slope.splits] == ["4F", "3F", "2F", "1F"]
    assert slope.date == date(2026, 5, 31)

    assert final.date == date(2026, 6, 3)
    assert final.condition == "不"
    assert final.mark == "☆"
    assert final.final_workout is True
    assert final.rank_in_group == 8

    other = page.by_name["エイ"]
    assert other.workouts[0].distance == "6F"
    assert other.workouts[0].time == "83.2"
    dumped = _dump(horse) + _dump(other)
    assert "99.9" not in dumped
    assert "人気" not in dumped
    assert "キタサンブラック記念" not in dumped


def test_absent_keibabook_page_does_not_use_the_race_name_or_odds():
    page = parse_keibabook_cyokyo(_load("keibabook_cyokyo_absent.html"))
    assert page.by_name == {}
    assert page.page_warning == TRAINING_TABLE_NOT_FOUND


def test_malformed_keibabook_row_is_not_parsed():
    page = parse_keibabook_cyokyo(_load("keibabook_cyokyo_malformed.html"))
    assert page.page_warning is None
    horse = page.by_name["壊れ馬"]
    assert horse.workouts == []
    assert horse.warning == TRAINING_TABLE_MALFORMED
    assert horse.data_sufficiency == "unavailable"
    dumped = _dump(horse)
    assert "66.6" not in dumped
    assert "12.3" not in dumped


def test_speedkeiba_article_keeps_published_rows_and_drops_popularity_laps():
    page = parse_speedkeiba_article(_load("speedkeiba_article.html"))
    assert page.page_warning is None
    horse = page.by_name["ビー"]
    assert horse.source == SOURCE_SPEEDKEIBA
    assert horse.data_sufficiency == "sufficient"
    assert horse.warning is None
    previous, early, latest = horse.workouts
    assert previous.section == "前回連対時"
    assert previous.date is None
    assert previous.date_label == "3/22 (日)"
    assert previous.course == "美浦 W"
    assert previous.condition == "良"
    assert previous.distance is None
    assert previous.time == "55.9 – 40.2 – 13.5"
    assert early.date == date(2026, 9, 20)
    assert early.condition == "稍重"
    assert latest.date == date(2026, 9, 23)
    assert latest.course == "栗東 坂"
    assert latest.section == "今回"
    assert horse.summary is not None
    assert horse.summary.evaluation == "○ : 坂路で加速ラップ"
    assert horse.summary.average is not None
    assert horse.summary.average.label == "(平均)"
    assert horse.summary.average.course == "美浦 W"
    assert horse.summary.average.time == "52.8 – 38.0 – 11.4"
    assert len(horse.summary.laps) == 1
    lap = horse.summary.laps[0]
    assert lap.label == "今走"
    assert lap.phase == "本追切"
    assert lap.course == "坂路"
    assert [(split.label, split.time) for split in lap.splits] == [
        ("4F", "16.5"),
        ("3F", "15.2"),
        ("2F", "13.6"),
        ("1F", "12.4"),
    ]
    dumped = _dump(horse)
    assert "17.3" not in dumped
    assert "16.8" not in dumped
    assert "12.9" not in dumped
    assert "99.9" not in dumped
    assert "人気" not in dumped
    assert "キタサンブラック記念" not in dumped
    assert "odds" not in horse.model_dump()
    assert "popularity" not in horse.model_dump()


def test_absent_speedkeiba_article_has_no_horses():
    page = parse_speedkeiba_article(_load("speedkeiba_article_absent.html"))
    assert page.by_name == {}
    assert page.page_warning == TRAINING_TABLE_NOT_FOUND


def test_malformed_speedkeiba_time_drops_the_other_row_too():
    page = parse_speedkeiba_article(_load("speedkeiba_article_malformed.html"))
    horse = page.by_name["壊れ馬"]
    assert horse.workouts == []
    assert horse.summary is None
    assert horse.warning == TRAINING_TABLE_MALFORMED
    assert "51.9" not in _dump(horse)
    assert "2.5" not in _dump(horse)


def test_speedkeiba_index_matches_one_graded_race_title():
    articles = parse_speedkeiba_index(_load("speedkeiba_index.html"))
    assert articles == [
        ("https://speedkeiba.com/week/sprinters", "スプリンターズステークス 2026 調教タイム"),
        ("https://speedkeiba.com/week/sirius", "シリウスステークス 2026 調教タイム"),
    ]
    assert match_speedkeiba_article("スプリンターズＳ", articles) == (
        "https://speedkeiba.com/week/sprinters"
    )
    assert match_speedkeiba_article("未勝利", articles) is None
    assert match_speedkeiba_article("S", articles) is None
    assert match_speedkeiba_article("キタサンブラック記念", articles) is None


def test_race_training_prefers_keibabook_and_does_not_invent_the_missing_horse():
    client = _Pages(
        {
            KEIBABOOK_CYOKYO.format(race_id=RACE_ID): _load("keibabook_cyokyo.html"),
            SPEEDKEIBA_INDEX: _load("speedkeiba_index.html"),
            "https://speedkeiba.com/week/sprinters": _load("speedkeiba_article.html"),
        }
    )
    scraper = TrainingScraper(http_client=client)  # type: ignore[arg-type]
    chosen = scraper.training_for_race(
        RACE_ID,
        "スプリンターズＳ",
        ["サルヴァトーレ", "エイ", "ビー", "シー"],
    )
    assert chosen["サルヴァトーレ"].source == SOURCE_KEIBABOOK_SMART
    assert chosen["サルヴァトーレ"].workouts[0].time == "66.6"
    assert chosen["エイ"].source == SOURCE_KEIBABOOK_SMART
    assert chosen["エイ"].workouts[0].time == "83.2"
    assert "11.1" not in _dump(chosen["エイ"])
    assert chosen["ビー"].source == SOURCE_SPEEDKEIBA
    assert chosen["ビー"].workouts[-1].time == "51.9 – 37.1 – 11.5"
    assert "17.3" not in _dump(chosen["ビー"])
    missing = chosen["シー"]
    assert missing.workouts == []
    assert missing.source is None
    assert missing.warning == TRAINING_NOT_ON_EITHER
    assert "99.9" not in _dump(missing)


def test_maiden_race_does_not_open_a_speedkeiba_article():
    client = _Pages(
        {
            KEIBABOOK_CYOKYO.format(race_id=RACE_ID): _load("keibabook_cyokyo_absent.html"),
            SPEEDKEIBA_INDEX: _load("speedkeiba_index.html"),
        }
    )
    scraper = TrainingScraper(http_client=client)  # type: ignore[arg-type]
    chosen = scraper.training_for_race(RACE_ID, "3歳未勝利", ["エイ"])
    assert chosen["エイ"].warning == TRAINING_TABLE_NOT_FOUND
    assert chosen["エイ"].workouts == []
    assert all("week/" not in url for url in client.calls)


def test_bad_race_id_does_not_fetch_keibabook():
    client = _Pages({SPEEDKEIBA_INDEX: _load("speedkeiba_index.html")})
    scraper = TrainingScraper(http_client=client)  # type: ignore[arg-type]
    chosen = scraper.training_for_race("not-a-race", "未勝利", ["エイ"])
    assert chosen["エイ"].warning == TRAINING_NOT_ON_KEIBABOOK
    assert client.calls == [SPEEDKEIBA_INDEX]


def test_keibabook_fetch_failure_stays_empty():
    client = _Pages(
        {
            KEIBABOOK_CYOKYO.format(race_id=RACE_ID): requests.RequestException("timeout"),
            SPEEDKEIBA_INDEX: _load("speedkeiba_index.html"),
        }
    )
    scraper = TrainingScraper(http_client=client)  # type: ignore[arg-type]
    chosen = scraper.training_for_race(RACE_ID, "3歳未勝利", ["エイ"])
    assert chosen["エイ"].warning == TRAINING_FETCH_FAILED
    assert chosen["エイ"].workouts == []


def test_horse_training_uses_one_speedkeiba_article():
    client = _Pages(
        {
            SPEEDKEIBA_INDEX: _load("speedkeiba_index.html"),
            "https://speedkeiba.com/week/sprinters": _load("speedkeiba_article.html"),
            "https://speedkeiba.com/week/sirius": _load("speedkeiba_article_absent.html"),
        }
    )
    scraper = TrainingScraper(http_client=client)  # type: ignore[arg-type]
    training = scraper.training_for_horse("ビー")
    assert training.source == SOURCE_SPEEDKEIBA
    assert training.workouts[0].course == "美浦 W"
    assert training.data_sufficiency == "sufficient"
    missed = scraper.training_for_horse("シー")
    assert missed.warning == TRAINING_HORSE_UNAVAILABLE
    assert missed.workouts == []
    assert "login" in missed.warning


def test_horse_on_two_articles_is_not_merged():
    index = """
    <h3><a href="https://speedkeiba.com/week/a">スプリンターズステークス 2026 調教タイム</a></h3>
    <h3><a href="https://speedkeiba.com/week/b">シリウスステークス 2026 調教タイム</a></h3>
    """
    client = _Pages(
        {
            SPEEDKEIBA_INDEX: index,
            "https://speedkeiba.com/week/a": _load("speedkeiba_article.html"),
            "https://speedkeiba.com/week/b": _load("speedkeiba_article.html"),
        }
    )
    scraper = TrainingScraper(http_client=client)  # type: ignore[arg-type]
    training = scraper.training_for_horse("ビー")
    assert training.workouts == []
    assert training.warning == "training ambiguous on speedkeiba"
    assert training.source is None


def test_speedkeiba_index_failure_on_a_horse_is_fetch_failed():
    client = _Pages({SPEEDKEIBA_INDEX: requests.RequestException("down")})
    scraper = TrainingScraper(http_client=client)  # type: ignore[arg-type]
    training = scraper.training_for_horse("ビー")
    assert training.warning == TRAINING_FETCH_FAILED
    assert training.workouts == []


def test_one_dated_workout_is_thin():
    page = parse_speedkeiba_article(_load("speedkeiba_article.html"))
    horse = page.by_name["エイ"]
    assert len(horse.workouts) == 1
    assert horse.data_sufficiency == "thin"
    assert horse.warning == TRAINING_THIN
    assert horse.workouts[0].time == "11.1 – 11.1 – 11.1"


def test_race_service_attaches_training_without_using_the_race_title():
    class _Netkeiba:
        def get_race_card(self, race_id: str):
            from datetime import date as date_cls

            from backend.models.race import Race, RaceCard, RaceEntry

            assert race_id == RACE_ID
            return RaceCard(
                race=Race(
                    race_id=race_id,
                    race_name="スプリンターズＳ",
                    race_number=11,
                    race_date=date_cls(2026, 9, 27),
                    venue="中山",
                    distance=1200,
                    track_type="芝",
                ),
                entries=[
                    RaceEntry(
                        bracket_number=1,
                        horse_number=1,
                        horse_name="ビー",
                        horse_id="h-bee",
                        sex="牝",
                        age=5,
                    )
                ],
            )

        def get_horse_performances(self, horse_id: str, limit: int = 10):
            return []

        def get_pedigree(self, horse_id: str):
            from backend.models.horse import Pedigree

            return Pedigree()

        def close(self) -> None:
            return None

    class _Training:
        def training_for_race(self, race_id: str, race_name: str, horse_names: list[str]):
            assert race_name == "スプリンターズＳ"
            assert horse_names == ["ビー"]
            page = parse_speedkeiba_article(_load("speedkeiba_article.html"))
            return {"ビー": page.by_name["ビー"]}

        def close(self) -> None:
            return None

    service = RaceService()
    service._netkeiba_scraper = _Netkeiba()  # type: ignore[assignment]
    service._training_scraper = _Training()  # type: ignore[assignment]
    service._day_of_bias = _OfflineBias()  # type: ignore[assignment]
    horses = service.get_horses_for_race(RACE_ID)
    assert len(horses) == 1
    training = horses[0].training
    assert training is not None
    assert training.source == SOURCE_SPEEDKEIBA
    assert training.workouts[-1].course == "栗東 坂"
    assert "スプリンターズ" not in _dump(training)


def test_cli_help_mentions_training_and_login():
    completed = subprocess.run(
        [sys.executable, "-m", "backend.cli", "horse", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "調教" in completed.stdout
    assert "ログイン" in completed.stdout
    assert "KEIBABOOK_LOGIN_ID" in completed.stdout
    assert "KEIBABOOK_PASSWORD" in completed.stdout
    assert "KEIBABOOK_COOKIE" in completed.stdout


_MEMBER = KeibabookCredentials(login_id="member", password="secret")


class _Web:
    """GET and POST keyed separately. Unknown URLs fail the test."""

    def __init__(self, pages: dict[tuple[str, str], str | Exception]):
        self.pages = pages
        self.calls: list[tuple[str, str]] = []
        self.posted: list[tuple[str, dict | None]] = []
        self.headers: dict[str, str] = {}

    def get(self, url, params=None, headers=None, encoding=None):
        self.calls.append(("GET", url))
        return self._body(("GET", url))

    def post(self, url, data=None, headers=None, encoding=None):
        self.calls.append(("POST", url))
        self.posted.append((url, data))
        return self._body(("POST", url))

    def update_headers(self, headers):
        self.headers.update(headers)

    def close(self) -> None:
        return None

    def _body(self, key: tuple[str, str]):
        if key not in self.pages:
            raise AssertionError(key)
        page = self.pages[key]
        if isinstance(page, Exception):
            raise page
        return page


def _member_pages(extra: dict[tuple[str, str], str | Exception]) -> dict:
    pages: dict[tuple[str, str], str | Exception] = {
        ("GET", KEIBABOOK_LOGIN): _load("keibabook_login_form.html"),
        ("POST", KEIBABOOK_LOGIN): "<html>posted</html>",
        ("POST", KEIBABOOK_LOGINCHECK): _load("keibabook_logincheck_in.html"),
    }
    pages.update(extra)
    return pages


def test_credentials_from_env_ignore_blanks():
    assert credentials_from_env({}).configured is False
    assert (
        credentials_from_env(
            {ENV_KEIBABOOK_LOGIN_ID: "  ", ENV_KEIBABOOK_PASSWORD: "secret"}
        ).configured
        is False
    )
    assert (
        credentials_from_env(
            {ENV_KEIBABOOK_LOGIN_ID: "member", ENV_KEIBABOOK_PASSWORD: "secret"}
        ).password_login
        is True
    )
    cookie_only = credentials_from_env({ENV_KEIBABOOK_COOKIE: "keibabook_session=abc"})
    assert cookie_only.configured is True
    assert cookie_only.password_login is False
    assert "secret" not in repr(cookie_only)


def test_logincheck_requires_a_logged_in_marker():
    assert logincheck_authenticated(_load("keibabook_logincheck_in.html")) is True
    assert logincheck_authenticated(_load("keibabook_logincheck_out.html")) is False
    assert logincheck_authenticated("<html></html>") is False


def test_syutuba_links_map_umacd_and_ignore_pedigree_links():
    mapping = parse_keibabook_syutuba(_load("keibabook_syutuba.html"))
    assert mapping == {"シー": "0000003", "ディー": "0000004"}
    ambiguous = """
    <a href="/db/uma/0000003" umacd="0000003">シー</a>
    <a href="/db/uma/0000099" umacd="0000099">シー</a>
    """
    assert "シー" not in parse_keibabook_syutuba(ambiguous)


def test_crireki_page_reads_full_dates_and_leaves_symbols_null():
    training = parse_keibabook_crireki(_load("keibabook_crireki.html"))
    assert training.source == SOURCE_KEIBABOOK_CRIREKI
    assert training.data_sufficiency == "sufficient"
    assert training.warning is None
    symbol, flat, slope = training.workouts
    assert symbol.date is None
    assert symbol.date_label == "■"
    assert flat.date == date(2026, 5, 29)
    assert flat.distance == "5F"
    assert flat.time == "67.4"
    assert flat.rank_in_group == 3
    assert slope.date == date(2026, 6, 1)
    assert slope.course == "美坂"
    assert slope.distance == "4F"
    assert slope.time == "58.1"
    assert slope.slope_reps == 1
    assert slope.condition == "稍"
    dumped = _dump(training)
    assert "99.9" not in dumped
    assert "人気" not in dumped
    assert "キタサンブラック記念" not in dumped


def test_locked_empty_and_malformed_crireki_do_not_invent_clocks():
    locked = parse_keibabook_crireki(_load("keibabook_crireki_locked.html"))
    assert locked.workouts == []
    assert locked.warning == TRAINING_HISTORY_REQUIRES_LOGIN
    empty = parse_keibabook_crireki(_load("keibabook_crireki_empty.html"))
    assert empty.workouts == []
    assert empty.warning == TRAINING_HISTORY_EMPTY
    broken = parse_keibabook_crireki(_load("keibabook_crireki_malformed.html"))
    assert broken.workouts == []
    assert broken.warning == TRAINING_TABLE_MALFORMED
    assert "67.4" not in _dump(broken)


def test_search_keeps_only_the_row_whose_birth_year_and_sex_match():
    candidates = parse_keibabook_horse_search(_load("keibabook_search_multi.html"))
    assert [item.umacd for item in candidates] == ["0000003", "0000008", "0000009"]
    assert select_umacd(candidates, "シー", date(2022, 3, 4), "牡") == "0000003"
    assert select_umacd(candidates, "シー", date(2022, 3, 4), "牝") is None
    assert select_umacd(candidates, "シー", None, "牡") is None

    twins = [
        UmacdCandidate(umacd="0000003", name="シー", birth_year=2022, sex="牡"),
        UmacdCandidate(umacd="0000008", name="シー", birth_year=2022, sex="牡"),
    ]
    assert select_umacd(twins, "シー", date(2022, 3, 4), "牡") is None
    assert "0000111" not in {item.umacd for item in candidates}
    profile = parse_keibabook_horse_search(_load("keibabook_horse_profile.html"))
    assert select_umacd(profile, "シー", date(2022, 3, 4), "牡") == "0000003"
    assert select_umacd(profile, "シー", date(2022, 3, 5), "牡") is None


def test_logged_in_crireki_fills_a_horse_missing_from_the_anonymous_page():
    crireki = KEIBABOOK_CRIREKI.format(umacd="0000003")
    client = _Web(
        _member_pages(
            {
                ("GET", KEIBABOOK_CYOKYO.format(race_id=RACE_ID)): _load("keibabook_cyokyo.html"),
                ("GET", KEIBABOOK_SYUTUBA.format(race_id=RACE_ID)): _load("keibabook_syutuba.html"),
                ("GET", crireki): _load("keibabook_crireki.html"),
                ("GET", SPEEDKEIBA_INDEX): _load("speedkeiba_index.html"),
                ("GET", "https://speedkeiba.com/week/sprinters"): _load("speedkeiba_article.html"),
            }
        )
    )
    scraper = TrainingScraper(http_client=client, credentials=_MEMBER)  # type: ignore[arg-type]
    chosen = scraper.training_for_race(
        RACE_ID, "スプリンターズＳ", ["サルヴァトーレ", "シー", "ビー", "未知"]
    )
    assert chosen["サルヴァトーレ"].source == SOURCE_KEIBABOOK_SMART
    assert chosen["サルヴァトーレ"].workouts[0].time == "66.6"
    covered = chosen["シー"]
    assert covered.source == SOURCE_KEIBABOOK_CRIREKI
    assert covered.data_sufficiency == "sufficient"
    assert covered.workouts[1].time == "67.4"
    assert covered.summary is not None
    assert covered.summary.comment == "動き良い"
    dumped = _dump(covered)
    assert "99.9" not in dumped
    assert "人気" not in dumped
    assert "secret" not in dumped
    assert chosen["ビー"].source == SOURCE_SPEEDKEIBA
    assert chosen["未知"].workouts == []
    assert chosen["未知"].warning == TRAINING_NOT_ON_EITHER
    urls = [url for _method, url in client.calls]
    assert crireki in urls
    assert KEIBABOOK_CRIREKI.format(umacd="0000004") not in urls
    login_post = [data for url, data in client.posted if url == KEIBABOOK_LOGIN][0]
    assert login_post is not None
    assert login_post["login_id"] == "member"
    assert login_post["pswd"] == "secret"


def test_member_cyokyo_page_covers_the_horse_without_opening_crireki():
    client = _Web(
        _member_pages(
            {
                ("GET", KEIBABOOK_CYOKYO.format(race_id=RACE_ID)): _load("keibabook_crireki.html"),
                ("GET", KEIBABOOK_SYUTUBA.format(race_id=RACE_ID)): _load("keibabook_syutuba.html"),
                ("GET", SPEEDKEIBA_INDEX): _load("speedkeiba_index.html"),
            }
        )
    )
    scraper = TrainingScraper(http_client=client, credentials=_MEMBER)  # type: ignore[arg-type]
    chosen = scraper.training_for_race(RACE_ID, "未勝利", ["シー"])
    assert chosen["シー"].source == SOURCE_KEIBABOOK_SMART
    assert chosen["シー"].workouts[1].time == "67.4"
    assert all("/crireki" not in url for _method, url in client.calls)


def test_failed_login_keeps_anonymous_clocks_and_skips_history():
    pages = _member_pages(
        {
            ("GET", KEIBABOOK_CYOKYO.format(race_id=RACE_ID)): _load("keibabook_cyokyo.html"),
            ("GET", SPEEDKEIBA_INDEX): _load("speedkeiba_index.html"),
            ("GET", "https://speedkeiba.com/week/sprinters"): _load("speedkeiba_article.html"),
        }
    )
    pages[("POST", KEIBABOOK_LOGINCHECK)] = _load("keibabook_logincheck_out.html")
    client = _Web(pages)
    scraper = TrainingScraper(http_client=client, credentials=_MEMBER)  # type: ignore[arg-type]
    chosen = scraper.training_for_race(RACE_ID, "スプリンターズＳ", ["サルヴァトーレ", "シー"])
    assert chosen["サルヴァトーレ"].source == SOURCE_KEIBABOOK_SMART
    assert chosen["サルヴァトーレ"].workouts[0].time == "66.6"
    missed = chosen["シー"]
    assert missed.workouts == []
    assert missed.warning == TRAINING_LOGIN_FAILED
    assert missed.source is None
    assert all("/crireki" not in url and "syutuba" not in url for _method, url in client.calls)


def test_malformed_crireki_drops_the_row_and_locked_history_stays_empty():
    crireki = KEIBABOOK_CRIREKI.format(umacd="0000003")
    client = _Web(
        _member_pages(
            {
                ("GET", KEIBABOOK_CYOKYO.format(race_id=RACE_ID)): _load(
                    "keibabook_cyokyo_absent.html"
                ),
                ("GET", KEIBABOOK_SYUTUBA.format(race_id=RACE_ID)): _load("keibabook_syutuba.html"),
                ("GET", crireki): _load("keibabook_crireki_malformed.html"),
                ("GET", SPEEDKEIBA_INDEX): _load("speedkeiba_index.html"),
            }
        )
    )
    scraper = TrainingScraper(http_client=client, credentials=_MEMBER)  # type: ignore[arg-type]
    broken = scraper.training_for_race(RACE_ID, "未勝利", ["シー"])["シー"]
    assert broken.workouts == []
    assert broken.warning == TRAINING_TABLE_MALFORMED
    assert "67.4" not in _dump(broken)

    client.pages[("GET", crireki)] = _load("keibabook_crireki_locked.html")
    scraper._login_state = None
    locked = scraper.training_for_race(RACE_ID, "未勝利", ["シー"])["シー"]
    assert locked.workouts == []
    assert locked.warning == TRAINING_HISTORY_REQUIRES_LOGIN

    client.pages[("GET", crireki)] = _load("keibabook_crireki_empty.html")
    scraper._login_state = None
    empty = scraper.training_for_race(RACE_ID, "未勝利", ["シー"])["シー"]
    assert empty.warning == TRAINING_HISTORY_EMPTY
    assert empty.workouts == []


def test_login_form_without_a_token_does_not_pretend_success():
    client = _Web(
        {
            ("GET", KEIBABOOK_LOGIN): "<html><form></form></html>",
            ("GET", KEIBABOOK_CYOKYO.format(race_id=RACE_ID)): _load("keibabook_cyokyo.html"),
            ("GET", SPEEDKEIBA_INDEX): _load("speedkeiba_index.html"),
        }
    )
    scraper = TrainingScraper(http_client=client, credentials=_MEMBER)  # type: ignore[arg-type]
    chosen = scraper.training_for_race(RACE_ID, "未勝利", ["サルヴァトーレ", "シー"])
    assert chosen["サルヴァトーレ"].source == SOURCE_KEIBABOOK_SMART
    assert chosen["シー"].workouts == []
    assert chosen["シー"].warning == TRAINING_LOGIN_FAILED
    assert all(method != "POST" for method, _url in client.calls)


def test_anonymous_race_does_not_call_login_or_crireki():
    client = _Web(
        {
            ("GET", KEIBABOOK_CYOKYO.format(race_id=RACE_ID)): _load("keibabook_cyokyo.html"),
            ("GET", SPEEDKEIBA_INDEX): _load("speedkeiba_index.html"),
        }
    )
    scraper = TrainingScraper(http_client=client)  # type: ignore[arg-type]
    chosen = scraper.training_for_race(RACE_ID, "未勝利", ["サルヴァトーレ"])
    assert chosen["サルヴァトーレ"].source == SOURCE_KEIBABOOK_SMART
    assert [method for method, _url in client.calls] == ["GET", "GET"]
    assert all("login" not in url and "crireki" not in url for _method, url in client.calls)


def test_horse_history_follows_the_matching_search_link_only():
    crireki = KEIBABOOK_CRIREKI.format(umacd="0000003")
    client = _Web(
        _member_pages(
            {
                ("GET", SPEEDKEIBA_INDEX): _load("speedkeiba_index.html"),
                ("GET", "https://speedkeiba.com/week/sprinters"): _load(
                    "speedkeiba_article_absent.html"
                ),
                ("GET", "https://speedkeiba.com/week/sirius"): _load(
                    "speedkeiba_article_absent.html"
                ),
                ("GET", KEIBABOOK_HORSE_SEARCH): _load("keibabook_search_form.html"),
                ("POST", KEIBABOOK_HORSE_SEARCH): _load("keibabook_search_multi.html"),
                ("GET", crireki): _load("keibabook_crireki.html"),
            }
        )
    )
    scraper = TrainingScraper(http_client=client, credentials=_MEMBER)  # type: ignore[arg-type]
    training = scraper.training_for_horse("シー", birth_date=date(2022, 3, 4), sex="牡")
    assert training.source == SOURCE_KEIBABOOK_CRIREKI
    assert training.workouts[1].time == "67.4"
    assert "99.9" not in _dump(training)
    assert "2019105283" not in " ".join(url for _method, url in client.calls)
    search = [data for url, data in client.posted if url == KEIBABOOK_HORSE_SEARCH][0]
    assert search is not None
    assert search["bamei"] == "シー"
    assert search["_token"] == "search-token"

    missed = scraper.training_for_horse("シー", birth_date=date(2019, 1, 1), sex="牡")
    assert missed.workouts == []
    assert missed.warning == TRAINING_UMACD_UNMAPPED
    assert missed.source is None
    urls = [url for _method, url in client.calls]
    assert urls.count(crireki) == 1


def test_profile_redirect_maps_umacd_only_when_the_printed_birth_date_matches():
    crireki = KEIBABOOK_CRIREKI.format(umacd="0000003")
    client = _Web(
        _member_pages(
            {
                ("GET", SPEEDKEIBA_INDEX): _load("speedkeiba_index.html"),
                ("GET", "https://speedkeiba.com/week/sprinters"): _load(
                    "speedkeiba_article_absent.html"
                ),
                ("GET", "https://speedkeiba.com/week/sirius"): _load(
                    "speedkeiba_article_absent.html"
                ),
                ("GET", KEIBABOOK_HORSE_SEARCH): _load("keibabook_search_form.html"),
                ("POST", KEIBABOOK_HORSE_SEARCH): _load("keibabook_horse_profile.html"),
                ("GET", crireki): _load("keibabook_crireki.html"),
            }
        )
    )
    scraper = TrainingScraper(http_client=client, credentials=_MEMBER)  # type: ignore[arg-type]
    training = scraper.training_for_horse("シー", birth_date=date(2022, 3, 4), sex="牡")
    assert training.source == SOURCE_KEIBABOOK_CRIREKI
    assert training.summary is not None
    assert training.summary.arrow == "↗"
    mismatch = scraper.training_for_horse("シー", birth_date=date(2022, 3, 5), sex="牡")
    assert mismatch.warning == TRAINING_UMACD_UNMAPPED
    assert mismatch.workouts == []
    assert "99.9" not in _dump(training)


def test_cookie_session_is_not_treated_as_login_when_logincheck_is_logged_out():
    client = _Web(
        {
            ("POST", KEIBABOOK_LOGINCHECK): _load("keibabook_logincheck_out.html"),
            ("GET", SPEEDKEIBA_INDEX): _load("speedkeiba_index.html"),
            ("GET", "https://speedkeiba.com/week/sprinters"): _load(
                "speedkeiba_article_absent.html"
            ),
            ("GET", "https://speedkeiba.com/week/sirius"): _load("speedkeiba_article_absent.html"),
        }
    )
    scraper = TrainingScraper(
        http_client=client,  # type: ignore[arg-type]
        credentials=KeibabookCredentials(cookie="keibabook_session=abc"),
    )
    training = scraper.training_for_horse("シー", birth_date=date(2022, 3, 4), sex="牡")
    assert training.workouts == []
    assert training.warning == TRAINING_LOGIN_FAILED
    assert client.headers["Cookie"] == "keibabook_session=abc"
    assert all(url != KEIBABOOK_LOGIN for _method, url in client.calls)
    assert all("/crireki" not in url for _method, url in client.calls)


def test_get_horse_info_passes_birth_date_into_training_lookup():
    from datetime import datetime

    from backend.models.horse import Horse

    seen: dict = {}

    class _Net:
        def get_horse_info(self, horse_id: str):
            assert horse_id == "2019105283"
            return Horse(
                horse_id=horse_id,
                horse_name="シー",
                sex="牡",
                birth_date=date(2022, 3, 4),
                fetched_at=datetime(2026, 9, 28, 1, 2, 3),
            )

        def close(self) -> None:
            return None

    class _Training:
        def training_for_horse(self, horse_name: str, **kwargs):
            seen["name"] = horse_name
            seen["kwargs"] = kwargs
            return unavailable_training(TRAINING_HORSE_UNAVAILABLE)

        def close(self) -> None:
            return None

    service = RaceService()
    service._netkeiba_scraper = _Net()  # type: ignore[assignment]
    service._training_scraper = _Training()  # type: ignore[assignment]
    horse = service.get_horse_info("2019105283")
    assert horse is not None
    assert seen["name"] == "シー"
    assert seen["kwargs"] == {"birth_date": date(2022, 3, 4), "sex": "牡"}
    assert horse.training is not None
    assert horse.training.warning == TRAINING_HORSE_UNAVAILABLE
    assert "2019105283" not in _dump(horse.training)


@pytest.mark.integration
def test_live_keibabook_clocks_are_copied_from_the_page():
    import re

    from backend.utils.http_client import HTTPClient

    url = KEIBABOOK_CYOKYO.format(race_id="202604040211")
    html = HTTPClient(min_request_interval=0.2).get(url, encoding="utf-8")
    page = parse_keibabook_cyokyo(html)
    assert page.page_warning is None
    assert page.by_name
    for training in page.by_name.values():
        assert training.source == SOURCE_KEIBABOOK_SMART
        for workout in training.workouts:
            if workout.time:
                assert workout.time in html
            for split in workout.splits:
                assert split.time in html
        dumped = _dump(training)
        assert not re.search(r"単勝|人気", dumped)


@pytest.mark.integration
def test_live_anonymous_history_stays_locked_and_shutuba_links_are_real():
    """Network check. Anonymous crireki has no clocks. Shutuba links supply umacd."""
    from backend.scrapers.training_scraper import name_key
    from backend.utils.http_client import HTTPClient

    client = HTTPClient(min_request_interval=0.2)
    try:
        check = client.post(KEIBABOOK_LOGINCHECK, data={"motourl": "/"}, encoding="utf-8")
        assert "ログインしていない" in check
        history = client.get(KEIBABOOK_CRIREKI.format(umacd="0952386"), encoding="utf-8")
        parsed = parse_keibabook_crireki(history)
        assert parsed.workouts == []
        assert parsed.warning == TRAINING_HISTORY_REQUIRES_LOGIN
        shutuba = client.get(KEIBABOOK_SYUTUBA.format(race_id="202604040211"), encoding="utf-8")
        mapping = parse_keibabook_syutuba(shutuba)
        assert mapping[name_key("アドマイヤクワッズ")] == "0952386"
    finally:
        client.close()

    creds = credentials_from_env()
    if not creds.configured:
        return
    scraper = TrainingScraper(credentials=creds)
    try:
        assert scraper._ensure_login() == "in"
    finally:
        scraper.close()
