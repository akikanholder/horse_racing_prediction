"""Pure trend tables over HistoricalRace rows.

This module does not fetch pages. Missing inputs stay null.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import TypedDict, cast

from backend.models.historical import HistoricalRace
from backend.models.horse import Pedigree
from backend.models.race import RacePayout, SettledFinisher
from backend.models.race_trends import (
    EMPTY_POPULARITY_BAND,
    EMPTY_RUNNING_STYLE,
    FETCHED_AT_DIFFERS,
    FINAL_WIN_ODDS_MISSING,
    FRAME_MISSING,
    GATE_MISSING,
    HORSE_ID_MISSING,
    JOCKEY_MISSING,
    NO_STARTERS,
    PLACE_FIELD_UNKNOWN,
    PLACE_NOT_SOLD,
    PLACE_ODDS_PAYOUT_DISAGREE,
    PLACE_PAYOUT_MISSING,
    PLACE_RANGE_UNUSED,
    PLACE_ROI_PARTIAL,
    POPULARITY_BANDS,
    POPULARITY_MISSING,
    RANK_MISSING,
    RESULT_PHASE_MISSING,
    RESULT_PHASE_PROVISIONAL,
    SCRATCHED,
    SIRE_UNAVAILABLE,
    STAKE_YEN,
    SURFACE_MISSING,
    WIN_ODDS_PAYOUT_DISAGREE,
    WIN_ROI_PARTIAL,
    ExcludedRace,
    FlatSurface,
    FrameRow,
    GateRow,
    JockeySurfaceRow,
    PopularityBandRow,
    RaceTrendReport,
    RecentMeetingWindow,
    RunnerOmission,
    RunningStyleRow,
    SireSurfaceRow,
    TrendFilter,
    running_style_definition,
)
from backend.models.running_style import (
    STYLE_FIELD_SIZE_UNAVAILABLE,
    STYLE_LABELS,
    STYLE_PASSING_UNAVAILABLE,
    StyleLabel,
    classify_early_call,
    early_position,
)
from backend.scrapers.runner_status import join_warnings

_RESULT_BLOCKS = (
    "race result returned 403",
    "race result requires login",
    "race list returned 403",
    "race list requires login",
)
_LOSS_WITHOUT_RANK = frozenset({"did_not_finish", "disqualified"})
_NON_START = frozenset({"scratched", "excluded"})
_FOUR = Decimal("0.0001")
_SURFACE_ORDER = {"芝": 0, "ダート": 1}


class _Measures(TypedDict):
    sample_size: int
    wins: int
    top2: int
    top3: int
    win_rate: float | None
    top2_rate: float | None
    top3_rate: float | None
    win_roi: float | None
    place_roi: float | None
    win_stake_count: int
    place_stake_count: int
    warning: str | None


@dataclass
class _Counted:
    surface: str | None
    horse_number: int | None
    bracket_number: int | None
    popularity: int | None
    rank: int | None
    style: StyleLabel | None
    style_reason: str | None
    jockey_name: str | None
    horse_id: str | None
    win_return: int | None
    win_warning: str | None
    place_return: int | None
    place_warning: str | None


def aggregate_race_trends(
    races: Sequence[HistoricalRace],
    *,
    filt: TrendFilter,
    sires: Mapping[str, Pedigree] | None = None,
    sire_unfetched: Mapping[str, str] | None = None,
    extra_excluded: Sequence[ExcludedRace] = (),
    recent_meeting: RecentMeetingWindow | None = None,
    batch_warning: str | None = None,
    history_skipped: int = 0,
    computed_at: datetime,
) -> RaceTrendReport:
    """Build the tables. Races that fail a required input are listed in ``excluded``."""
    included: list[HistoricalRace] = []
    excluded: list[ExcludedRace] = []
    for race in races:
        reason = _exclude_reason(race, filt)
        if reason == "":
            continue
        if reason is None:
            included.append(race)
        else:
            excluded.append(ExcludedRace(race_id=race.race_id, reason=reason))
    seen = {race.race_id for race in races}
    for item in extra_excluded:
        if item.race_id not in seen:
            excluded.append(item)
            seen.add(item.race_id)

    counted_by_race = [_runners(race) for race in included]
    flat = [runner for group in counted_by_race for runner in group]
    omissions: Counter[str] = Counter()
    for race in included:
        for finisher in race.result.results:
            role = _role(finisher)
            if role == "out":
                omissions[SCRATCHED] += 1
            elif role == "unknown":
                omissions[RANK_MISSING] += 1
    for runner in flat:
        _omit_runner(runner, omissions, sires or {}, sire_unfetched or {})

    fetched = [race.fetched_at for race in included]
    fetched_min = min(fetched) if fetched else None
    fetched_max = max(fetched) if fetched else None
    same_fetch = fetched_min is not None and fetched_min == fetched_max
    notes = [
        batch_warning,
        None if recent_meeting is None else recent_meeting.warning,
        FETCHED_AT_DIFFERS if fetched and not same_fetch else None,
        None
        if not included or _join_source(*(race.source for race in included))
        else ("source was not on the included races"),
    ]
    return RaceTrendReport(
        mode="recent_meeting" if filt.recent_meeting else "range",
        filter=filt,
        recent_meeting=recent_meeting,
        sample_size=len(included),
        runner_sample_size=len(flat),
        included_race_ids=[race.race_id for race in included],
        excluded=excluded,
        history_skipped=history_skipped,
        runner_omissions=_omission_rows(omissions),
        by_popularity=_popularity(flat),
        by_gate=_gates(flat),
        by_frame=_frames(flat),
        running_style_definition=running_style_definition(),
        by_running_style=_styles(flat),
        by_jockey=_jockeys(flat),
        by_sire=_sires(flat, sires or {}, sire_unfetched or {}),
        source=_join_source(*(race.source for race in included)),
        fetched_at=fetched_min if same_fetch else None,
        fetched_at_min=fetched_min,
        fetched_at_max=fetched_max,
        computed_at=computed_at,
        warning=join_warnings(*notes),
    )


def _exclude_reason(race: HistoricalRace, filt: TrendFilter) -> str | None:
    """None includes the race. ``""`` skips it. Any other string excludes it."""
    blocked = _result_block(race.warning) or _result_block(race.result.warning)
    if blocked:
        return blocked
    if race.flat is False:
        return "jump races are excluded"
    if not _matches_filter(race, filt):
        return ""
    missing = _missing_filter_field(race, filt)
    if missing:
        return missing
    phase = race.result.result_phase
    if phase == "provisional":
        return RESULT_PHASE_PROVISIONAL
    if phase != "confirmed":
        return RESULT_PHASE_MISSING
    starters = [row for row in race.result.results if _role(row) in {"ranked", "loss"}]
    if any(row.horse_number is None for row in starters):
        return GATE_MISSING
    if not starters:
        return NO_STARTERS
    if not race.odds_final or any(_win_odds(race, row.horse_number) is None for row in starters):
        return _final_odds_reason(race)
    return None


def _matches_filter(race: HistoricalRace, filt: TrendFilter) -> bool:
    """False when a printed field is present and outside the filter."""
    settled = race.result
    if filt.venues and settled.venue and _text(settled.venue) not in filt.venues:
        return False
    if filt.surface and race.surface and race.surface != filt.surface:
        return False
    if filt.distance is not None and race.distance is not None and race.distance != filt.distance:
        return False
    if filt.race_class and race.race_class and _text(race.race_class) != filt.race_class:
        return False
    if filt.going and race.going and _text(race.going) != filt.going:
        return False
    raced = settled.race_date
    if filt.date_from and raced and raced < filt.date_from:
        return False
    if filt.date_to and raced and raced > filt.date_to:
        return False
    return True


def _missing_filter_field(race: HistoricalRace, filt: TrendFilter) -> str | None:
    settled = race.result
    if filt.venues and not _text(settled.venue):
        return "venue was not on the page"
    if filt.surface and race.surface is None:
        return SURFACE_MISSING
    if filt.distance is not None and race.distance is None:
        return "distance was not on the page"
    if filt.race_class and not _text(race.race_class):
        return "race class was not on the page"
    if filt.going and not _text(race.going):
        return "going was not on the page"
    if (filt.date_from or filt.date_to) and settled.race_date is None:
        return "race date was not on the page"
    return None


def _runners(race: HistoricalRace) -> list[_Counted]:
    field_size = race.field_size if race.field_size and race.field_size > 0 else None
    active = _active(race.result.results)
    starter_count: int | None = len(active) if _starter_count_known(race.result.results) else None
    pay_through, place_rule = _place_rule(starter_count)
    rows: list[_Counted] = []
    for finisher in active:
        assert finisher.horse_number is not None
        rank = finisher.rank if finisher.rank and finisher.rank >= 1 else None
        style, style_reason = _style(finisher.passing_order, field_size)
        win_return, win_warning = _win_return(race, finisher.horse_number, rank)
        place_return, place_warning = _place_return(
            race, finisher.horse_number, rank, pay_through, place_rule
        )
        rows.append(
            _Counted(
                surface=race.surface,
                horse_number=finisher.horse_number,
                bracket_number=finisher.bracket_number,
                popularity=finisher.popularity
                if finisher.popularity and finisher.popularity > 0
                else None,
                rank=rank,
                style=style,
                style_reason=style_reason,
                jockey_name=_text(finisher.jockey_name),
                horse_id=_text(finisher.horse_id),
                win_return=win_return,
                win_warning=win_warning,
                place_return=place_return,
                place_warning=place_warning,
            )
        )
    return rows


def _omit_runner(
    runner: _Counted,
    omissions: Counter[str],
    sires: Mapping[str, Pedigree],
    unfetched: Mapping[str, str],
) -> None:
    reasons: set[str] = set()
    if runner.popularity is None:
        reasons.add(POPULARITY_MISSING)
    if runner.horse_number is None:
        reasons.add(GATE_MISSING)
    if runner.bracket_number is None or runner.bracket_number < 1:
        reasons.add(FRAME_MISSING)
    if runner.style_reason:
        reasons.add(runner.style_reason)
    if not runner.jockey_name:
        reasons.add(JOCKEY_MISSING)
    if runner.surface not in {"芝", "ダート"}:
        reasons.add(SURFACE_MISSING)
    sire_reason = _sire_reason(runner, sires, unfetched)
    if sire_reason:
        reasons.add(sire_reason)
    for reason in reasons:
        omissions[reason] += 1


def _sire_reason(
    runner: _Counted,
    sires: Mapping[str, Pedigree],
    unfetched: Mapping[str, str],
) -> str | None:
    if runner.surface not in {"芝", "ダート"}:
        return None
    if not runner.horse_id:
        return HORSE_ID_MISSING
    if runner.horse_id in unfetched:
        return unfetched[runner.horse_id]
    pedigree = sires.get(runner.horse_id)
    if pedigree is None or pedigree.sire is None:
        return pedigree.warning if pedigree and pedigree.warning else SIRE_UNAVAILABLE
    if not _text(pedigree.sire.name) and not pedigree.sire.horse_id:
        return pedigree.warning or SIRE_UNAVAILABLE
    return None


def _popularity(rows: Sequence[_Counted]) -> list[PopularityBandRow]:
    grouped: dict[str, list[_Counted]] = defaultdict(list)
    for row in rows:
        band = _band(row.popularity)
        if band:
            grouped[band].append(row)
    result: list[PopularityBandRow] = []
    for name, low, high in POPULARITY_BANDS:
        measures = _measures(grouped.get(name, []), empty_warning=EMPTY_POPULARITY_BAND)
        result.append(
            PopularityBandRow(band=name, popularity_min=low, popularity_max=high, **measures)
        )
    return result


def _gates(rows: Sequence[_Counted]) -> list[GateRow]:
    grouped: dict[int, list[_Counted]] = defaultdict(list)
    for row in rows:
        if row.horse_number is not None and row.horse_number >= 1:
            grouped[row.horse_number].append(row)
    return [
        GateRow(horse_number=number, **_measures(grouped[number], empty_warning=""))
        for number in sorted(grouped)
    ]


def _frames(rows: Sequence[_Counted]) -> list[FrameRow]:
    grouped: dict[int, list[_Counted]] = defaultdict(list)
    for row in rows:
        if row.bracket_number is not None and row.bracket_number >= 1:
            grouped[row.bracket_number].append(row)
    return [
        FrameRow(bracket_number=number, **_measures(grouped[number], empty_warning=""))
        for number in sorted(grouped)
    ]


def _styles(rows: Sequence[_Counted]) -> list[RunningStyleRow]:
    grouped: dict[str, list[_Counted]] = defaultdict(list)
    for row in rows:
        if row.style:
            grouped[row.style].append(row)
    return [
        RunningStyleRow(
            label=label, **_measures(grouped.get(label, []), empty_warning=EMPTY_RUNNING_STYLE)
        )
        for label in STYLE_LABELS
    ]


def _jockeys(rows: Sequence[_Counted]) -> list[JockeySurfaceRow]:
    grouped: dict[tuple[str, str], list[_Counted]] = defaultdict(list)
    for row in rows:
        if row.jockey_name and row.surface in {"芝", "ダート"}:
            grouped[(row.jockey_name, row.surface)].append(row)
    keys = sorted(grouped, key=lambda item: (item[0], _SURFACE_ORDER[item[1]]))
    return [
        JockeySurfaceRow(
            jockey_name=name,
            surface=cast(FlatSurface, surface),
            **_measures(grouped[(name, surface)], empty_warning=""),
        )
        for name, surface in keys
    ]


def _sires(
    rows: Sequence[_Counted],
    sires: Mapping[str, Pedigree],
    unfetched: Mapping[str, str],
) -> list[SireSurfaceRow]:
    grouped: dict[tuple[str | None, str | None, str], list[_Counted]] = defaultdict(list)
    sources: dict[tuple[str | None, str | None, str], list[str]] = defaultdict(list)
    warnings: dict[tuple[str | None, str | None, str], list[str | None]] = defaultdict(list)
    for row in rows:
        if row.surface not in {"芝", "ダート"} or not row.horse_id or row.horse_id in unfetched:
            continue
        pedigree = sires.get(row.horse_id)
        if pedigree is None or pedigree.sire is None:
            continue
        name = _text(pedigree.sire.name)
        if not name and not pedigree.sire.horse_id:
            continue
        key = (pedigree.sire.horse_id, name, row.surface)
        grouped[key].append(row)
        if pedigree.source:
            sources[key].append(pedigree.source)
        if pedigree.sire.horse_id is None:
            warnings[key].append("sire id was not on the pedigree page")
        if not name:
            warnings[key].append(SIRE_UNAVAILABLE)
    keys = sorted(
        grouped,
        key=lambda item: (item[1] or "", item[0] or "", _SURFACE_ORDER[item[2]]),
    )
    built: list[SireSurfaceRow] = []
    for key in keys:
        sire_id, name, surface = key
        measures = _measures(grouped[key], empty_warning="")
        measures["warning"] = join_warnings(measures["warning"], *warnings[key])
        built.append(
            SireSurfaceRow(
                sire_name=name,
                sire_id=sire_id,
                surface=cast(FlatSurface, surface),
                pedigree_source=_join_source(*sources[key]),
                **measures,
            )
        )
    return built


def _measures(rows: Sequence[_Counted], *, empty_warning: str) -> _Measures:
    sample = len(rows)
    if sample == 0:
        return {
            "sample_size": 0,
            "wins": 0,
            "top2": 0,
            "top3": 0,
            "win_rate": None,
            "top2_rate": None,
            "top3_rate": None,
            "win_roi": None,
            "place_roi": None,
            "win_stake_count": 0,
            "place_stake_count": 0,
            "warning": empty_warning or None,
        }
    wins = sum(1 for row in rows if row.rank == 1)
    top2 = sum(1 for row in rows if row.rank is not None and row.rank <= 2)
    top3 = sum(1 for row in rows if row.rank is not None and row.rank <= 3)
    win_priced = [row.win_return for row in rows if row.win_return is not None]
    place_priced = [row.place_return for row in rows if row.place_return is not None]
    notes: list[str | None] = []
    if 0 < len(win_priced) < sample:
        notes.append(WIN_ROI_PARTIAL)
    if len(win_priced) < sample:
        notes.extend(row.win_warning for row in rows if row.win_return is None)
    if 0 < len(place_priced) < sample:
        notes.append(PLACE_ROI_PARTIAL)
    if len(place_priced) < sample:
        notes.extend(row.place_warning for row in rows if row.place_return is None)
    return {
        "sample_size": sample,
        "wins": wins,
        "top2": top2,
        "top3": top3,
        "win_rate": _ratio(wins, sample),
        "top2_rate": _ratio(top2, sample),
        "top3_rate": _ratio(top3, sample),
        "win_roi": _roi(win_priced),
        "place_roi": _roi(place_priced),
        "win_stake_count": len(win_priced),
        "place_stake_count": len(place_priced),
        "warning": join_warnings(*notes),
    }


def _active(finishers: Sequence[SettledFinisher]) -> list[SettledFinisher]:
    return [row for row in finishers if _role(row) in {"ranked", "loss"} and row.horse_number]


def _starter_count_known(finishers: Sequence[SettledFinisher]) -> bool:
    return all(_role(row) != "unknown" for row in finishers)


def _role(finisher: SettledFinisher) -> str:
    if finisher.status in _NON_START:
        return "out"
    if finisher.rank is not None and finisher.rank >= 1:
        return "ranked"
    if finisher.status in _LOSS_WITHOUT_RANK:
        return "loss"
    return "unknown"


def _style(passing: str | None, field_size: int | None) -> tuple[StyleLabel | None, str | None]:
    position = early_position(passing)
    if position is None:
        return None, STYLE_PASSING_UNAVAILABLE
    if field_size is None:
        return None, STYLE_FIELD_SIZE_UNAVAILABLE
    label = classify_early_call(position, field_size)
    if label is None:
        return None, STYLE_FIELD_SIZE_UNAVAILABLE
    return label, None


def _place_rule(starter_count: int | None) -> tuple[int | None, str | None]:
    if starter_count is None:
        return None, PLACE_FIELD_UNKNOWN
    if starter_count >= 8:
        return 3, None
    if starter_count >= 5:
        return 2, None
    return None, PLACE_NOT_SOLD


def _win_return(
    race: HistoricalRace, horse_number: int, rank: int | None
) -> tuple[int | None, str | None]:
    odds = _win_odds(race, horse_number)
    yen = None if odds is None else _odds_to_yen(odds)
    if rank != 1:
        if yen is None and odds is not None:
            return None, "win odds were not a whole yen amount"
        return 0, None
    payouts = _payouts(race, "単勝", horse_number)
    if len(payouts) > 1:
        return None, "win payout was duplicated"
    if len(payouts) == 1:
        payout = payouts[0].payout
        if yen is not None and payout != yen:
            return None, WIN_ODDS_PAYOUT_DISAGREE
        return payout, None
    if yen is None:
        return None, FINAL_WIN_ODDS_MISSING
    return yen, None


def _place_return(
    race: HistoricalRace,
    horse_number: int,
    rank: int | None,
    pay_through: int | None,
    rule_warning: str | None,
) -> tuple[int | None, str | None]:
    if rule_warning:
        return None, rule_warning
    assert pay_through is not None
    if rank is None or rank > pay_through:
        return 0, None
    payouts = _payouts(race, "複勝", horse_number)
    price, price_warning = _place_price_yen(race, horse_number)
    if len(payouts) > 1:
        return None, "place payout was duplicated"
    if len(payouts) == 1:
        payout = payouts[0].payout
        if price is not None and payout != price:
            return None, PLACE_ODDS_PAYOUT_DISAGREE
        return payout, None
    if price_warning:
        return None, price_warning
    if price is not None:
        return price, None
    return None, PLACE_PAYOUT_MISSING


def _place_price_yen(race: HistoricalRace, horse_number: int) -> tuple[int | None, str | None]:
    quotes = [
        quote
        for quote in race.final_odds or []
        if quote.bet_type == "place" and _same_horse(quote.combination, horse_number)
    ]
    if len(quotes) > 1:
        return None, "place odds were duplicated"
    if not quotes:
        return None, None
    quote = quotes[0]
    ranged = quote.odds_min is not None or quote.odds_max is not None
    if ranged and quote.odds is None:
        return None, PLACE_RANGE_UNUSED
    if quote.odds is None:
        return None, None
    yen = _odds_to_yen(quote.odds)
    if yen is None:
        return None, "place odds were not a whole yen amount"
    return yen, None


def _win_odds(race: HistoricalRace, horse_number: int | None) -> float | None:
    if horse_number is None:
        return None
    for quote in race.final_odds or []:
        if quote.bet_type == "win" and _same_horse(quote.combination, horse_number):
            return quote.odds
    return None


def _payouts(race: HistoricalRace, bet_type: str, horse_number: int) -> list[RacePayout]:
    return [
        row
        for row in race.result.payouts
        if row.bet_type.strip() == bet_type and _same_horse(row.combination, horse_number)
    ]


def _same_horse(combination: str, horse_number: int) -> bool:
    text = combination.strip()
    if text == str(horse_number):
        return True
    return text.isdigit() and int(text) == horse_number


def _odds_to_yen(odds: float) -> int | None:
    yen = Decimal(str(odds)) * Decimal(STAKE_YEN)
    if yen != yen.to_integral_value():
        return None
    return int(yen)


def _ratio(numer: int, denom: int) -> float | None:
    if denom == 0:
        return None
    return float((Decimal(numer) / Decimal(denom)).quantize(_FOUR, rounding=ROUND_HALF_UP))


def _roi(returns: Sequence[int]) -> float | None:
    if not returns:
        return None
    stake = Decimal(STAKE_YEN) * Decimal(len(returns))
    total = sum((Decimal(value) for value in returns), Decimal(0))
    return float(((total / stake) - Decimal(1)).quantize(_FOUR, rounding=ROUND_HALF_UP))


def _band(popularity: int | None) -> str | None:
    if popularity is None or popularity < 1:
        return None
    for name, low, high in POPULARITY_BANDS:
        if high is None and popularity >= low:
            return name
        if high is not None and low <= popularity <= high:
            return name
    return None


def _result_block(warning: str | None) -> str | None:
    if not warning:
        return None
    for marker in _RESULT_BLOCKS:
        if marker in warning:
            return marker
    return None


def _final_odds_reason(race: HistoricalRace) -> str:
    warning = race.warning or ""
    for marker in (
        "final odds were not confirmed",
        "final win odds were not on the page",
        "final odds were not on the page",
    ):
        if marker in warning:
            return marker
    return FINAL_WIN_ODDS_MISSING


def _omission_rows(omissions: Counter[str]) -> list[RunnerOmission]:
    return [RunnerOmission(reason=reason, count=omissions[reason]) for reason in sorted(omissions)]


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _join_source(*parts: str | None) -> str | None:
    found: list[str] = []
    for part in parts:
        if not part:
            continue
        for piece in part.split("+"):
            if piece and piece not in found:
                found.append(piece)
    if not found:
        return None
    return "+".join(found)
