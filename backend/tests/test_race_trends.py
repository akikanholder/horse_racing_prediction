"""Race-trend tables over settled history. Missing prices stay null."""

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from backend.cli import main as cli_main
from backend.main import app
from backend.models.historical import HistoricalBatch, HistoricalRace
from backend.models.horse import PEDIGREE_FORBIDDEN, Pedigree, PedigreeHorse
from backend.models.odds import OddsQuote
from backend.models.race import Race, RacePayout, SettledFinisher, SettledRaceResult
from backend.models.race_trends import (
    EMPTY_POPULARITY_BAND,
    EMPTY_RUNNING_STYLE,
    FETCHED_AT_DIFFERS,
    FINAL_WIN_ODDS_MISSING,
    PLACE_NOT_SOLD,
    PLACE_RANGE_UNUSED,
    RESULT_PHASE_PROVISIONAL,
    WIN_ODDS_PAYOUT_DISAGREE,
    TrendFilter,
)
from backend.models.running_style import STYLE_BASIS
from backend.scrapers.final_odds import PublicOddsPages
from backend.scrapers.jra_payout import JRA_PAYOUT_UNREACHABLE
from backend.scrapers.jra_scraper import JRAScraper
from backend.scrapers.netkeiba_odds_scraper import NetkeibaOddsScraper
from backend.scrapers.netkeiba_scraper import NetkeibaScraper
from backend.scrapers.page_cache import CachingHTTPClient
from backend.scrapers.page_fallback import AccessBlocked
from backend.services.historical_service import HistoricalService
from backend.services.race_service import RaceService
from backend.services.race_trend_aggregate import aggregate_race_trends
from backend.services.race_trend_service import RaceTrendService, previous_meeting_date

FETCHED = datetime(2026, 10, 12, 9, 0, 0)
LATER = datetime(2026, 10, 12, 11, 0, 0)
RACE_DAY = date(2025, 6, 8)


def _quote(
    race_id: str,
    horse_number: int,
    odds: float | None,
    *,
    bet_type: str = "win",
    odds_min: float | None = None,
    odds_max: float | None = None,
) -> OddsQuote:
    return OddsQuote(
        race_id=race_id,
        bet_type=bet_type,  # type: ignore[arg-type]
        combination=str(horse_number),
        odds=odds,
        odds_min=odds_min,
        odds_max=odds_max,
        source="netkeiba_html",
        kind="live",
        fetched_at=FETCHED,
    )


def _payout(bet_type: str, horse_number: int, payout: int) -> RacePayout:
    return RacePayout(
        bet_type=bet_type,
        combination=str(horse_number),
        payout=payout,
        popularity=1,
        source="netkeiba_html",
        fetched_at=FETCHED,
    )


def _finisher(
    number: int,
    *,
    rank: int | None,
    odds: float = 2.0,
    popularity: int | None = 1,
    bracket: int | None = 1,
    jockey: str | None = "ルメール",
    horse_id: str | None = None,
    passing: str | None = "1-1",
    status: str | None = "finished",
) -> SettledFinisher:
    return SettledFinisher(
        rank=rank,
        status=status,  # type: ignore[arg-type]
        bracket_number=bracket,
        horse_number=number,
        horse_name=f"馬{number}",
        horse_id=horse_id or f"{number:010d}",
        jockey_name=jockey,
        popularity=popularity,
        odds=odds,
        passing_order=passing,
        source="netkeiba_html",
        fetched_at=FETCHED,
    )


def _race(
    race_id: str = "202505030801",
    runners: list[SettledFinisher] | None = None,
    *,
    surface: str | None = "芝",
    distance: int | None = 1600,
    race_class: str | None = "未勝利",
    going: str | None = "良",
    venue: str | None = "東京",
    race_date: date | None = RACE_DAY,
    phase: str | None = "confirmed",
    complete: bool = True,
    odds_final: bool = True,
    quotes: list[OddsQuote] | None = None,
    payouts: list[RacePayout] | None = None,
    warning: str | None = None,
    source: str | None = "netkeiba_html",
    fetched_at: datetime = FETCHED,
    field_size: int | None = None,
    flat: bool | None = True,
) -> HistoricalRace:
    field = runners if runners is not None else [_finisher(1, rank=1, odds=1.5)]
    if quotes is None and odds_final:
        quotes = [
            _quote(race_id, row.horse_number, row.odds)
            for row in field
            if row.horse_number is not None and row.status not in {"scratched", "excluded"}
        ]
    if payouts is None:
        payouts = []
        for row in field:
            if row.rank == 1 and row.horse_number is not None and row.odds is not None:
                payouts.append(_payout("単勝", row.horse_number, int(row.odds * 100)))
    settled = SettledRaceResult(
        race_id=race_id,
        race_date=race_date,
        venue=venue,
        distance=distance,
        track_type=surface,
        track_condition=going,
        race_class=race_class,
        result_phase=phase,  # type: ignore[arg-type]
        complete=complete,
        results=field,
        payouts=payouts,
        source=source,
        fetched_at=fetched_at,
        warning=warning,
    )
    return HistoricalRace(
        race_id=race_id,
        surface=surface,  # type: ignore[arg-type]
        distance=distance,
        race_class=race_class,
        field_size=field_size if field_size is not None else len(field),
        going=going,
        flat=flat,
        in_scope=True,
        result=settled,
        final_odds=quotes,
        non_runners=[],
        odds_final=odds_final,
        source=source,
        fetched_at=fetched_at,
        warning=warning,
    )


def _filter(**kwargs: object) -> TrendFilter:
    return TrendFilter(**kwargs)  # type: ignore[arg-type]


def _report(races: list[HistoricalRace], **kwargs: object):
    filt = kwargs.pop("filt", None)
    return aggregate_race_trends(
        races,
        filt=filt if isinstance(filt, TrendFilter) else _filter(**kwargs),  # type: ignore[arg-type]
        computed_at=FETCHED,
        sires=kwargs.pop("sires", {}),  # type: ignore[arg-type]
        sire_unfetched=kwargs.pop("sire_unfetched", {}),  # type: ignore[arg-type]
        extra_excluded=kwargs.pop("extra_excluded", ()),  # type: ignore[arg-type]
    )


def test_popularity_bands_keep_rates_and_do_not_invent_empty_ones():
    winner = _finisher(1, rank=1, odds=1.5, popularity=1, passing="1-1")
    second = _finisher(2, rank=2, odds=4.0, popularity=2, passing="2-2")
    report = _report(
        [_race(runners=[winner, second], field_size=2)],
        date_from=RACE_DAY,
        date_to=RACE_DAY,
    )
    bands = {row.band: row for row in report.by_popularity}
    assert bands["1"].sample_size == 1
    assert bands["1"].win_rate == 1.0
    assert bands["1"].top2_rate == 1.0
    assert bands["1"].win_roi == 0.5
    assert bands["2"].sample_size == 1
    assert bands["2"].win_rate == 0.0
    assert bands["2"].top2_rate == 1.0
    assert bands["2"].top3_rate == 1.0
    assert bands["2"].win_roi == -1.0
    assert bands["3"].sample_size == 0
    assert bands["3"].win_rate is None
    assert bands["3"].win_roi is None
    assert bands["3"].warning == EMPTY_POPULARITY_BAND
    assert bands["10+"].sample_size == 0
    assert report.sample_size == 1
    assert report.runner_sample_size == 2
    assert report.source == "netkeiba_html"
    assert report.fetched_at == FETCHED
    assert report.computed_at == FETCHED
    assert report.stake_yen == 100


def test_place_roi_uses_payouts_and_not_a_range_midpoint():
    runners = []
    quotes = []
    payouts = [_payout("単勝", 1, 200)]
    for number in range(1, 6):
        runners.append(
            _finisher(
                number,
                rank=number,
                odds=2.0 if number == 1 else 10.0,
                popularity=1,
                bracket=number,
                passing=f"{number}-{number}",
                jockey="武豊",
            )
        )
        quotes.append(_quote("202505030801", number, 2.0 if number == 1 else 10.0))
    quotes.append(_quote("202505030801", 1, None, bet_type="place", odds_min=1.1, odds_max=1.9))
    payouts.append(_payout("複勝", 2, 180))
    report = _report(
        [
            _race(
                runners=runners,
                quotes=quotes,
                payouts=payouts,
                field_size=5,
            )
        ]
    )
    band = report.by_popularity[0]
    assert band.band == "1"
    assert band.sample_size == 5
    assert band.win_rate == 0.2
    assert band.top2_rate == 0.4
    assert band.top3_rate == 0.6
    assert band.win_roi == -0.6
    assert band.place_stake_count == 4
    assert band.place_roi == -0.55
    assert band.warning is not None
    assert PLACE_RANGE_UNUSED in band.warning
    assert report.place_basis
    assert "中央値" in report.place_basis


def test_place_roi_is_null_when_fewer_than_five_start():
    report = _report([_race(runners=[_finisher(1, rank=1), _finisher(2, rank=2)], field_size=2)])
    assert report.by_popularity[0].place_roi is None
    assert report.by_popularity[0].warning is not None
    assert PLACE_NOT_SOLD in report.by_popularity[0].warning


def test_disagreement_between_win_payout_and_odds_is_not_averaged():
    runner = _finisher(1, rank=1, odds=2.0, popularity=1)
    report = _report(
        [
            _race(
                runners=[runner],
                quotes=[_quote("202505030801", 1, 2.0)],
                payouts=[_payout("単勝", 1, 210)],
            )
        ]
    )
    band = report.by_popularity[0]
    assert band.win_rate == 1.0
    assert band.win_roi is None
    assert band.win_stake_count == 0
    assert band.warning is not None
    assert WIN_ODDS_PAYOUT_DISAGREE in band.warning


def test_missing_final_win_odds_and_403_are_excluded():
    missing = _race("202505030802", odds_final=False, quotes=[], payouts=[], warning=None)
    blocked = _race(
        "202505030803",
        runners=[],
        odds_final=False,
        quotes=[],
        payouts=[],
        warning="race result returned 403",
    )
    report = _report([missing, blocked, _race()])
    reasons = {item.race_id: item.reason for item in report.excluded}
    assert reasons["202505030802"] == FINAL_WIN_ODDS_MISSING
    assert reasons["202505030803"] == "race result returned 403"
    assert report.sample_size == 1
    assert report.races_pending == 0
    assert "202505030801" in report.included_race_ids


def test_filter_mismatch_is_not_listed_as_excluded():
    other = _race("202505030802", distance=1800)
    unknown = _race("202505030803", distance=None)
    report = _report([_race(), other, unknown], distance=1600)
    assert report.included_race_ids == ["202505030801"]
    assert [item.race_id for item in report.excluded] == ["202505030803"]
    assert report.excluded[0].reason == "distance was not on the page"


def test_gate_and_frame_keep_a_single_runner():
    runner = _finisher(7, rank=1, odds=3.0, popularity=1, bracket=4, passing="4-4")
    report = _report([_race(runners=[runner], field_size=8)])
    assert [row.horse_number for row in report.by_gate] == [7]
    assert report.by_gate[0].sample_size == 1
    assert report.by_gate[0].win_roi == 2.0
    assert [row.bracket_number for row in report.by_frame] == [4]
    assert report.by_frame[0].sample_size == 1
    assert all(row.horse_number != 9 for row in report.by_gate)


def test_running_style_uses_the_first_call_and_keeps_small_samples():
    # Field of 12: front_end = 4, mid_end = 8.
    # 4-1 is 先行. The 4th-corner 1 is not 逃げ.
    runners = [
        _finisher(1, rank=1, odds=2.0, popularity=1, passing="1-9"),
        _finisher(4, rank=2, odds=3.0, popularity=2, passing="4-1"),
        _finisher(5, rank=3, odds=4.0, popularity=3, passing="5-5"),
        _finisher(8, rank=4, odds=8.0, popularity=4, passing="8-8"),
        _finisher(9, rank=5, odds=12.0, popularity=5, passing="9-9"),
    ]
    report = _report([_race(runners=runners, field_size=12)])
    definition = report.running_style_definition
    assert definition.basis == STYLE_BASIS
    assert "4区分" in definition.position
    assert "4コーナー" in definition.position
    assert "1" in definition.escape
    assert "逃げ" in definition.escape
    assert "ceil(field_size / 3)" in definition.front_end
    assert "先行" in definition.front_end
    assert "ceil(field_size * 2 / 3)" in definition.mid_end
    assert "差し" in definition.mid_end
    assert "ceil(field_size * 2 / 3)" in definition.closer
    assert "追込" in definition.closer
    styles = {row.label: row for row in report.by_running_style}
    assert list(styles) == ["逃げ", "先行", "差し", "追込"]
    assert styles["逃げ"].sample_size == 1
    assert styles["先行"].sample_size == 1
    assert styles["差し"].sample_size == 2
    assert styles["追込"].sample_size == 1
    assert styles["逃げ"].win_rate == 1.0
    assert "落とさない" in definition.unusable


def test_running_style_leaves_front_empty_when_the_third_starts_at_one():
    # Field of 3: front_end = 1, so the leader is 逃げ and position 2 is 差し.
    runners = [
        _finisher(1, rank=1, odds=2.0, popularity=1, passing="1-1"),
        _finisher(2, rank=2, odds=3.0, popularity=2, passing="2-2"),
        _finisher(3, rank=3, odds=4.0, popularity=3, passing="3-3"),
    ]
    report = _report([_race(runners=runners, field_size=3)])
    styles = {row.label: row for row in report.by_running_style}
    assert styles["逃げ"].sample_size == 1
    assert styles["先行"].sample_size == 0
    assert styles["先行"].win_rate is None
    assert styles["先行"].warning == EMPTY_RUNNING_STYLE
    assert styles["差し"].sample_size == 1
    assert styles["追込"].sample_size == 1


def test_scratched_runner_is_outside_the_sample():
    scratch = _finisher(2, rank=None, status="scratched", odds=None, popularity=None, passing=None)
    report = _report(
        [
            _race(
                runners=[_finisher(1, rank=1, odds=1.5), scratch],
                quotes=[_quote("202505030801", 1, 1.5)],
            )
        ]
    )
    assert report.runner_sample_size == 1
    assert {item.reason: item.count for item in report.runner_omissions}[
        "scratched or excluded"
    ] == 1


def test_jockey_and_sire_are_split_by_surface():
    turf = _race(
        "202505030801",
        runners=[_finisher(1, rank=1, odds=2.0, jockey="ルメール", horse_id="h1")],
        surface="芝",
    )
    dirt = _race(
        "202505030802",
        runners=[_finisher(1, rank=2, odds=5.0, jockey="ルメール", horse_id="h2", passing="4-4")],
        surface="ダート",
    )
    sires = {
        "h1": Pedigree(
            sire=PedigreeHorse(name="ディープインパクト", horse_id="s1"),
            dam=PedigreeHorse(name="母", horse_id="d1"),
            source="netkeiba_ped",
        ),
        "h2": Pedigree(sire=None, dam=None, warning="pedigree not on profile or ped page"),
    }
    report = _report([turf, dirt], sires=sires)
    jockeys = {(row.jockey_name, row.surface): row for row in report.by_jockey}
    assert set(jockeys) == {("ルメール", "芝"), ("ルメール", "ダート")}
    assert jockeys[("ルメール", "芝")].win_rate == 1.0
    assert jockeys[("ルメール", "ダート")].win_rate == 0.0
    assert jockeys[("ルメール", "芝")].sample_size == 1
    assert len(report.by_sire) == 1
    assert report.by_sire[0].sire_name == "ディープインパクト"
    assert report.by_sire[0].surface == "芝"
    assert report.by_sire[0].pedigree_source == "netkeiba_ped"
    omissions = {item.reason: item.count for item in report.runner_omissions}
    assert omissions["pedigree not on profile or ped page"] == 1


def test_jump_race_is_outside_the_turf_and_dirt_splits():
    jump = _race(
        "202608040604",
        runners=[_finisher(1, rank=1, odds=3.0, jockey="ルメール", horse_id="h1")],
        surface=None,
        flat=False,
    )
    turf = _race(
        "202505030801",
        runners=[_finisher(1, rank=1, odds=2.0, jockey="ルメール", horse_id="h2")],
        surface="芝",
    )
    report = _report([jump, turf])
    assert {(row.jockey_name, row.surface) for row in report.by_jockey} == {("ルメール", "芝")}
    assert all(row.sample_size == 1 for row in report.by_jockey)
    assert report.by_sire == [] or all(row.surface in {"芝", "ダート"} for row in report.by_sire)


def test_sire_id_missing_stays_null():
    race = _race(runners=[_finisher(1, rank=1, horse_id="h1")])
    report = _report(
        [race],
        sires={
            "h1": Pedigree(
                sire=PedigreeHorse(name="父馬", horse_id=None),
                dam=PedigreeHorse(name="母", horse_id="d1"),
                source="netkeiba_ped",
            )
        },
    )
    assert report.by_sire[0].sire_id is None
    assert report.by_sire[0].sire_name == "父馬"
    assert report.by_sire[0].warning is not None
    assert "sire id was not on the pedigree page" in report.by_sire[0].warning


def test_fetched_at_is_null_when_the_races_disagree():
    first = _race("202505030801", fetched_at=FETCHED)
    second = _race("202505030802", fetched_at=LATER)
    report = _report([first, second])
    assert report.fetched_at is None
    assert report.fetched_at_min == FETCHED
    assert report.fetched_at_max == LATER
    assert report.warning is not None
    assert FETCHED_AT_DIFFERS in report.warning


def test_provisional_race_is_excluded():
    report = _report([_race(phase="provisional")])
    assert report.sample_size == 0
    assert report.races_pending == 1
    assert report.excluded[0].reason == RESULT_PHASE_PROVISIONAL
    assert report.warning == "1 race is not final and was left out: 202505030801"
    assert report.by_popularity[0].sample_size == 0
    assert report.by_popularity[0].win_rate is None


def test_pending_warning_names_every_race_id():
    report = _report(
        [
            _race(phase="provisional"),
            _race("202505030802", complete=False),
            _race("202505030803"),
        ]
    )
    assert report.races_pending == 2
    assert report.sample_size == 1
    assert report.warning == ("2 races are not final and were left out: 202505030801, 202505030802")


def test_tables_split_by_surface_when_surface_is_omitted():
    turf = _race(
        "202605040801",
        runners=[_finisher(1, rank=1, odds=2.4, popularity=2, bracket=2, passing="1-1")],
        surface="芝",
    )
    dirt = _race(
        "202605040802",
        runners=[_finisher(3, rank=1, odds=3.1, popularity=1, bracket=4, passing="3-3")],
        surface="ダート",
    )
    report = _report([turf, dirt])
    assert [item.surface for item in report.by_surface] == ["芝", "ダート"]
    turf_pop = {row.band: row.sample_size for row in report.by_surface[0].by_popularity}
    dirt_pop = {row.band: row.sample_size for row in report.by_surface[1].by_popularity}
    assert turf_pop["2"] == 1
    assert turf_pop["1"] == 0
    assert dirt_pop["1"] == 1
    assert dirt_pop["2"] == 0
    combined = {row.band: row.sample_size for row in report.by_popularity}
    assert combined["1"] == 1
    assert combined["2"] == 1
    turf_gate = {row.horse_number: row.sample_size for row in report.by_surface[0].by_gate}
    dirt_frame = {row.bracket_number: row.sample_size for row in report.by_surface[1].by_frame}
    assert turf_gate == {1: 1}
    assert dirt_frame == {4: 1}
    assert {row.horse_number for row in report.by_gate} == {1, 3}
    only = _report([turf, dirt], surface="芝")
    assert [item.surface for item in only.by_surface] == ["芝"]
    assert only.sample_size == 1
    assert {row.band: row.sample_size for row in only.by_popularity}["2"] == 1


def test_confirmed_but_incomplete_race_is_pending_and_not_counted():
    report = _report([_race(complete=False), _race("202505030802")])
    assert report.sample_size == 1
    assert report.included_race_ids == ["202505030802"]
    assert report.included_races[0].source == "netkeiba_html"
    assert report.included_races[0].fetched_at == FETCHED
    assert report.races_pending == 1
    reasons = {item.race_id: item.reason for item in report.excluded}
    assert reasons["202505030801"] == "race result is not complete"
    assert report.by_running_style[0].sample_size == 1


def test_previous_meeting_stops_on_403_without_opening_the_day_before():
    calls: list[date] = []

    def list_races(day: date) -> list[Race]:
        calls.append(day)
        if day == date(2025, 6, 7):
            raise AccessBlocked(403, "blocked")
        raise AssertionError(day)

    found, warning, blocked = previous_meeting_date(
        list_races, venue="東京", as_of=date(2025, 6, 8)
    )
    assert found is None
    assert blocked is True
    assert warning == "race list returned 403"
    assert calls == [date(2025, 6, 7)]


def test_previous_meeting_is_the_latest_day_at_the_venue():
    calls: list[date] = []

    def list_races(day: date) -> list[Race]:
        calls.append(day)
        if day == date(2025, 6, 7):
            return [
                Race(
                    race_id="202505030701",
                    race_name="テスト",
                    race_number=1,
                    race_date=day,
                    venue="東京",
                    distance=1600,
                    track_type="芝",
                )
            ]
        return []

    found, warning, blocked = previous_meeting_date(
        list_races, venue="東京", as_of=date(2025, 6, 8)
    )
    assert found == date(2025, 6, 7)
    assert warning is None
    assert blocked is False
    assert calls == [date(2025, 6, 7)]


def test_recent_meeting_uses_confirmed_races_and_does_not_fetch_after_a_block():
    as_of = date(2025, 6, 8)
    previous = date(2025, 6, 7)
    today_race = _race("202505030801", race_date=as_of)
    provisional = _race("202505030802", race_date=as_of, phase="provisional")
    previous_race = _race("202505030701", race_date=previous, surface="ダート")
    batches = {
        previous: _batch([previous_race], day=previous),
        as_of: _batch([today_race, provisional], day=as_of),
    }
    historical = _Hist(batches)

    def list_races(day: date) -> list[Race]:
        if day == previous:
            return [
                Race(
                    race_id="202505030701",
                    race_name="テスト",
                    race_number=1,
                    race_date=day,
                    venue="東京",
                    distance=1600,
                    track_type="ダート",
                )
            ]
        return []

    service = RaceTrendService(
        historical=historical,  # type: ignore[arg-type]
        list_races=list_races,
        pedigree_lookup=lambda _horse_id: Pedigree(warning="pedigree fetch failed"),
        clock=lambda: as_of,
    )
    report = service.get(venues=["東京"], recent_meeting=True, as_of=as_of, sires={})
    assert [call["on_date"] for call in historical.calls] == [previous, as_of]
    assert report.recent_meeting is not None
    assert report.recent_meeting.previous_meeting_date == previous
    assert report.included_race_ids == ["202505030701", "202505030801"]
    assert report.excluded[0].race_id == "202505030802"
    assert report.mode == "recent_meeting"


def test_recent_meeting_does_not_open_races_after_a_list_403():
    historical = _Hist({})

    def list_races(day: date) -> list[Race]:
        raise AccessBlocked(403, "blocked")

    service = RaceTrendService(
        historical=historical,  # type: ignore[arg-type]
        list_races=list_races,
        clock=lambda: date(2025, 6, 8),
    )
    report = service.get(venues=["東京"], recent_meeting=True, as_of=date(2025, 6, 8))
    assert historical.calls == []
    assert report.sample_size == 0
    assert report.warning is not None
    assert "race list returned 403" in report.warning
    assert report.by_popularity[0].win_rate is None


def test_pedigree_403_stops_later_horses():
    first = _race(
        "202505030801",
        runners=[
            _finisher(1, rank=1, horse_id="aaa", jockey="A"),
            _finisher(2, rank=2, odds=3.0, horse_id="bbb", jockey="B", popularity=2),
        ],
    )
    second = _race(
        "202505030802",
        runners=[_finisher(1, rank=1, horse_id="ccc", jockey="C")],
    )
    calls: list[str] = []

    def lookup(horse_id: str) -> Pedigree:
        calls.append(horse_id)
        if horse_id == "bbb":
            return Pedigree(warning=PEDIGREE_FORBIDDEN)
        return Pedigree(
            sire=PedigreeHorse(name=f"父{horse_id}", horse_id=f"s-{horse_id}"),
            dam=PedigreeHorse(name="母", horse_id="d"),
            source="netkeiba_ped",
        )

    service = RaceTrendService(
        historical=_Hist(  # type: ignore[arg-type]
            {
                RACE_DAY: _batch([first, second], day=RACE_DAY),
            }
        ),
        pedigree_lookup=lookup,
    )
    report = service.get(on_date=RACE_DAY, venues=["東京"])
    assert calls == ["aaa", "bbb"]
    names = {row.sire_name for row in report.by_sire}
    assert names == {"父aaa"}
    assert report.warning is not None
    assert PEDIGREE_FORBIDDEN in report.warning
    omissions = {item.reason for item in report.runner_omissions}
    assert any(PEDIGREE_FORBIDDEN in reason for reason in omissions)


def test_races_left_unfetched_after_a_403_are_excluded():
    stopped = "202505030899"
    later = "202505030812"
    warning = f"stopped at {stopped}: race result returned 403"
    batch = _batch(
        [_race()],
        day=RACE_DAY,
        warning=warning,
        remaining=[stopped, later],
        stopped=stopped,
    )
    service = RaceTrendService(
        historical=_Hist({RACE_DAY: batch}),  # type: ignore[arg-type]
        pedigree_lookup=lambda _horse_id: Pedigree(warning="pedigree fetch failed"),
    )
    report = service.get(on_date=RACE_DAY, venues=["東京"])
    reasons = {item.race_id: item.reason for item in report.excluded}
    assert reasons[stopped] == warning
    assert reasons[later] == f"not fetched: {warning}"
    assert report.sample_size == 1


def test_recent_meeting_requires_one_venue():
    service = RaceTrendService(
        historical=_Hist({}),  # type: ignore[arg-type]
        clock=lambda: RACE_DAY,
    )
    report = service.get(recent_meeting=True, as_of=RACE_DAY)
    assert report.sample_size == 0
    assert report.warning is not None
    assert "recent meeting requires one venue" in report.warning


def test_cli_trends_json(monkeypatch, capsys):
    expected = _report([_race()])

    class _Service:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get_race_trends(self, **kwargs):
            assert kwargs["surface"] == "芝"
            assert kwargs["distance"] == 1600
            assert kwargs["race_class"] == "未勝利"
            assert kwargs["going"] == "良"
            assert kwargs["venues"] == ["東京"]
            return expected

    monkeypatch.setattr("backend.cli.RaceService", lambda: _Service())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backend.cli",
            "trends",
            "--date",
            "2025-06-08",
            "--venue",
            "東京",
            "--surface",
            "芝",
            "--distance",
            "1600",
            "--class",
            "未勝利",
            "--going",
            "良",
            "--json",
        ],
    )
    cli_main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["sample_size"] == 1
    assert payload["by_popularity"][0]["win_rate"] == 1.0
    assert payload["running_style_definition"]["basis"] == STYLE_BASIS
    assert payload["fetched_at"]
    assert payload["computed_at"]


def test_trends_api_returns_the_report_and_requires_a_date(monkeypatch):
    expected = _report([_race()])
    captured: dict[str, object] = {}

    def _fake(self, **kwargs):
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(RaceService, "get_race_trends", _fake)
    with TestClient(app) as client:
        missing = client.get("/api/races/trends")
        response = client.get(
            "/api/races/trends",
            params={
                "date_from": "2025-06-01",
                "date_to": "2025-06-08",
                "venue": "東京",
                "surface": "芝",
                "distance": 1600,
                "class": "未勝利",
                "going": "良",
            },
        )
    assert missing.status_code == 400
    assert response.status_code == 200
    body = response.json()
    assert body["sample_size"] == 1
    assert body["by_gate"][0]["sample_size"] == 1
    assert body["running_style_definition"]["front_end"]
    assert captured["race_class"] == "未勝利"
    assert captured["date_from"] == date(2025, 6, 1)
    assert captured["today"] is False


def test_cli_today_and_yyyymmdd_are_one_meeting_day(monkeypatch, capsys):
    expected = _report([_race()])
    seen: list[dict[str, object]] = []

    class _Service:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get_race_trends(self, **kwargs):
            seen.append(kwargs)
            return expected

    monkeypatch.setattr("backend.cli.RaceService", lambda: _Service())
    monkeypatch.setattr(
        sys, "argv", ["backend.cli", "trends", "--today", "--venue", "東京", "--json"]
    )
    cli_main()
    assert seen[-1]["today"] is True
    assert seen[-1]["on_date"] is None
    assert seen[-1]["venues"] == ["東京"]
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["backend.cli", "trends", "--date", "20261018", "--json"])
    cli_main()
    assert seen[-1]["today"] is False
    assert seen[-1]["on_date"] == date(2026, 10, 18)
    payload = json.loads(capsys.readouterr().out)
    assert payload["sample_size"] == 1


def test_trends_api_accepts_today_and_yyyymmdd(monkeypatch):
    expected = _report([_race()])
    captured: list[dict[str, object]] = []

    def _fake(self, **kwargs):
        captured.append(kwargs)
        return expected

    monkeypatch.setattr(RaceService, "get_race_trends", _fake)
    with TestClient(app) as client:
        conflict = client.get("/api/races/trends", params={"today": "true", "date": "20261018"})
        compact = client.get("/api/races/trends", params={"date": "20261018", "venue": "東京"})
        today = client.get("/api/races/trends", params={"today": "true"})
        bad = client.get("/api/races/trends", params={"date": "18-10-2026"})
        span = client.get(
            "/api/races/trends",
            params={"date_from": "20250601", "date_to": "20250608"},
        )
        bad_from = client.get("/api/races/trends", params={"date_from": "2025-13-40"})
    assert conflict.status_code == 400
    assert bad.status_code == 400
    assert bad_from.status_code == 400
    assert compact.status_code == 200
    assert today.status_code == 200
    assert span.status_code == 200
    assert captured[0]["on_date"] == date(2026, 10, 18)
    assert captured[0]["today"] is False
    assert captured[0]["venues"] == ["東京"]
    assert captured[1]["today"] is True
    assert captured[1]["on_date"] is None
    assert captured[2]["date_from"] == date(2025, 6, 1)
    assert captured[2]["date_to"] == date(2025, 6, 8)


def test_today_uses_asia_tokyo_and_refuses_another_date():
    historical = _Hist(
        {date(2026, 10, 18): _batch([_race(race_date=date(2026, 10, 18))], day=date(2026, 10, 18))}
    )
    service = RaceTrendService(
        historical=historical,  # type: ignore[arg-type]
        clock=lambda: date(2026, 10, 18),
    )
    report = service.get(today=True, venues=["東京"], sires={})
    assert historical.calls[0]["on_date"] == date(2026, 10, 18)
    assert report.filter.today is True
    assert report.filter.date_from == date(2026, 10, 18)
    assert report.filter.date_to == date(2026, 10, 18)
    assert report.sample_size == 1
    blocked = RaceTrendService(
        historical=historical,  # type: ignore[arg-type]
        clock=lambda: date(2026, 10, 18),
    )
    refused = blocked.get(today=True, on_date=date(2026, 10, 18), sires={})
    assert refused.sample_size == 0
    assert refused.warning is not None
    assert "does not take another date" in refused.warning
    assert len(historical.calls) == 1


def test_same_day_rerun_includes_a_race_that_settles_later(tmp_path: Path, monkeypatch):
    meeting = date(2026, 10, 18)
    race_id = "202605040801"
    monkeypatch.setattr(
        "backend.scrapers.page_cache.tokyo_now",
        lambda: datetime(2026, 10, 18, 16, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
    )
    fixtures = Path(__file__).parent / "fixtures"
    unsettled = (
        (fixtures / "netkeiba_race_result_unsettled.html")
        .read_text(encoding="utf-8")
        .replace("20261003", "20261018")
        .replace("2026年10月3日", "2026年10月18日")
    )
    confirmed = """<!DOCTYPE html><html><head>
<title>未勝利 結果・払戻 | 2026年10月18日 東京1R レース情報(JRA) - netkeiba</title>
</head><body>
<div class="RaceList_Date"><dl id="RaceList_DateList"><dd class="Active">
<a href="../top/race_list.html?kaisai_date=20261018">10月18日</a></dd></dl></div>
<div class="RaceList_Item02">
<h1 class="RaceName">未勝利</h1>
<p class="RaceResultState">確定</p>
<div class="RaceData01"><span>芝1600m</span> / 天候:晴 <span class="Item03">/ 馬場:良</span></div>
<div class="RaceData02"><span>4回</span><span>東京</span><span>8日目</span>
<span>未勝利</span><span>1頭</span></div>
</div>
<table summary="全着順" class="Table_Show_All"><tr>
<th>着順</th><th>枠</th><th>馬番</th><th>馬名</th><th>斤量</th><th>騎手</th>
<th>タイム</th><th>着差</th><th>人気</th><th>単勝オッズ</th><th>後3F</th><th>コーナー通過順</th>
</tr>
<tr class="HorseList">
<td class="Result_Num"><div class="Rank">1</div></td>
<td><div>1</div></td><td><div>1</div></td>
<td class="Horse_Info"><a href="https://db.netkeiba.com/horse/0000000001">アルファ</a></td>
<td>55.0</td><td>ルメール</td><td>1:33.0</td><td></td><td>1</td><td>2.4</td><td>34.0</td><td>1-1</td>
</tr>
</table>
<table class="Payout_Detail_Table"><tr><th>単勝</th>
<td class="Result"><div><span>1</span></div></td>
<td class="Payout"><span>240円</span></td><td class="Ninki"><span>1人気</span></td></tr>
</table>
</body></html>"""
    listing = """
    <dl>
      <dt><p class="RaceList_DataTitle"><small>4回</small> 東京 <small>8日目</small></p></dt>
      <dd><ul>
        <li class="RaceList_DataItem">
          <a href="../race/shutuba.html?race_id=202605040801">
            <span class="ItemTitle">未勝利</span>
            <div class="RaceData"><span>芝1600m</span><span class="RaceList_Itemnumber">1頭</span></div>
          </a>
        </li>
      </ul></dd>
    </dl>
    """
    pages = {"result": unsettled}
    result_gets: list[str] = []

    class _Transport:
        def get(self, url, params=None, headers=None, encoding=None):
            if "result.html" in url:
                result_gets.append((params or {}).get("race_id") or "")
                return pages["result"]
            if "race_list" in url:
                return listing
            return "<html></html>"

        def post(self, url, data=None, headers=None, encoding=None):
            return "<html></html>"

    http = CachingHTTPClient(tmp_path, transport=_Transport(), min_request_interval=0)

    class _Odds:
        def _snapshot(self, bet_type: str):
            from backend.models.odds import RaceOdds

            return RaceOdds(
                race_id=race_id,
                bet_types=[bet_type],  # type: ignore[list-item]
                prefer="live",
                status="unavailable",
                reason="result odds empty",
                race_phase="pre",
                quotes=[],
                fetched_at=FETCHED,
            )

        def get_win_odds(self, race_id, fetched_at=None):
            return self._snapshot("win")

        def get_place_odds(self, race_id, fetched_at=None):
            return self._snapshot("place")

        def get_wide_odds(self, race_id, fetched_at=None):
            return self._snapshot("wide")

        def get_umaren_odds(self, race_id, fetched_at=None):
            return self._snapshot("umaren")

        def get_sanrenpuku_odds(self, race_id, fetched_at=None):
            return self._snapshot("sanrenpuku")

        def close(self):
            return None

    class _Jra:
        def fetch_odds_pages(self, race_id, fetched_at=None):
            return PublicOddsPages([], "JRA odds page was not on the card", False)

        def close(self):
            return None

    def _open() -> RaceTrendService:
        history = HistoricalService(
            netkeiba=NetkeibaScraper(http_client=http, headless_render="off"),  # type: ignore[arg-type]
            jra=_Jra(),  # type: ignore[arg-type]
            odds=_Odds(),  # type: ignore[arg-type]
            delay_seconds=0,
        )
        return RaceTrendService(historical=history, clock=lambda: meeting)

    first = _open().get(today=True, venues=["東京"], sires={})
    assert first.sample_size == 0
    assert first.races_pending == 1
    assert first.included_races == []
    assert first.excluded[0].race_id == race_id
    assert first.excluded[0].reason == "race not yet settled"
    assert first.by_running_style[0].sample_size == 0
    assert result_gets == [race_id]

    pages["result"] = confirmed
    second = _open().get(today=True, venues=["東京"], sires={})
    assert result_gets == [race_id, race_id]
    assert second.sample_size == 1
    assert second.races_pending == 0
    assert second.included_race_ids == [race_id]
    assert second.included_races[0].source
    assert second.included_races[0].fetched_at is not None
    escape = next(row for row in second.by_running_style if row.label == "逃げ")
    assert escape.sample_size == 1
    assert escape.wins == 1

    third = _open().get(today=True, venues=["東京"], sires={})
    assert result_gets == [race_id, race_id]
    assert third.included_race_ids == [race_id]


def test_midday_rerun_refetches_today_jra_payout_odds_and_card(tmp_path: Path, monkeypatch):
    """A morning payout page and pre-race odds must not be replayed after noon.

    The JRA scraper and the odds API client are the real ones. Only the HTTP
    transport is a fixture. The clock is 2026-10-18 in Asia/Tokyo, so the
    year+kai+day digits inside the cname are not treated as a past meeting.
    """
    meeting = date(2026, 10, 18)
    race_id = "202605040801"
    monkeypatch.setattr(
        "backend.scrapers.page_cache.tokyo_now",
        lambda: datetime(2026, 10, 18, 12, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
    )
    day_cname = "pw01hde10052026040820261018/A4"
    meeting_cname = "pw01drl10052026040820261018/AB"
    card_cname = "pw01dde1005202604080120261018/AB"
    odds_cname = "pw01ode0105202604080120261018/AB"
    phase = {"payout": "morning", "odds": "middle"}
    payout_posts: list[str] = []
    odds_posts: list[str] = []
    card_posts: list[str] = []
    odds_api: list[str] = []
    confirmed = """<!DOCTYPE html><html><head>
<title>未勝利 結果・払戻 | 2026年10月18日 東京1R レース情報(JRA) - netkeiba</title>
</head><body>
<div class="RaceList_Item02">
<h1 class="RaceName">未勝利</h1>
<p class="RaceResultState">確定</p>
<div class="RaceData01"><span>芝1600m</span> / 天候:晴 <span class="Item03">/ 馬場:良</span></div>
<div class="RaceData02"><span>4回</span><span>東京</span><span>8日目</span>
<span>未勝利</span><span>1頭</span></div>
</div>
<table summary="全着順" class="Table_Show_All"><tr>
<th>着順</th><th>枠</th><th>馬番</th><th>馬名</th><th>斤量</th><th>騎手</th>
<th>タイム</th><th>着差</th><th>人気</th><th>単勝オッズ</th><th>後3F</th><th>コーナー通過順</th>
</tr>
<tr class="HorseList">
<td class="Result_Num"><div class="Rank">1</div></td>
<td><div>1</div></td><td><div>1</div></td>
<td class="Horse_Info"><a href="https://db.netkeiba.com/horse/0000000001">アルファ</a></td>
<td>55.0</td><td>ルメール</td><td>1:33.0</td><td></td><td>1</td><td>2.4</td><td>34.0</td><td>1-1</td>
</tr>
</table>
<table class="Payout_Detail_Table"><tr><th>単勝</th>
<td class="Result"><div><span>1</span></div></td>
<td class="Payout"><span>240円</span></td><td class="Ninki"><span>1人気</span></td></tr>
</table>
</body></html>"""
    listing = """
    <dl>
      <dt><p class="RaceList_DataTitle"><small>4回</small> 東京 <small>8日目</small></p></dt>
      <dd><ul>
        <li class="RaceList_DataItem">
          <a href="../race/shutuba.html?race_id=202605040801">
            <span class="ItemTitle">未勝利</span>
            <div class="RaceData"><span>芝1600m</span><span class="RaceList_Itemnumber">1頭</span></div>
          </a>
        </li>
      </ul></dd>
    </dl>
    """
    morning_payout = '<img src="/img/race_number_main/race_num_1.png" alt="1R">'
    afternoon_payout = """
    <img src="/img/race_number_main/race_num_1.png" alt="1R">
    <div class="refund_unit"><ul><li><dl><dt>単勝</dt><dd>
      <div class="line"><span class="num">1</span><span class="yen">240円</span>
      <span class="pop">1番人気</span></div>
    </dd></dl></li></ul></div>
    """

    class _Transport:
        def get(self, url, params=None, headers=None, encoding=None):
            if "api_get_jra_odds.html" in url:
                bet = str((params or {}).get("type") or "")
                odds_api.append(bet)
                if phase["odds"] != "result":
                    return json.dumps({"status": "middle", "data": ""})
                if bet == "1":
                    return json.dumps({"status": "result", "data": {"odds": {"1": {"1": "2.4"}}}})
                return json.dumps({"status": "result", "data": {"odds": {}}})
            if "result.html" in url:
                return confirmed
            if "race_list" in url:
                return listing
            return "<html></html>"

        def post(self, url, data=None, headers=None, encoding=None):
            cname = str((data or {}).get("cname") or "")
            if "accessH.html" in url:
                payout_posts.append(cname)
                if cname == day_cname and phase["payout"] == "afternoon":
                    return afternoon_payout
                if cname == day_cname:
                    return morning_payout
                if cname == "pw01hli00/03":
                    return (
                        '<html><body><a onclick="doAction('
                        f"'/JRADB/accessH.html','{day_cname}');\">払戻</a></body></html>"
                    )
            if "accessD.html" in url:
                card_posts.append(cname)
                if cname == "pw01dli00/F3":
                    return (
                        '<html><body><a onclick="doAction('
                        f"'/JRADB/accessD.html','{meeting_cname}');\">開催</a></body></html>"
                    )
                if cname == meeting_cname:
                    return (
                        '<html><body><a onclick="doAction('
                        f"'/JRADB/accessD.html','{card_cname}');\">1R</a></body></html>"
                    )
                if cname == card_cname:
                    return (
                        '<html><body><a onclick="doAction('
                        f"'/JRADB/accessO.html','{odds_cname}');\">オッズ</a></body></html>"
                    )
            if "accessO.html" in url:
                odds_posts.append(cname)
                return '<table><tr><td class="odds">2.4</td></tr></table>'
            return "<html></html>"

    http = CachingHTTPClient(tmp_path, transport=_Transport(), min_request_interval=0)
    batches: list[HistoricalBatch] = []

    def _open() -> RaceTrendService:
        history = HistoricalService(
            netkeiba=NetkeibaScraper(http_client=http, headless_render="off"),
            jra=JRAScraper(http, headless_render="off", fetch_mode="html"),
            odds=NetkeibaOddsScraper(http),
            delay_seconds=0,
        )
        original = history.get

        def _record(**kwargs: object) -> HistoricalBatch:
            batch = original(**kwargs)  # type: ignore[arg-type]
            batches.append(batch)
            return batch

        history.get = _record  # type: ignore[method-assign]
        return RaceTrendService(historical=history, clock=lambda: meeting)

    first = _open().get(today=True, venues=["東京"], sires={})
    assert first.sample_size == 1
    assert first.included_race_ids == [race_id]
    assert batches[0].races[0].warning is not None
    assert JRA_PAYOUT_UNREACHABLE in batches[0].races[0].warning
    assert payout_posts.count(day_cname) == 1
    assert odds_posts == [odds_cname]
    assert card_posts.count(card_cname) == 1
    assert odds_api == ["1"]

    phase["payout"] = "afternoon"
    phase["odds"] = "result"
    second = _open().get(today=True, venues=["東京"], sires={})
    assert payout_posts.count(day_cname) == 2
    assert odds_posts == [odds_cname, odds_cname]
    assert card_posts.count(card_cname) == 2
    assert odds_api[0] == "1"
    assert "1" in odds_api[1:]
    assert len(odds_api) > 2
    assert second.sample_size == 1
    assert second.included_race_ids == [race_id]
    assert batches[1].races[0].warning is None or (
        JRA_PAYOUT_UNREACHABLE not in batches[1].races[0].warning
    )
    assert second.included_races[0].fetched_at is not None
    assert second.included_races[0].fetched_at.utcoffset() == timedelta(hours=9)


class _Hist:
    def __init__(self, batches: dict[date, HistoricalBatch]):
        self.batches = batches
        self.calls: list[dict[str, object]] = []

    def get(self, **kwargs: object) -> HistoricalBatch:
        self.calls.append(kwargs)
        day = kwargs.get("on_date")
        if isinstance(day, date) and day in self.batches:
            return self.batches[day]
        return _batch([], day=day if isinstance(day, date) else None)


def _batch(
    races: list[HistoricalRace],
    *,
    day: date | None,
    warning: str | None = None,
    remaining: list[str] | None = None,
    stopped: str | None = None,
) -> HistoricalBatch:
    return HistoricalBatch(
        races=races,
        skipped=0,
        flat_only=True,
        venues=["東京"],
        date_from=day,
        date_to=day,
        stopped_at_race_id=stopped,
        completed_race_ids=[race.race_id for race in races],
        remaining_race_ids=remaining or [],
        fetched_at=FETCHED,
        warning=warning,
    )
