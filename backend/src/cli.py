#!/usr/bin/env python3
"""CLI tool for testing horse racing scrapers."""

import argparse
import json
import logging
import sys
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from backend.models.historical import HistoricalBatch, HistoricalRace
from backend.models.horse import Horse, HorsePerformance, PedigreeHorse
from backend.models.odds import OddsQuote
from backend.models.race import RacePayout, SettledFinisher, SettledRaceResult, TrackBias
from backend.models.race_trends import RaceTrendReport, TrendBucket
from backend.models.running_style import RunningStyle
from backend.models.training import Training
from backend.scrapers.track_freshness import parse_race_day, require_finite_seconds
from backend.services.prediction_store import (
    PredictionStore,
    PredictionValidationError,
    parse_meeting_date,
    resolve_predictions_dir,
)
from backend.services.race_service import RaceService
from backend.services.track_info_service import TrackInfoService

logger = logging.getLogger(__name__)


def _finite_arg(name: str) -> Callable[[str], float]:
    """Argparse type: a finite number of seconds, zero or more."""

    def parse(value: str) -> float:
        try:
            parsed = float(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{name} must be a finite number >= 0") from exc
        try:
            return require_finite_seconds(parsed, name)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc

    return parse


def setup_logging(verbose: bool = False):
    """Setup logging configuration."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


def _jra_fetch_flags() -> argparse.ArgumentParser:
    """``--fetch-mode`` for JRA official pages. The default stays the HTTP path."""
    flags = argparse.ArgumentParser(add_help=False)
    flags.add_argument(
        "--fetch-mode",
        choices=["html", "headless", "auto"],
        default="html",
        help=(
            "JRA公式の取得。html は現行のHTTP。headless はブラウザ。"
            "auto はブラウザがあれば使い、無ければ HTTP。"
            "403・ログイン・captcha は HTTP に戻さない"
        ),
    )
    return flags


def _open_service(factory: Callable[[], Any], args: Any) -> Any:
    """Construct a service and set JRA fetch mode when the service has one."""
    service = factory()
    mode = getattr(args, "fetch_mode", "html")
    if hasattr(service, "fetch_mode"):
        service.fetch_mode = mode
    scraper = getattr(service, "_jra_scraper", None)
    if scraper is not None and hasattr(scraper, "fetch_mode"):
        scraper.fetch_mode = mode
    netkeiba = getattr(service, "_netkeiba_scraper", None)
    if netkeiba is not None and hasattr(netkeiba, "fetch_mode"):
        netkeiba.fetch_mode = mode
    return service


def _json_ready(data):
    """Turn models, including ones nested in dicts, into JSON values."""
    if hasattr(data, "model_dump"):
        return data.model_dump(mode="json")
    if isinstance(data, dict):
        return {str(key): _json_ready(value) for key, value in data.items()}
    if isinstance(data, (list, tuple)) and not isinstance(data, (str, bytes)):
        return [_json_ready(item) for item in data]
    return data


def print_json(data, indent: int = 2):
    """Print data as formatted JSON."""
    data = _json_ready(data)

    def date_handler(obj):
        if isinstance(obj, date):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

    print(json.dumps(data, indent=indent, ensure_ascii=False, default=date_handler))


def cmd_races(args):
    """Get weekly races."""
    with _open_service(RaceService, args) as service:
        schedule = service.get_weekly_races()
        print(f"\n=== 今週のレース ({schedule.week_start} ~ {schedule.week_end}) ===")
        print(f"取得時刻: {schedule.fetched_at}")
        print(f"レース数: {len(schedule.races)}\n")

        if args.json:
            print_json(schedule)
        else:
            for race in schedule.races:
                print(
                    f"[{race.race_date}] {race.venue} {race.race_number}R "
                    f"{race.race_name} ({race.track_type}{race.distance}m)"
                )


def cmd_race_card(args):
    """Get race card."""
    with _open_service(RaceService, args) as service:
        race_card = service.get_race_card(args.race_id)
        if not race_card:
            print(f"レース {args.race_id} が見つかりません")
            return

        print(f"\n=== 出馬表: {race_card.race.race_name} ===")
        print(f"開催日: {race_card.race.race_date}")
        print(f"競馬場: {race_card.race.venue}")
        if race_card.race.post_time:
            print(
                f"発走: {race_card.race.post_time} "
                f"({race_card.race.post_time_source}, {race_card.race.post_time_fetched_at})"
            )
        print(f"コース: {race_card.race.track_type} {race_card.race.distance}m")
        print(f"出走頭数: {len(race_card.entries)}")
        if race_card.race.track_bias:
            _print_track_bias(race_card.race.track_bias)
        print()

        if args.json:
            print_json(race_card)
        else:
            for entry in race_card.entries:
                label = f" {entry.status_label}" if entry.status_label else ""
                print(
                    f"{entry.bracket_number}枠 {entry.horse_number}番 "
                    f"{entry.horse_name} ({entry.sex}{entry.age}) "
                    f"[{entry.jockey_name}]{label}"
                )
            if race_card.warning:
                print(f"警告: {race_card.warning}")


def cmd_horse(args):
    """Get horse information."""
    with _open_service(RaceService, args) as service:
        horse = service.get_horse_info(args.horse_id)
        if not horse:
            print(f"馬 {args.horse_id} が見つかりません")
            return

        print(f"\n=== 競走馬情報: {horse.horse_name} ===")
        print(f"馬ID: {horse.horse_id}")
        print(f"性別: {horse.sex}")
        print(f"取得時刻: {horse.fetched_at}")
        print(f"近走取得: {horse.runs_fetched}走")
        if horse.source:
            print(f"近走ソース: {horse.source}")
        if horse.login_required_for_more is True:
            print("続きの成績: ログインが必要")
        elif horse.login_required_for_more is False:
            print("続きの成績: ログインは不要")
        if horse.birth_year:
            print(f"生年: {horse.birth_year}年")
        _print_pedigree(horse)
        _print_running_style(horse.running_style)
        _print_track_bias(horse.track_bias)
        _print_training(horse.training)
        if horse.trainer_name:
            print(f"調教師: {horse.trainer_name}")
        if horse.training_center:
            print(f"所属: {horse.training_center}")
        if horse.total_races is None and horse.total_wins is None:
            print("通算成績: 不明")
        else:
            races = "-" if horse.total_races is None else horse.total_races
            wins = "-" if horse.total_wins is None else horse.total_wins
            print(f"通算成績: {races}戦{wins}勝")
        if horse.total_earnings is not None:
            print(f"獲得賞金: {horse.total_earnings}万円")
        if horse.warning:
            print(f"警告: {horse.warning}")

        if args.json:
            print_json(horse)
        else:
            print("\n--- 近走成績（過去走。今走の結果ではない） ---")
            if not horse.performances:
                print("なし")
            for perf in horse.performances[:5]:
                result = f"{perf.rank}着" if perf.rank else "---"
                print(f"{_format_performance(perf)} → {result}")


def _print_pedigree(horse: Horse) -> None:
    """Text view of sire, dam, and damsire. Missing sides stay 不明."""
    pedigree = horse.pedigree
    if pedigree is None:
        print("血統: 不明")
        return
    print(
        "血統: "
        f"{_format_ancestor('父', pedigree.sire)} / "
        f"{_format_ancestor('母', pedigree.dam)} / "
        f"{_format_ancestor('母父', pedigree.damsire)}"
    )
    if pedigree.source:
        print(f"血統ソース: {pedigree.source}")
    if pedigree.warning:
        print(f"血統警告: {pedigree.warning}")


def _format_ancestor(label: str, member: PedigreeHorse | None) -> str:
    if member is None or (member.name is None and member.horse_id is None):
        return f"{label} 不明"
    name = member.name or "名前不明"
    horse_id = member.horse_id or "id不明"
    return f"{label} {name} ({horse_id})"


def _print_running_style(style: RunningStyle | None) -> None:
    """Text view. A null label is not filled from odds or the race name."""
    if style is None:
        print("脚質: 不明")
        return
    label = style.label or "不明"
    confidence = "-" if style.confidence is None else f"{style.confidence:.2f}"
    print(f"脚質: {label}（{style.data_sufficiency}, 信頼度 {confidence}, {style.sample_size}走）")
    counts = " ".join(
        f"{name}{style.counts.get(name, 0)}" for name in ("逃げ", "先行", "差し", "追込")
    )
    print(f"脚質内訳: {counts}")
    if style.short_distance_front_under_specified:
        print(
            f"短距離の逃げ・先行: {style.short_distance_front_runs}走"
            f"（{style.short_distance_m}m以下、3走未満のため未充足）"
        )
    if style.warning:
        print(f"脚質警告: {style.warning}")


def _print_training(training: Training | None) -> None:
    """Text view. Clocks that were not on the page stay なし."""
    if training is None:
        print("調教: なし")
        return
    source = training.source or "なし"
    print(f"調教: {source}（{training.data_sufficiency}、{len(training.workouts)}本）")
    if training.warning:
        print(f"調教警告: {training.warning}")
    summary = training.summary
    if summary is not None:
        if summary.comment or summary.arrow:
            comment = summary.comment or "なし"
            arrow = summary.arrow or "なし"
            print(f"追い切り短評: {comment} {arrow}")
        if summary.average is not None and summary.average.time:
            print(f"調教平均: {summary.average.label} {summary.average.time}")
        if summary.evaluation:
            print(f"調教評価: {summary.evaluation}")
    if not training.workouts:
        print("調教時計: なし")
        return
    print("--- 調教（ページにある行。オッズからは作らない） ---")
    for workout in training.workouts[:5]:
        when = workout.date.isoformat() if workout.date else (workout.date_label or "日付なし")
        course = workout.course or "コースなし"
        distance = workout.distance or "距離なし"
        clock = workout.time or "時計なし"
        condition = workout.condition or "馬場なし"
        rank = "-" if workout.rank_in_group is None else str(workout.rank_in_group)
        print(f"{when} {course} {distance} {clock} 馬場:{condition} 回り:{rank}")


def _print_track_bias(bias: TrackBias) -> None:
    """Conditions from the race page, and bias labels copied from official pages."""
    bits: list[str] = []
    if bias.venue:
        bits.append(bias.venue)
    if bias.surface and bias.distance:
        bits.append(f"{bias.surface}{bias.distance}m")
    elif bias.surface:
        bits.append(bias.surface)
    elif bias.distance:
        bits.append(f"{bias.distance}m")
    if bias.going:
        bits.append(f"馬場:{bias.going}")
    condition = " ".join(bits) if bits else "開催条件なし"
    inside = bias.inside_outside or "なし"
    pace = bias.pace_bias or "なし"
    source = f" [{bias.source}]" if bias.source else ""
    print(f"トラックバイアス: 内外 {inside} / ペース {pace}（{condition}）{source}")
    print(f"バイアス充足: {bias.data_sufficiency}")
    if bias.warning:
        print(f"バイアス警告: {bias.warning}")


def _format_performance(perf: HorsePerformance) -> str:
    """Format one past performance for the text view."""
    if perf.track_type and perf.distance is not None:
        course = f"{perf.track_type}{perf.distance}m"
    elif perf.distance is not None:
        course = f"{perf.distance}m"
    else:
        course = perf.track_type or ""
    if perf.track_condition:
        course = f"{course} {perf.track_condition}".strip()
    return f"{perf.race_date} {perf.venue} {perf.race_name} ({course})"


def cmd_courses(args):
    """Get course information."""
    with _open_service(RaceService, args) as service:
        if args.venue:
            course = service.get_course_info(args.venue)
            if not course:
                print(f"競馬場 {args.venue} が見つかりません")
                return

            print(f"\n=== コース情報: {course.venue_name} ===")
            print(f"競馬場コード: {course.venue_code}")
            if course.turf_circumference:
                print(f"芝コース周回: {course.turf_circumference}m")
            if course.dirt_circumference:
                print(f"ダートコース周回: {course.dirt_circumference}m")
            if course.turf_straight:
                print(f"芝直線: {course.turf_straight}m")
            if course.dirt_straight:
                print(f"ダート直線: {course.dirt_straight}m")

            if args.json:
                print_json(course)
        else:
            courses = service.get_all_course_info()
            print(f"\n=== 全競馬場コース情報 ({len(courses)}場) ===\n")

            if args.json:
                print_json(courses)
            else:
                for course in courses:
                    print(f"【{course.venue_name}】")
                    if course.turf_circumference:
                        print(
                            f"  芝: 周回{course.turf_circumference}m / 直線{course.turf_straight}m"
                        )
                    if course.dirt_circumference:
                        print(
                            f"  ダート: 周回{course.dirt_circumference}m / "
                            f"直線{course.dirt_straight}m"
                        )


def _cushion_text(course) -> str:
    """Printed cushion number and grade. A missing number is left off."""
    cushion = course.cushion if course is not None else None
    if cushion is None or cushion.value is None:
        return ""
    grade = f" {cushion.grade}" if cushion.grade else ""
    return f" クッション値: {cushion.value}{grade} ({cushion.source}, {cushion.fetched_at})"


def _moisture_text(moisture) -> str:
    """Printed goal and corner moisture, with the source that supplied them."""
    if moisture is None:
        return ""
    if moisture.goal_front is None and moisture.fourth_corner is None:
        return ""
    goal = "-" if moisture.goal_front is None else moisture.goal_front
    corner = "-" if moisture.fourth_corner is None else moisture.fourth_corner
    return f"ゴール前 {goal} / 4コーナー {corner} ({moisture.source}, {moisture.fetched_at})"


def cmd_track(args):
    """Get detailed track (baba) information."""
    with _open_service(TrackInfoService, args) as service:
        kwargs = {}
        if getattr(args, "wait_fresh", False):
            kwargs = {
                "wait_fresh": True,
                "retry_interval": getattr(args, "retry_interval", 60),
                "max_wait": getattr(args, "max_wait", 600),
            }
        results = service.get_track_info(
            racecourse=args.venue,
            date=args.date,
            merge=args.merge,
            **kwargs,
        )

    if args.json:
        print_json(results)
        return

    print("\n=== 馬場情報 ===")
    if args.venue:
        print(f"競馬場: {args.venue}")
    print(f"ソース: {', '.join(results.keys()) or 'なし'}\n")

    for source, info in results.items():
        place = info.racecourse or "不明"
        print(f"[{source}] {place} 天候: {info.weather.value}")
        if info.weather_source:
            print(f"  天候ソース: {info.weather_source} {info.weather_fetched_at}")
        if info.post_time:
            print(
                f"  発走: {info.post_time} ({info.post_time_source}, {info.post_time_fetched_at})"
            )
        if info.measured_at is not None:
            print(f"  測定: {info.measured_at.isoformat()}")
        if info.stale is True:
            print("  測定日: レース日と違う")
        elif info.is_today is True:
            print("  測定日: 今日")
        if info.warning:
            print(f"  警告: {info.warning}")
        if info.warning and info.racecourse is None:
            continue
        if info.turf:
            print(f"  芝: {info.turf.condition.value}{_cushion_text(info.turf)}")
            moisture = _moisture_text(info.turf.moisture)
            if moisture:
                print(f"  芝含水率: {moisture}")
        if info.dirt:
            print(f"  ダート: {info.dirt.condition.value}")
            moisture = _moisture_text(info.dirt.moisture)
            if moisture:
                print(f"  ダート含水率: {moisture}")


def cmd_odds(args):
    """Get win, wide, umaren, and sanrenpuku odds quotes."""
    with _open_service(RaceService, args) as service:
        snapshot = service.get_race_odds(
            args.race_id,
            bet_types=args.bet_types,
            prefer=args.prefer,
        )

    if args.json:
        print_json(snapshot)
        return

    print(f"\n=== {_odds_title(snapshot.bet_types)}: {snapshot.race_id} ===")
    print(f"状態: {snapshot.status}")
    print(f"局面: {_race_phase_label(snapshot.race_phase)}")
    print(f"文字コード: {snapshot.encoding}")
    if snapshot.reason:
        print(f"理由: {snapshot.reason}")
    if snapshot.non_runners:
        marks = "、".join(
            f"{runner.horse_number}番{runner.status_label or runner.status}"
            for runner in snapshot.non_runners
        )
        print(f"取消・除外: {marks}")
    elif snapshot.non_runners is None and snapshot.warning:
        print("取消・除外: 不明")
    if snapshot.warning:
        print(f"警告: {snapshot.warning}")
    print(f"取得時刻: {snapshot.fetched_at}")
    if not snapshot.quotes:
        return

    updated = snapshot.quotes[0].market_updated_at
    if updated:
        print(f"オッズ更新: {updated}")
    print()
    for quote in snapshot.quotes:
        print(_format_odds_quote(quote))


def _race_phase_label(race_phase: str | None) -> str:
    """Text label. The token matches RaceOdds.race_phase."""
    if race_phase == "pre":
        return "pre（レース前）"
    if race_phase == "post":
        return "post（レース後）"
    return "不明"


def _odds_title(bet_types: Sequence[str]) -> str:
    if bet_types == ["wide"]:
        return "ワイドオッズ"
    if bet_types == ["umaren"]:
        return "馬連オッズ"
    if bet_types == ["sanrenpuku"]:
        return "三連複オッズ"
    if bet_types == ["win"]:
        return "単勝オッズ"
    return "オッズ"


def _format_odds_quote(quote: OddsQuote) -> str:
    tag = f"[{quote.source} / {quote.kind}]"
    mark = ""
    if quote.non_runner:
        mark = " 取消・除外"
    elif quote.withheld:
        mark = " ----"
    if quote.bet_type == "wide":
        price = "----" if quote.withheld else f"{quote.odds_min}-{quote.odds_max}"
        return f"ワイド {quote.combination} {price} {tag}{mark}"
    if quote.bet_type == "umaren":
        price = "----" if quote.withheld else str(quote.odds)
        return f"馬連 {quote.combination} {price} {tag}{mark}"
    if quote.bet_type == "sanrenpuku":
        price = "----" if quote.withheld else str(quote.odds)
        return f"三連複 {quote.combination} {price} {tag}{mark}"
    price = "----" if quote.withheld else str(quote.odds)
    return f"{quote.combination}番 {price} {tag}{mark}"


def cmd_result(args):
    """Get the settled result of one race.

    This is post-race settlement for mark × finish logs.
    ``cli horse`` is that horse's past runs and is not this command.
    """
    kwargs = {}
    if getattr(args, "wait_complete", False):
        kwargs = {
            "wait_complete": True,
            "retry_interval": getattr(args, "retry_interval", 60),
            "max_wait": getattr(args, "max_wait", 600),
        }
    with _open_service(RaceService, args) as service:
        settled = service.get_settled_race_result(args.race_id, **kwargs)

    if args.json:
        print_json(settled)
        return

    _print_settled_result(settled)


def _print_settled_result(settled: SettledRaceResult) -> None:
    """Text view. Says this is settlement, not near-run history."""
    name = settled.race_name or settled.race_id
    print(f"\n=== 確定結果（このレースの精算。馬の近走ではない）: {name} ===")
    print(f"レースID: {settled.race_id}")
    print(f"開催日: {settled.race_date or '不明'}")
    course = _settled_course(settled)
    print(f"条件: {settled.venue or '不明'} {course}".rstrip())
    print(f"取得時刻: {settled.fetched_at}")
    phase = {"confirmed": "確定", "provisional": "確定前"}.get(settled.result_phase or "", "不明")
    print(f"結果区分: {phase}")
    print(f"出走と一致: {'はい' if settled.complete else 'いいえ'}")
    if settled.source:
        print(f"取得元: {settled.source}")
    if settled.warning:
        print(f"警告: {settled.warning}")
    print("\n--- 着順 ---")
    if not settled.results:
        print("なし")
    for finisher in settled.results:
        print(_format_finisher(finisher))
    print("\n--- 払戻 ---")
    if not settled.payouts:
        print("なし")
    for payout in settled.payouts:
        print(_format_payout(payout))
    if settled.payout_note:
        print(f"注記: {settled.payout_note}")
    if settled.refunds:
        print("\n--- 返還 ---")
        for refund in settled.refunds:
            horses = "、".join(f"{number}番" for number in refund.horse_numbers) or "馬番の印字なし"
            bets = "" if not refund.bet_types else " " + " ".join(refund.bet_types)
            brackets = ""
            if refund.bracket_numbers:
                brackets = " 同枠" + "、".join(f"{number}枠" for number in refund.bracket_numbers)
            origin = f" [{refund.source}]" if refund.source else ""
            print(f"{horses}{brackets}{bets}{origin}")


def _settled_course(settled: SettledRaceResult) -> str:
    if settled.track_type and settled.distance is not None:
        course = f"{settled.track_type}{settled.distance}m"
    elif settled.distance is not None:
        course = f"{settled.distance}m"
    else:
        course = settled.track_type or ""
    if settled.track_condition:
        course = f"{course} 馬場:{settled.track_condition}".strip()
    if settled.weather:
        course = f"{course} 天候:{settled.weather}".strip()
    return course


def _format_finisher(finisher: SettledFinisher) -> str:
    if finisher.rank is not None:
        rank = f"{finisher.rank}着"
    elif finisher.status_label:
        rank = finisher.status_label
    else:
        rank = "---"
    if finisher.demoted:
        rank = f"{rank}降着"
    number = f"{finisher.horse_number}番" if finisher.horse_number is not None else "?番"
    name = finisher.horse_name or ""
    jockey = finisher.jockey_name or ""
    time = finisher.finish_time or ""
    margin = finisher.margin or ""
    odds = "" if finisher.odds is None else str(finisher.odds)
    return f"{rank} {number} {name} [{jockey}] {time} {margin} {odds}".rstrip()


def _format_payout(payout: RacePayout) -> str:
    popularity = "" if payout.popularity is None else f" {payout.popularity}人気"
    return f"{payout.bet_type} {payout.combination} {payout.payout}円{popularity}"


def cmd_history(args):
    """Past flat races: final odds, payouts, and the full finish order."""
    if not args.race_id and not args.date and not args.date_from:
        print("race_id か --date か --from を指定してください")
        sys.exit(1)
    with _open_service(RaceService, args) as service:
        batch = service.get_historical_races(
            race_id=args.race_id,
            on_date=_cli_date(args.date),
            date_from=_cli_date(args.date_from),
            date_to=_cli_date(args.date_to),
            venues=args.venue,
            flat_only=args.flat_only,
            priority_venues=args.priority_venues,
            delay_seconds=args.delay,
            resume_from=args.resume_from,
        )
        if args.export:
            output = Path(args.output) if args.output else _default_history_dir()
            written = service.export_historical_races(batch, str(output), args.export)
            logger.info("historical export: %s", ", ".join(written.paths))

    if args.json:
        print_json(batch)
        return
    _print_history(batch)


def _cli_date(value: str | None) -> date | None:
    if not value:
        return None
    day, warning = parse_race_day(value.strip())
    if day is None or warning:
        print("日付は YYYYMMDD または YYYY-MM-DD で指定してください")
        sys.exit(1)
    return day


def _default_history_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "csv_output" / "historical"


def _print_history(batch: HistoricalBatch) -> None:
    print("\n=== 過去レース（バックテスト。予想の入力ではない） ===")
    span = ""
    if batch.date_from:
        span = f"{batch.date_from} から {batch.date_to}"
    print(f"範囲: {span or 'レースID'}")
    print(f"件数: {len(batch.races)}（範囲外 {batch.skipped}）")
    if batch.venues:
        print(f"競馬場: {'、'.join(batch.venues)}")
    print(f"平地のみ: {'はい' if batch.flat_only else 'いいえ'}")
    if batch.warning:
        print(f"警告: {batch.warning}")
    if batch.stopped_at_race_id or batch.remaining_race_ids:
        print(f"停止: {batch.stopped_at_race_id or 'レースIDなし'}")
        print(f"完了: {', '.join(batch.completed_race_ids) or 'なし'}")
        print(f"残り: {', '.join(batch.remaining_race_ids) or 'なし'}")
    for race in batch.races:
        _print_historical_race(race)


def _print_historical_race(race: HistoricalRace) -> None:
    settled = race.result
    name = settled.race_name or race.race_id
    surface = race.surface or "面不明"
    distance = f"{race.distance}m" if race.distance is not None else "距離不明"
    going = race.going or "馬場不明"
    race_class = race.race_class or "クラス不明"
    field = f"{race.field_size}頭" if race.field_size is not None else "頭数不明"
    print(
        f"\n--- {settled.race_date or '日付不明'} {settled.venue or '場不明'} "
        f"{name} ({surface}{distance} {race_class} {going} {field}) ---"
    )
    print(f"範囲内: {'はい' if race.in_scope else 'いいえ'}")
    print(f"確定オッズ: {'あり' if race.odds_final else 'なし'}")
    if race.warning:
        print(f"警告: {race.warning}")
    if not settled.results:
        print("着順: なし")
    for finisher in settled.results:
        print(_format_finisher(finisher))
    if settled.payouts:
        print("払戻: " + " / ".join(_format_payout(payout) for payout in settled.payouts))
    if settled.refunds:
        horses = "、".join(
            f"{number}番" for refund in settled.refunds for number in refund.horse_numbers
        )
        print(f"返還: {horses or '印字なし'}")


def cmd_trends(args):
    """Win rate, top-2, top-3, and ROI over settled history."""
    if args.today and (
        args.date or args.date_from or args.date_to or args.recent_meeting or args.as_of
    ):
        print("--today は Asia/Tokyo のその日だけです。ほかの日付と一緒には使えません")
        sys.exit(1)
    if not args.today and not args.recent_meeting and not args.date and not args.date_from:
        print("--today か --date か --from か --recent-meeting を指定してください")
        sys.exit(1)
    with _open_service(RaceService, args) as service:
        report = service.get_race_trends(
            venues=args.venue,
            surface=args.surface,
            distance=args.distance,
            race_class=args.race_class,
            going=args.going,
            on_date=None if args.today else _cli_date(args.date),
            date_from=_cli_date(args.date_from),
            date_to=_cli_date(args.date_to),
            recent_meeting=args.recent_meeting,
            as_of=_cli_date(args.as_of),
            today=args.today,
        )
    if args.json:
        print_json(report)
        return
    _print_trends(report)


def _print_trends(report: RaceTrendReport) -> None:
    print("\n=== レース傾向（確定結果の集計。予想の入力ではない） ===")
    print(f"モード: {report.mode}")
    print(f"レース数: {report.sample_size}  出走: {report.runner_sample_size}")
    if report.races_pending:
        print(f"未確定: {report.races_pending}")
    if report.included_races:
        print("集計に入れたレース:")
        for row in report.included_races:
            fetched = row.fetched_at.isoformat(timespec="seconds")
            print(f"  {row.race_id}  {row.source or 'ソースなし'}  {fetched}")
    if report.recent_meeting and report.recent_meeting.previous_meeting_date:
        print(
            f"当日: {report.recent_meeting.as_of}  "
            f"前開催日: {report.recent_meeting.previous_meeting_date}"
        )
    if report.source:
        print(f"ソース: {report.source}")
    print(f"計算時刻: {report.computed_at}")
    if report.fetched_at:
        print(f"取得時刻: {report.fetched_at}")
    elif report.fetched_at_min:
        print(f"取得時刻: {report.fetched_at_min} から {report.fetched_at_max}")
    if report.warning:
        print(f"警告: {report.warning}")
    if report.excluded:
        print("除外:")
        for item in report.excluded:
            print(f"  {item.race_id}: {item.reason}")
    combined = "（芝とダート）" if len(report.by_surface) > 1 else ""
    _print_trend_rows(f"人気{combined}", report.by_popularity, lambda row: row.band)
    _print_trend_rows(f"馬番{combined}", report.by_gate, lambda row: f"{row.horse_number}番")
    _print_trend_rows(f"枠{combined}", report.by_frame, lambda row: f"{row.bracket_number}枠")
    print(f"脚質の定義: {report.running_style_definition.position}")
    print(f"  逃げ: {report.running_style_definition.escape}")
    print(f"  先行: {report.running_style_definition.front_end}")
    print(f"  差し: {report.running_style_definition.mid_end}")
    print(f"  追込: {report.running_style_definition.closer}")
    _print_trend_rows(f"脚質{combined}", report.by_running_style, lambda row: row.label)
    if len(report.by_surface) > 1:
        for table in report.by_surface:
            _print_trend_rows(f"人気 {table.surface}", table.by_popularity, lambda row: row.band)
            _print_trend_rows(
                f"馬番 {table.surface}", table.by_gate, lambda row: f"{row.horse_number}番"
            )
            _print_trend_rows(
                f"枠 {table.surface}", table.by_frame, lambda row: f"{row.bracket_number}枠"
            )
            _print_trend_rows(
                f"脚質 {table.surface}", table.by_running_style, lambda row: row.label
            )
    _print_trend_rows(
        "騎手",
        report.by_jockey,
        lambda row: f"{row.jockey_name} {row.surface}",
    )
    _print_trend_rows(
        "父",
        report.by_sire,
        lambda row: f"{row.sire_name or row.sire_id or '父不明'} {row.surface}",
    )


def _print_trend_rows(title: str, rows: Sequence[Any], label: Callable[[Any], str]) -> None:
    print(f"\n--- {title} ---")
    if not rows:
        print("なし")
        return
    for row in rows:
        rates = _rate_text(row)
        warning = f" 警告: {row.warning}" if row.warning else ""
        print(f"{label(row)}  n={row.sample_size}  {rates}{warning}")


def _rate_text(row: TrendBucket) -> str:
    def _num(value: float | None) -> str:
        return "-" if value is None else f"{value:.4f}"

    return (
        f"勝 {_num(row.win_rate)} 連対 {_num(row.top2_rate)} "
        f"複勝率 {_num(row.top3_rate)} 単勝ROI {_num(row.win_roi)} "
        f"複勝ROI {_num(row.place_roi)}"
    )


def _prediction_store(args) -> PredictionStore:
    return PredictionStore(resolve_predictions_dir(getattr(args, "dir", None)))


def _read_prediction_text(file_name: str) -> str:
    if file_name == "-":
        return sys.stdin.read()
    path = Path(file_name)
    if not path.is_file():
        raise PredictionValidationError(f"prediction file not found: {file_name}")
    return path.read_text(encoding="utf-8")


def cmd_predictions_add(args):
    """Validate prediction records and append them. Duplicates are rejected."""
    store = _prediction_store(args)
    stored = store.add_text(_read_prediction_text(args.file))
    if args.json:
        print_json(stored)
        return
    for record in stored:
        day = date.fromisoformat(record["race"]["date"])
        print(f"追記 {record['record_id']} -> {store.path_for(day)}")


def cmd_predictions_show(args):
    """Print one meeting day's prediction records."""
    store = _prediction_store(args)
    shown = store.show(parse_meeting_date(args.date))
    if args.json:
        print_json(shown)
        return
    print(f"\n=== 予想記録 {shown['date']} ({len(shown['records'])}件) ===")
    if not shown["records"]:
        print("なし")
        return
    for record in shown["records"]:
        race = record["race"]
        result = record.get("result") or {}
        print(
            f"{record['record_id']} {race.get('venue') or ''} "
            f"{race.get('race_number')}R {record['run']['edition']} "
            f"result={result.get('status')}"
        )


def cmd_predictions_settle(args):
    """Fill pending results from the existing settled-race fetch. One fetch per race."""
    store = _prediction_store(args)
    day = parse_meeting_date(args.date)
    with _open_service(RaceService, args) as service:
        report = store.settle(day, lambda race_id: service.get_settled_race_result(race_id))
    if args.json:
        print_json(report)
        return
    print(f"\n=== 予想の精算 {report['date']} ===")
    print(f"確定にした記録: {len(report['settled'])}")
    for item in report["settled"]:
        print(f"  {item['record_id']}")
    print(f"未確定のまま: {len(report['pending'])}")
    for item in report["pending"]:
        phase = item["result_phase"] or "不明"
        warning = item["warning"] or ""
        print(f"  {item['record_id']} {phase} complete={item['complete']} {warning}".rstrip())


def cmd_venues(args):
    """Get venue schedules."""
    with _open_service(RaceService, args) as service:
        schedules = service.get_venue_schedules()
        print(f"\n=== 今週の開催競馬場 ({len(schedules)}場) ===\n")

        if args.json:
            print_json(schedules)
        else:
            for schedule in schedules:
                dates_str = ", ".join(str(d) for d in schedule.dates)
                print(f"【{schedule.venue}】 {dates_str}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="競馬スクレイピングCLIツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="詳細ログを出力")
    parser.add_argument("--json", action="store_true", help="JSON形式で出力")

    subparsers = parser.add_subparsers(dest="command", help="コマンド")
    jra_flags = _jra_fetch_flags()

    races_parser = subparsers.add_parser(
        "races", help="今週のレース一覧を取得", parents=[jra_flags]
    )
    races_parser.add_argument("--json", action="store_true", help="JSON形式で出力")
    races_parser.set_defaults(func=cmd_races)

    card_parser = subparsers.add_parser(
        "card",
        parents=[jra_flags],
        help="出馬表を取得",
        description=(
            "出馬表。race.track_bias はページにある競馬場・芝/ダート・距離・馬場状態を写す。"
            "内外とペースは accessD/JRADB と JRA 馬場情報にそのラベルがあるときだけ。"
            "外回り・使用コース・芝の傷み・オッズからは作らず、無いときは null。"
            "各馬の status は entered / scratched（取消）/ excluded（除外）/ unknown。"
            "status_label はページの文字のまま。JRA を優先し、無ければ netkeiba。"
        ),
    )
    card_parser.add_argument("race_id", help="レースID")
    card_parser.add_argument("--json", action="store_true", help="JSON形式で出力")
    card_parser.set_defaults(func=cmd_race_card)

    horse_parser = subparsers.add_parser(
        "horse",
        help="競走馬の近走・血統・脚質・調教を取得（確定したレース結果ではない）",
        description=(
            "競走馬の近走成績、血統（父・母・母父）、通過順から集計した脚質、調教。"
            "近走はログイン無しで見える成績だけ。netkeiba の資格情報は使わない。"
            "過去走であり、確定したレース結果ではない。着順と払戻の精算は result を使う。"
            "血統がページに無いときは null と warning。脚質はオッズやレース名からは作らない。"
            "調教は競馬ブックの調教ページと speedkeiba の今週の重賞記事。"
            "KEIBABOOK_LOGIN_ID と KEIBABOOK_PASSWORD、または KEIBABOOK_COOKIE があるときだけ"
            "馬個別の調教履歴を読む。未設定やログイン失敗は匿名のまま。"
            "umacd は出馬表か馬検索のリンクにあり、生年月日が一致するときだけ対応づける。"
            "当日のトラックバイアスは出さない（開催日が無いので accessD/JRADB を開かず、warning 付きの null）。"
            "馬場状態・芝ダート・距離は近走の各行と、出馬表の race.track_bias にある。"
        ),
    )
    horse_parser.add_argument("horse_id", help="馬ID")
    horse_parser.add_argument("--json", action="store_true", help="JSON形式で出力")
    horse_parser.set_defaults(func=cmd_horse)

    courses_parser = subparsers.add_parser("courses", help="コース情報を取得")
    courses_parser.add_argument("--venue", "-V", help="競馬場名")
    courses_parser.add_argument("--json", action="store_true", help="JSON形式で出力")
    courses_parser.set_defaults(func=cmd_courses)

    venues_parser = subparsers.add_parser("venues", help="開催競馬場を取得")
    venues_parser.add_argument("--json", action="store_true", help="JSON形式で出力")
    venues_parser.set_defaults(func=cmd_venues)

    track_parser = subparsers.add_parser("track", help="馬場情報を取得", parents=[jra_flags])
    track_parser.add_argument("--venue", "-V", help="競馬場名 (例: 中山)")
    track_parser.add_argument("--date", help="日付 YYYYMMDD (netkeiba)")
    track_parser.add_argument(
        "--merge", action="store_true", help="JRAとnetkeibaのデータを統合する"
    )
    track_parser.add_argument("--json", action="store_true", help="JSON形式で出力")
    track_parser.add_argument(
        "--wait-fresh",
        action="store_true",
        help="今日の開催で測定日がそれより前のときだけ取り直す。403 とログインは取り直さない",
    )
    track_parser.add_argument(
        "--retry-interval",
        type=_finite_arg("retry_interval"),
        default=60,
        help="再取得の間隔（秒）。0以上の有限値。60秒未満は60秒にする",
    )
    track_parser.add_argument(
        "--max-wait",
        type=_finite_arg("max_wait"),
        default=600,
        help="待ちの上限（秒）。0以上の有限値",
    )
    track_parser.set_defaults(func=cmd_track)

    result_parser = subparsers.add_parser(
        "result",
        parents=[jra_flags],
        help="確定した1レースの着順と払戻を取得（馬の近走成績ではない）",
        description=(
            "確定した1レースの全頭の着順と払戻。マーク×着順の事後ログ用。"
            "取消・除外・中止・失格は rank を null のまま status で区別する。"
            "cli horse の近走成績ではなく、レース前の期待値の入力に混ぜない。"
        ),
    )
    result_parser.add_argument("race_id", help="レースID")
    result_parser.add_argument("--json", action="store_true", help="JSON形式で出力")
    result_parser.add_argument(
        "--wait-complete",
        action="store_true",
        help="着順が出走頭数に揃うまで取り直す。403 とログインは取り直さない",
    )
    result_parser.add_argument(
        "--retry-interval",
        type=_finite_arg("retry_interval"),
        default=60,
        help="再取得の間隔（秒）。0以上の有限値。60秒未満は60秒にする",
    )
    result_parser.add_argument(
        "--max-wait",
        type=_finite_arg("max_wait"),
        default=600,
        help="待ちの上限（秒）。0以上の有限値",
    )
    result_parser.set_defaults(func=cmd_result)

    odds_parser = subparsers.add_parser(
        "odds",
        parents=[jra_flags],
        help="単勝・ワイド・馬連・三連複のオッズを取得",
        description=(
            "単勝・ワイド・馬連・三連複のオッズ引用。"
            "encoding は utf-8（オッズAPIのJSON。馬成績ページの EUC-JP ではない）。"
            "combination は ASCII の馬番（1、3-7、1-2-5）。"
            "race_phase は pre（API status の yoso と middle。レース前）か "
            "post（API status の result。レース後の確定）。"
            "HTTP 400、空の確定オッズ、壊れた JSON は quotes を空にし reason を返す。"
            "価格は作らない。ページが ---- または 取消 の行は odds を null のまま残す。"
            "取消・除外の馬番は non_runners。prefer は kind を書き換えない。"
            "prefer=live で市場が無いとき yoso へは自動で切り替えない。"
        ),
    )
    odds_parser.add_argument("race_id", help="レースID")
    odds_parser.add_argument(
        "--bet-types",
        default="win",
        help=(
            "馬券種（カンマ区切り。win、wide、umaren、sanrenpuku）。"
            "応答の encoding は utf-8、race_phase は pre か post。"
            "prefer=live で取れないとき yoso へは切り替えない"
        ),
    )
    odds_parser.add_argument(
        "--prefer",
        default="live",
        choices=["live", "yoso"],
        help="優先する種別。kind は書き換えない。live が無いとき yoso は明示して取る",
    )
    odds_parser.add_argument("--json", action="store_true", help="JSON形式で出力")
    odds_parser.set_defaults(func=cmd_odds)

    history_parser = subparsers.add_parser(
        "history",
        parents=[jra_flags],
        help="過去の平地レースの確定オッズ・払戻・全着順を取得する",
        description=(
            "EV のバックテスト用。2025年以降の JRA 平地が対象。"
            "race_id、または --date / --from と --to。"
            "確定オッズはページが確定オッズと出すか、レース後のページのときだけ。"
            "発売中のオッズは確定にしない。欠測は null と警告。"
            "日付の範囲は1レースずつ、間隔を空け、ページはローカルにキャッシュする。"
            "結果ページか開催一覧の 403 とログイン画面で止める。同じ実行では再試行しない。"
            "再実行はキャッシュ済みのレースを取り直さず、止めたレースから続ける。"
            "--resume-from はそれより前をキャッシュだけにする。"
            "--venue で競馬場を絞る。--priority-venues は東京・京都・中山・阪神。"
            "--flat-only が既定。障害は --no-flat-only。"
            "--export jsonl か csv は芝・ダートで校正できる形。"
        ),
    )
    history_parser.add_argument("race_id", nargs="?", help="レースID")
    history_parser.add_argument("--date", help="その日だけ。YYYYMMDD または YYYY-MM-DD")
    history_parser.add_argument(
        "--from", dest="date_from", help="開始日。YYYYMMDD または YYYY-MM-DD"
    )
    history_parser.add_argument(
        "--to", dest="date_to", help="終了日。含む。YYYYMMDD または YYYY-MM-DD"
    )
    history_parser.add_argument(
        "--venue",
        action="append",
        help="競馬場。繰り返す。優先は東京、京都、中山、阪神",
    )
    history_parser.add_argument(
        "--priority-venues",
        action="store_true",
        help="東京・京都・中山・阪神だけ",
    )
    history_parser.add_argument(
        "--flat-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="障害を除く（既定）。--no-flat-only で障害も残す",
    )
    history_parser.add_argument(
        "--resume-from",
        help="このレースIDからネットワークを開く。それより前はキャッシュだけ",
    )
    history_parser.add_argument(
        "--delay",
        type=float,
        default=1.5,
        help="日付範囲でレースとレースのあいだに待つ秒数",
    )
    history_parser.add_argument(
        "--export",
        choices=["jsonl", "csv"],
        help="jsonl は1レース1行。csv は馬ごとの行と、芝・ダート別のファイル",
    )
    history_parser.add_argument(
        "--output",
        help="書き出し先。既定は backend/csv_output/historical",
    )
    history_parser.add_argument("--json", action="store_true", help="JSON形式で出力")
    history_parser.set_defaults(func=cmd_history)

    trends_parser = subparsers.add_parser(
        "trends",
        parents=[jra_flags],
        help="確定レースの人気・枠・脚質・騎手・父の勝率と回収を集計する",
        description=(
            "history の確定着順・確定オッズ・払戻と、ローカルキャッシュから集計する。"
            "人気帯、馬番、枠、脚質、騎手、父。surface を付けないときは人気・馬番・枠・脚質を"
            "芝とダートに分け、合わせた表も出す。騎手と父も面を分けて出す。"
            "脚質は通過順の最初の数字と頭数の三等分。4コーナーは使わない。"
            "少ない標本も sample_size 付きで残す。計算できない値は null と warning。"
            "入れるのは result_phase が confirmed かつ complete が true のレースだけ。"
            "未確定は races_pending に数え、着順は作らない。"
            "403 とログイン画面は再試行せず、User-Agent の差し替えや captcha はしない。"
            "--today は今日（Asia/Tokyo）の1日。--date もその開催日だけ。"
            "未確定の result.html と、今日の開催一覧はキャッシュから返さない。"
            "--recent-meeting は当日の確定と、同じ競馬場の前開催日だけ。"
        ),
    )
    trends_parser.add_argument(
        "--today",
        action="store_true",
        help="開催日は今日（Asia/Tokyo）だけ。ほかの日付と一緒には使えない",
    )
    trends_parser.add_argument("--date", help="その日だけ。YYYYMMDD または YYYY-MM-DD")
    trends_parser.add_argument(
        "--from", dest="date_from", help="開始日。YYYYMMDD または YYYY-MM-DD"
    )
    trends_parser.add_argument(
        "--to", dest="date_to", help="終了日。含む。YYYYMMDD または YYYY-MM-DD"
    )
    trends_parser.add_argument("--venue", action="append", help="競馬場。繰り返す")
    trends_parser.add_argument("--surface", choices=["芝", "ダート"], help="芝またはダート")
    trends_parser.add_argument("--distance", type=int, help="距離 (m)")
    trends_parser.add_argument("--class", dest="race_class", help="クラス。印字との一致")
    trends_parser.add_argument("--going", choices=["良", "稍重", "重", "不良"], help="馬場状態")
    trends_parser.add_argument(
        "--recent-meeting",
        action="store_true",
        help="当日の確定レースと、同じ競馬場の前開催日だけ",
    )
    trends_parser.add_argument(
        "--as-of",
        help="recent meeting の当日。YYYYMMDD または YYYY-MM-DD。無ければ今日（Asia/Tokyo）",
    )
    trends_parser.add_argument("--json", action="store_true", help="JSON形式で出力")
    trends_parser.set_defaults(func=cmd_trends)

    predictions_parser = subparsers.add_parser(
        "predictions",
        help="予想記録の保存と、確定結果への精算",
        description=(
            "EV が書いた予想を開催日ごとの JSONL に残す。"
            "確率、期待値、買うかどうか、金額、rules の中身はこのコマンドでは計算しない。"
            "settle は既存の確定結果の取得が confirmed かつ complete のときだけ result を埋める。"
            "それ以外は行を変えず、報告する。401 / 403 とログインは取り直さない。"
        ),
    )
    pred_sub = predictions_parser.add_subparsers(dest="predictions_command", required=True)

    def _prediction_flags(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--dir",
            help="JSONL のディレクトリ。無ければ HRP_PREDICTIONS_DIR、それも無ければ ./predictions",
        )
        target.add_argument("--json", action="store_true", help="JSON形式で出力")

    add_parser = pred_sub.add_parser(
        "add",
        help="検証して追記する。同じ record_id は拒否する",
    )
    add_parser.add_argument("file", help="JSON、JSON配列、または JSONL。- は標準入力")
    _prediction_flags(add_parser)
    add_parser.set_defaults(func=cmd_predictions_add)

    show_parser = pred_sub.add_parser("show", help="開催日の予想記録を表示する")
    show_parser.add_argument("--date", required=True, help="YYYYMMDD または YYYY-MM-DD")
    _prediction_flags(show_parser)
    show_parser.set_defaults(func=cmd_predictions_show)

    settle_parser = pred_sub.add_parser(
        "settle",
        parents=[jra_flags],
        help="確定かつ complete のレースだけ result を埋める",
    )
    settle_parser.add_argument("--date", required=True, help="YYYYMMDD または YYYY-MM-DD")
    _prediction_flags(settle_parser)
    settle_parser.set_defaults(func=cmd_predictions_settle)

    args = parser.parse_args()

    setup_logging(args.verbose)

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\n中断しました")
        sys.exit(1)
    except Exception as e:
        logger.error(f"エラーが発生しました: {e}")
        if args.verbose:
            raise
        sys.exit(1)


if __name__ == "__main__":
    main()
