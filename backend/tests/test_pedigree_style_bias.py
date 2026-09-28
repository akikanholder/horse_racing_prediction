"""Pedigree, running style, and track bias. Missing data stays null."""

import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest
import requests

from backend.cli import _print_pedigree, _print_running_style, _print_track_bias
from backend.models.horse import (
    PEDIGREE_ABSENT,
    PEDIGREE_FETCH_FAILED,
    PEDIGREE_INCOMPLETE,
    PEDIGREE_SOURCE_BOTH,
    PEDIGREE_SOURCE_PED,
    PEDIGREE_SOURCE_PROFILE,
    PEDIGREE_TABLE_MALFORMED,
    PEDIGREE_TABLE_NOT_FOUND,
    Horse,
    HorsePerformance,
    Pedigree,
    PedigreeHorse,
)
from backend.models.race import (
    TRACK_BIAS_UNAVAILABLE,
    Race,
    RaceCard,
    RaceEntry,
)
from backend.models.running_style import (
    STYLE_FIELD_SIZE_UNAVAILABLE,
    STYLE_PASSING_UNAVAILABLE,
    STYLE_SAMPLE_TOO_SMALL,
    STYLE_TIE,
    classify_early_call,
    derive_running_style,
)
from backend.models.training import TRAINING_NOT_ON_KEIBABOOK, unavailable_training
from backend.scrapers.netkeiba_scraper import NetkeibaScraper
from backend.services.race_service import RaceService

FIXTURES = Path(__file__).parent / "fixtures"
HORSE_ID = "2019105283"
FETCHED_AT = datetime(2026, 9, 28, 9, 0, 0)


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class _Pages:
    def __init__(self, profile: str | Exception, result: str | Exception, ped: str | Exception):
        self.profile = profile
        self.result = result
        self.ped = ped
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, headers=None, encoding=None):
        self.calls.append((url, headers or {}))
        if "/horse/ped/" in url:
            return self._unwrap(self.ped)
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


def _horse(profile: str | Exception, result: str | Exception, ped: str | Exception):
    client = _Pages(profile, result, ped)
    scraper = NetkeibaScraper(http_client=client)  # type: ignore[arg-type]
    return scraper.get_horse_info(HORSE_ID, fetched_at=FETCHED_AT), client


def _run(
    passing: str | None,
    entry: int | None,
    distance: int | None,
    surface: str | None = "芝",
    **overrides,
) -> HorsePerformance:
    values = {
        "race_date": date(2024, 1, 1),
        "race_name": "未勝利",
        "venue": "東京",
        "passing_order": passing,
        "entry_count": entry,
        "distance": distance,
        "track_type": surface,
    }
    values.update(overrides)
    return HorsePerformance(**values)


def _repeat(run: HorsePerformance, count: int) -> list[HorsePerformance]:
    return [run.model_copy() for _ in range(count)]


def test_ped_page_fills_sire_dam_and_damsire_without_the_decoy():
    horse, client = _horse(
        _load("netkeiba_horse_profile.html"),
        _load("netkeiba_horse_result.html"),
        _load("netkeiba_horse_ped.html"),
    )
    assert horse is not None
    pedigree = horse.pedigree
    assert pedigree is not None
    assert pedigree.sire is not None
    assert pedigree.sire.name == "ハーツクライ"
    assert pedigree.sire.horse_id == "2001103038"
    assert pedigree.dam is not None
    assert pedigree.dam.name == "ダストアンドダイヤモンズ"
    assert pedigree.dam.horse_id == "000a0137a4"
    assert pedigree.damsire is not None
    assert pedigree.damsire.name == "Vindication"
    assert pedigree.damsire.horse_id == "000a010544"
    assert pedigree.source == PEDIGREE_SOURCE_PED
    assert pedigree.warning is None
    assert horse.sire_name == "ハーツクライ"
    assert horse.sire_id == "2001103038"
    assert horse.dam_name == "ダストアンドダイヤモンズ"
    assert horse.damsire_name == "Vindication"
    dumped = pedigree.model_dump()
    assert "キタサンブラック" not in str(dumped)
    assert "誤読父" not in str(dumped)
    ped_call = next(call for call in client.calls if "/horse/ped/" in call[0])
    assert ped_call[0] == f"https://db.netkeiba.com/horse/ped/{HORSE_ID}/"
    assert "Chrome/140" in ped_call[1]["User-Agent"]
    assert ped_call[1]["Referer"] == f"https://db.netkeiba.com/horse/{HORSE_ID}/"
    style = horse.running_style
    assert style is not None
    assert style.label is None
    assert style.counts["追込"] == 2
    assert style.sample_size == 2
    assert style.unusable_runs == 1
    assert style.warning == STYLE_SAMPLE_TOO_SMALL
    assert style.data_sufficiency == "insufficient"
    assert horse.track_bias.inside_outside is None
    assert horse.track_bias.pace_bias is None
    assert horse.track_bias.venue is None
    assert horse.track_bias.warning == TRACK_BIAS_UNAVAILABLE


def test_absent_pedigree_page_does_not_read_the_race_name():
    horse, _client = _horse(
        _load("netkeiba_horse_profile.html"),
        _load("netkeiba_horse_result_empty.html"),
        _load("netkeiba_horse_ped_absent.html"),
    )
    assert horse is not None
    pedigree = horse.pedigree
    assert pedigree is not None
    assert pedigree.sire is None
    assert pedigree.dam is None
    assert pedigree.damsire is None
    assert pedigree.source is None
    assert pedigree.warning == PEDIGREE_TABLE_NOT_FOUND
    assert horse.sire_name is None
    assert horse.dam_name is None
    assert horse.damsire_name is None
    assert "キタサンブラック" not in str(pedigree.model_dump())


def test_malformed_pedigree_table_is_not_parsed():
    horse, _client = _horse(
        _load("netkeiba_horse_profile.html"),
        _load("netkeiba_horse_result_empty.html"),
        _load("netkeiba_horse_ped_malformed.html"),
    )
    assert horse is not None
    assert horse.pedigree is not None
    assert horse.pedigree.sire is None
    assert horse.pedigree.warning == PEDIGREE_TABLE_MALFORMED
    assert horse.sire_name is None


def test_pedigree_fetch_failure_stays_empty():
    horse, _client = _horse(
        _load("netkeiba_horse_profile.html"),
        _load("netkeiba_horse_result_empty.html"),
        requests.RequestException("timeout"),
    )
    assert horse is not None
    assert horse.pedigree is not None
    assert horse.pedigree.sire is None
    assert horse.pedigree.dam is None
    assert horse.pedigree.warning == PEDIGREE_FETCH_FAILED
    assert horse.warning != PEDIGREE_FETCH_FAILED


def test_profile_names_are_kept_when_the_ped_page_disagrees():
    profile = """
    <div class="horse_title"><h1>保持馬</h1><p class="txt_01">牡</p></div>
    <table class="db_prof_table">
      <tr><th>父</th><td>父馬</td></tr>
      <tr><th>母</th><td>母馬</td></tr>
    </table>
    """
    ped = """
    <table class="blood_table">
      <tr>
        <td rowspan="2"><a href="https://db.netkeiba.com/horse/sirenew1/">別父</a></td>
        <td><a href="https://db.netkeiba.com/horse/gsid1/">祖父</a></td>
      </tr>
      <tr><td><a href="https://db.netkeiba.com/horse/gmid1/">祖母</a></td></tr>
      <tr>
        <td rowspan="2"><a href="https://db.netkeiba.com/horse/damid1/">母馬</a></td>
        <td rowspan="2"><a href="https://db.netkeiba.com/horse/dsid1/">母父馬</a></td>
      </tr>
      <tr><td><a href="https://db.netkeiba.com/horse/ddid1/">母母</a></td></tr>
    </table>
    """
    horse, _client = _horse(profile, _load("netkeiba_horse_result_empty.html"), ped)
    assert horse is not None
    pedigree = horse.pedigree
    assert pedigree is not None
    assert pedigree.sire is not None
    assert pedigree.sire.name == "父馬"
    assert pedigree.sire.horse_id is None
    assert pedigree.dam is not None
    assert pedigree.dam.name == "母馬"
    assert pedigree.dam.horse_id == "damid1"
    assert pedigree.damsire is not None
    assert pedigree.damsire.name == "母父馬"
    assert pedigree.damsire.horse_id == "dsid1"
    assert pedigree.source == PEDIGREE_SOURCE_BOTH
    assert pedigree.warning is None
    assert "別父" not in str(pedigree.model_dump())


def test_complete_profile_does_not_request_the_ped_page():
    profile = """
    <div class="horse_title"><h1>揃い馬</h1><p class="txt_01">牝</p></div>
    <table class="db_prof_table">
      <tr><th>父</th><td><a href="https://db.netkeiba.com/horse/s1/">父馬</a></td></tr>
      <tr><th>母</th><td><a href="https://db.netkeiba.com/horse/d1/">母馬</a></td></tr>
      <tr><th>母父</th><td><a href="https://db.netkeiba.com/horse/ds1/">母父馬</a></td></tr>
    </table>
    """
    horse, client = _horse(profile, _load("netkeiba_horse_result_empty.html"), "<table></table>")
    assert horse is not None
    assert horse.pedigree is not None
    assert horse.pedigree.source == PEDIGREE_SOURCE_PROFILE
    assert horse.pedigree.sire is not None
    assert horse.pedigree.sire.horse_id == "s1"
    assert horse.pedigree.damsire is not None
    assert horse.pedigree.damsire.name == "母父馬"
    assert horse.pedigree.warning is None
    assert not any("/horse/ped/" in url for url, _headers in client.calls)


def test_profile_sire_only_is_incomplete_when_ped_page_has_no_dam():
    profile = """
    <div class="horse_title"><h1>片親</h1><p class="txt_01">牡</p></div>
    <table class="db_prof_table">
      <tr><th>父</th><td>父だけ</td></tr>
    </table>
    """
    ped = """
    <table class="blood_table">
      <tr><td rowspan="2"><a href="https://db.netkeiba.com/horse/s1/">父だけ</a></td><td>x</td></tr>
      <tr><td>y</td></tr>
      <tr><td rowspan="2"></td><td></td></tr>
      <tr><td></td></tr>
    </table>
    """
    horse, _client = _horse(profile, _load("netkeiba_horse_result_empty.html"), ped)
    assert horse is not None
    assert horse.pedigree is not None
    assert horse.pedigree.sire is not None
    assert horse.pedigree.sire.name == "父だけ"
    assert horse.pedigree.dam is None
    assert horse.pedigree.warning == PEDIGREE_INCOMPLETE


def test_compact_blood_table_still_fills_sire_and_dam():
    profile = """
    <div class="horse_title"><h1>血統馬</h1><p class="txt_01">牝 栗毛</p></div>
    <table class="blood"><tr><td><a>キタサンブラック</a><a>マルシェローラ</a></td></tr></table>
    """
    horse, _client = _horse(
        profile,
        _load("netkeiba_horse_result_empty.html"),
        _load("netkeiba_horse_ped_absent.html"),
    )
    assert horse is not None
    assert horse.sire_name == "キタサンブラック"
    assert horse.dam_name == "マルシェローラ"
    assert horse.damsire_name is None
    assert horse.pedigree is not None
    assert horse.pedigree.source == PEDIGREE_SOURCE_PROFILE
    assert horse.pedigree.warning is None
    assert horse.pedigree.sire is not None
    assert horse.pedigree.sire.horse_id is None


@pytest.mark.parametrize(
    ("position", "field_size", "expected"),
    [
        (1, 14, "逃げ"),
        (1, 1, "逃げ"),
        (2, 14, "先行"),
        (5, 14, "先行"),
        (6, 14, "差し"),
        (10, 14, "差し"),
        (11, 14, "追込"),
        (13, 14, "追込"),
        (11, 13, "追込"),
        (2, 5, "先行"),
        (3, 5, "差し"),
        (5, 5, "追込"),
        (2, 2, "差し"),
        (8, 18, "差し"),
        (13, 18, "追込"),
        (5, 4, None),
        (0, 10, None),
        (1, 0, None),
    ],
)
def test_early_call_thirds(position, field_size, expected):
    assert classify_early_call(position, field_size) == expected


def test_running_style_ignores_odds_popularity_and_race_name():
    front = _repeat(
        _run("1-1-1-1", 16, 1200, race_name="逃げ馬ステークス", odds=1.2, popularity=1), 5
    )
    closer = _repeat(
        _run("13-12-12-7", 14, 2400, race_name="逃げ馬ステークス", odds=99.9, popularity=14),
        5,
    )
    front_style = derive_running_style(front)
    closer_style = derive_running_style(closer)
    assert front_style.label == "逃げ"
    assert front_style.data_sufficiency == "sufficient"
    assert front_style.confidence == 1.0
    assert front_style.short_distance_front_runs == 5
    assert front_style.short_distance_front_under_specified is False
    assert closer_style.label == "追込"
    assert closer_style.basis == "passing_order_first_call"
    renamed = derive_running_style(
        _repeat(_run("1-1-1-1", 16, 1200, race_name="追込特別", odds=80.0, popularity=12), 5)
    )
    assert renamed.label == front_style.label
    assert renamed.counts == front_style.counts


def test_short_front_flag_does_not_clear_a_closer_label():
    runs = _repeat(_run("8-8-7", 14, 2000), 4)
    runs.append(_run("1-1", 14, 1200, "ダート"))
    style = derive_running_style(runs)
    assert style.label == "差し"
    assert style.sample_size == 5
    assert style.confidence == 0.8
    assert style.data_sufficiency == "sufficient"
    assert style.short_distance_runs == 1
    assert style.short_distance_front_runs == 1
    assert style.short_distance_front_under_specified is True
    assert style.warning is None


def test_one_or_two_runs_do_not_publish_a_label():
    style = derive_running_style(_repeat(_run("1-1", 12, 1200), 2))
    assert style.label is None
    assert style.counts["逃げ"] == 2
    assert style.sample_size == 2
    assert style.confidence is None
    assert style.data_sufficiency == "insufficient"
    assert style.warning == STYLE_SAMPLE_TOO_SMALL
    assert style.short_distance_front_under_specified is True


def test_tie_stays_null():
    runs = _repeat(_run("1-1", 16, 1800), 3) + _repeat(_run("12-12", 16, 1800), 3)
    style = derive_running_style(runs)
    assert style.label is None
    assert style.counts["逃げ"] == 3
    assert style.counts["追込"] == 3
    assert style.warning == STYLE_TIE
    assert style.data_sufficiency == "thin"
    assert style.confidence is None


def test_missing_passing_order_and_field_size_stay_unlabeled():
    empty = derive_running_style(
        [_run(None, 16, 1600), _run("**", 16, 1600), _run("13-12-x", 16, 1600)]
    )
    assert empty.label is None
    assert empty.sample_size == 0
    assert empty.warning == STYLE_PASSING_UNAVAILABLE
    assert empty.basis is None
    assert empty.counts == {"逃げ": 0, "先行": 0, "差し": 0, "追込": 0}

    no_field = derive_running_style([_run("4-4-4", None, 1600), _run("1", 0, 1400)])
    assert no_field.label is None
    assert no_field.warning == STYLE_FIELD_SIZE_UNAVAILABLE
    assert no_field.sample_size == 0


def test_obstacle_and_distance_edges_are_not_short_front():
    jumps = derive_running_style(_repeat(_run("1-1", 10, 1200, "障害"), 3))
    assert jumps.label == "逃げ"
    assert jumps.short_distance_runs == 0
    assert jumps.short_distance_front_runs == 0
    assert jumps.short_distance_front_under_specified is False

    mile = derive_running_style(_repeat(_run("1-1", 16, 1401), 3))
    assert mile.short_distance_front_runs == 0
    sprint = derive_running_style(_repeat(_run("2-2", 14, 1400), 3))
    assert sprint.label == "先行"
    assert sprint.short_distance_front_runs == 3
    assert sprint.data_sufficiency == "thin"
    unknown_distance = derive_running_style(_repeat(_run("1-1", 12, None), 3))
    assert unknown_distance.label == "逃げ"
    assert unknown_distance.short_distance_front_runs == 0


def test_three_runs_are_thin_and_five_clear_runs_are_sufficient():
    thin = derive_running_style(_repeat(_run("3-3", 14, 2000), 3))
    assert thin.label == "先行"
    assert thin.confidence == 1.0
    assert thin.data_sufficiency == "thin"
    mixed = derive_running_style(
        _repeat(_run("3-3", 14, 2000), 3) + _repeat(_run("12-12", 14, 2000), 2)
    )
    assert mixed.label == "先行"
    assert mixed.sample_size == 5
    assert mixed.confidence == 0.6
    assert mixed.data_sufficiency == "sufficient"


def test_horse_text_view_prints_nulls_and_warnings(capsys):
    horse = Horse(
        horse_id="1",
        horse_name="無名",
        sex="牡",
        fetched_at=FETCHED_AT,
        performances=[_run(None, 10, 1600, race_name="逃げ特別")],
    )
    assert horse.pedigree is not None
    assert horse.pedigree.warning == PEDIGREE_ABSENT
    assert horse.running_style is not None
    assert horse.running_style.label is None
    assert horse.running_style.warning == STYLE_PASSING_UNAVAILABLE
    _print_pedigree(horse)
    _print_running_style(horse.running_style)
    _print_track_bias(horse.track_bias)
    out = capsys.readouterr().out
    assert "父 不明" in out
    assert PEDIGREE_ABSENT in out
    assert "脚質: 不明" in out
    assert STYLE_PASSING_UNAVAILABLE in out
    assert TRACK_BIAS_UNAVAILABLE in out
    assert "逃げ特別" not in out


def test_race_card_copies_conditions_and_leaves_bias_null():
    card = RaceCard(
        race=Race(
            race_id="202609280605",
            race_name="短距離S",
            race_number=5,
            race_date=date(2026, 9, 28),
            venue="中山",
            distance=1200,
            track_type="ダート",
            track_condition="稍重",
        ),
        entries=[],
    )
    bias = card.race.track_bias
    assert bias is not None
    assert bias.venue == "中山"
    assert bias.surface == "ダート"
    assert bias.distance == 1200
    assert bias.going == "稍重"
    assert bias.inside_outside is None
    assert bias.pace_bias is None
    assert bias.source is None
    assert bias.warning == TRACK_BIAS_UNAVAILABLE

    blank = RaceCard(
        race=Race(
            race_id="x",
            race_name="不明",
            race_number=1,
            race_date=date(2026, 9, 28),
            venue="不明",
            distance=0,
            track_type="不明",
            track_condition="稍",
        ),
        entries=[],
    )
    assert blank.race.track_bias is not None
    assert blank.race.track_bias.venue is None
    assert blank.race.track_bias.surface is None
    assert blank.race.track_bias.distance is None
    assert blank.race.track_bias.going is None
    assert blank.race.track_bias.warning == TRACK_BIAS_UNAVAILABLE


def test_schedule_race_does_not_invent_track_bias():
    race = Race(
        race_id="202609280611",
        race_name="未定",
        race_number=11,
        race_date=date(2026, 9, 28),
        venue="東京",
        distance=1600,
        track_type="芝",
    )
    assert race.track_bias is None


def test_race_horses_payload_includes_pedigree_style_and_race_bias():
    performances = _repeat(_run("1-1", 14, 1200, "ダート"), 2)

    class Scraper:
        def get_race_card(self, race_id: str) -> RaceCard:
            return RaceCard(
                race=Race(
                    race_id=race_id,
                    race_name="テスト",
                    race_number=1,
                    race_date=date(2026, 9, 28),
                    venue="中山",
                    distance=1200,
                    track_type="ダート",
                    track_condition="稍重",
                ),
                entries=[
                    RaceEntry(
                        bracket_number=1,
                        horse_number=1,
                        horse_name="エイ",
                        horse_id="h1",
                        sex="牡",
                        age=4,
                    ),
                    RaceEntry(bracket_number=2, horse_number=2, horse_name="ビー"),
                ],
            )

        def get_horse_performances(self, horse_id: str, limit: int = 10):
            assert horse_id == "h1"
            assert limit == 5
            return performances

        def get_pedigree(self, horse_id: str) -> Pedigree:
            assert horse_id == "h1"
            return Pedigree(
                sire=PedigreeHorse(name="父馬", horse_id="s1"),
                dam=PedigreeHorse(name="母馬", horse_id="d1"),
                damsire=None,
                source=PEDIGREE_SOURCE_PED,
                warning=None,
            )

    class _Training:
        def training_for_race(self, race_id: str, race_name: str, horse_names: list[str]):
            assert race_id == "202609280601"
            assert race_name == "テスト"
            return {name: unavailable_training(TRAINING_NOT_ON_KEIBABOOK) for name in horse_names}

        def close(self) -> None:
            return None

    service = RaceService()
    service._netkeiba_scraper = Scraper()  # type: ignore[assignment]
    service._training_scraper = _Training()  # type: ignore[assignment]
    horses = service.get_horses_for_race("202609280601")
    assert len(horses) == 1
    horse = horses[0]
    assert horse.sire_name == "父馬"
    assert horse.dam_name == "母馬"
    assert horse.pedigree is not None
    assert horse.pedigree.sire is not None
    assert horse.pedigree.sire.horse_id == "s1"
    assert horse.pedigree.damsire is None
    assert horse.running_style is not None
    assert horse.running_style.label is None
    assert horse.running_style.sample_size == 2
    assert horse.running_style.short_distance_front_under_specified is True
    assert horse.track_bias.going == "稍重"
    assert horse.track_bias.surface == "ダート"
    assert horse.track_bias.distance == 1200
    assert horse.track_bias.inside_outside is None
    assert horse.track_bias.warning == TRACK_BIAS_UNAVAILABLE
    assert horse.training is not None
    assert horse.training.workouts == []
    assert horse.training.warning == TRAINING_NOT_ON_KEIBABOOK
    assert horse.training.source is None


def test_cli_help_documents_pedigree_style_and_bias():
    horse_help = subprocess.run(
        [sys.executable, "-m", "backend.cli", "horse", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "血統" in horse_help.stdout
    assert "脚質" in horse_help.stdout
    assert "調教" in horse_help.stdout
    assert "accessD/JRADB" in horse_help.stdout
    card_help = subprocess.run(
        [sys.executable, "-m", "backend.cli", "card", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "track_bias" in card_help.stdout
    assert "accessD/JRADB" in card_help.stdout


@pytest.mark.integration
def test_live_do_deuce_pedigree_comes_from_the_ped_page():
    with NetkeibaScraper() as scraper:
        horse = scraper.get_horse_info(HORSE_ID)

    assert horse is not None
    assert horse.pedigree is not None
    assert horse.pedigree.sire is not None
    assert horse.pedigree.sire.name == "ハーツクライ"
    assert horse.pedigree.sire.horse_id == "2001103038"
    assert horse.pedigree.dam is not None
    assert horse.pedigree.dam.name == "ダストアンドダイヤモンズ"
    assert horse.pedigree.dam.horse_id == "000a0137a4"
    assert horse.pedigree.damsire is not None
    assert horse.pedigree.damsire.name == "Vindication (米)"
    assert horse.pedigree.damsire.horse_id == "000a010544"
    assert horse.pedigree.warning is None
    assert horse.pedigree.source == PEDIGREE_SOURCE_PED
    assert horse.sire_id == "2001103038"
    style = horse.running_style
    assert style is not None
    assert style.sample_size > 0
    assert set(style.counts) == {"逃げ", "先行", "差し", "追込"}
    if style.label is not None:
        assert style.label in {"逃げ", "先行", "差し", "追込"}
    assert horse.track_bias.inside_outside is None
    assert horse.track_bias.pace_bias is None
    assert horse.track_bias.warning == TRACK_BIAS_UNAVAILABLE
