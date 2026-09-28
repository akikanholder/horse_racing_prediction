#!/usr/bin/env python3
"""CLI tool for testing horse racing scrapers."""

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from datetime import date

from backend.models.horse import Horse, HorsePerformance, PedigreeHorse
from backend.models.odds import OddsQuote
from backend.models.race import RacePayout, SettledFinisher, SettledRaceResult, TrackBias
from backend.models.running_style import RunningStyle
from backend.services.race_service import RaceService
from backend.services.track_info_service import TrackInfoService

logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False):
    """Setup logging configuration."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


def print_json(data, indent: int = 2):
    """Print data as formatted JSON."""
    if hasattr(data, "model_dump"):
        data = data.model_dump()
    elif hasattr(data, "__iter__") and not isinstance(data, (str, dict)):
        data = [d.model_dump() if hasattr(d, "model_dump") else d for d in data]

    def date_handler(obj):
        if isinstance(obj, date):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

    print(json.dumps(data, indent=indent, ensure_ascii=False, default=date_handler))


def cmd_races(args):
    """Get weekly races."""
    with RaceService() as service:
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
    with RaceService() as service:
        race_card = service.get_race_card(args.race_id)
        if not race_card:
            print(f"レース {args.race_id} が見つかりません")
            return

        print(f"\n=== 出馬表: {race_card.race.race_name} ===")
        print(f"開催日: {race_card.race.race_date}")
        print(f"競馬場: {race_card.race.venue}")
        print(f"コース: {race_card.race.track_type} {race_card.race.distance}m")
        print(f"出走頭数: {len(race_card.entries)}")
        if race_card.race.track_bias:
            _print_track_bias(race_card.race.track_bias)
        print()

        if args.json:
            print_json(race_card)
        else:
            for entry in race_card.entries:
                print(
                    f"{entry.bracket_number}枠 {entry.horse_number}番 "
                    f"{entry.horse_name} ({entry.sex}{entry.age}) "
                    f"[{entry.jockey_name}]"
                )


def cmd_horse(args):
    """Get horse information."""
    with RaceService() as service:
        horse = service.get_horse_info(args.horse_id)
        if not horse:
            print(f"馬 {args.horse_id} が見つかりません")
            return

        print(f"\n=== 競走馬情報: {horse.horse_name} ===")
        print(f"馬ID: {horse.horse_id}")
        print(f"性別: {horse.sex}")
        print(f"取得時刻: {horse.fetched_at}")
        if horse.birth_year:
            print(f"生年: {horse.birth_year}年")
        _print_pedigree(horse)
        _print_running_style(horse.running_style)
        _print_track_bias(horse.track_bias)
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


def _print_track_bias(bias: TrackBias) -> None:
    """Conditions that were on the page, and the reason bias itself is null."""
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
    print(f"トラックバイアス: 内外なし / ペースバイアスなし（{condition}）")
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
    with RaceService() as service:
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


def cmd_track(args):
    """Get detailed track (baba) information."""
    with TrackInfoService() as service:
        results = service.get_track_info(
            racecourse=args.venue,
            date=args.date,
            merge=args.merge,
        )
        print("\n=== 馬場情報 ===")
        if args.venue:
            print(f"競馬場: {args.venue}")
        print(f"ソース: {', '.join(results.keys()) or 'なし'}\n")

        if args.json:
            print_json(results)
            return

        for source, info in results.items():
            print(f"[{source}] {info.racecourse} 天候: {info.weather.value}")
            if info.turf:
                cushion = ""
                if info.turf.cushion and info.turf.cushion.value is not None:
                    cushion = (
                        f" クッション値: {info.turf.cushion.value}"
                        f" ({info.turf.cushion.hardness_level})"
                    )
                print(f"  芝: {info.turf.condition.value}{cushion}")
            if info.dirt:
                print(f"  ダート: {info.dirt.condition.value}")


def cmd_odds(args):
    """Get win, wide, umaren, and sanrenpuku odds quotes."""
    with RaceService() as service:
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
    if quote.bet_type == "wide":
        return f"ワイド {quote.combination} {quote.odds_min}-{quote.odds_max} {tag}"
    if quote.bet_type == "umaren":
        return f"馬連 {quote.combination} {quote.odds} {tag}"
    if quote.bet_type == "sanrenpuku":
        return f"三連複 {quote.combination} {quote.odds} {tag}"
    return f"{quote.combination}番 {quote.odds} {tag}"


def cmd_result(args):
    """Get the settled result of one race.

    This is post-race settlement for mark × finish logs.
    ``cli horse`` is that horse's past runs and is not this command.
    """
    with RaceService() as service:
        settled = service.get_settled_race_result(args.race_id)

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
    rank = f"{finisher.rank}着" if finisher.rank is not None else "---"
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


def cmd_venues(args):
    """Get venue schedules."""
    with RaceService() as service:
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

    races_parser = subparsers.add_parser("races", help="今週のレース一覧を取得")
    races_parser.add_argument("--json", action="store_true", help="JSON形式で出力")
    races_parser.set_defaults(func=cmd_races)

    card_parser = subparsers.add_parser(
        "card",
        help="出馬表を取得",
        description=(
            "出馬表。race.track_bias はページにある競馬場・芝/ダート・距離・馬場状態を写す。"
            "内外と先行有利などの当日バイアスは accessD/JRADB が無く null。"
        ),
    )
    card_parser.add_argument("race_id", help="レースID")
    card_parser.add_argument("--json", action="store_true", help="JSON形式で出力")
    card_parser.set_defaults(func=cmd_race_card)

    horse_parser = subparsers.add_parser(
        "horse",
        help="競走馬の近走・血統・脚質を取得（確定したレース結果ではない）",
        description=(
            "競走馬の近走成績、血統（父・母・母父）、通過順から集計した脚質。"
            "過去走であり、確定したレース結果ではない。着順と払戻の精算は result を使う。"
            "血統がページに無いときは null と warning。脚質はオッズやレース名からは作らない。"
            "当日のトラックバイアスは出さない（accessD/JRADB が無く、warning 付きの null）。"
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

    track_parser = subparsers.add_parser("track", help="馬場情報を取得")
    track_parser.add_argument("--venue", "-V", help="競馬場名 (例: 中山)")
    track_parser.add_argument("--date", help="日付 YYYYMMDD (netkeiba)")
    track_parser.add_argument(
        "--merge", action="store_true", help="JRAとnetkeibaのデータを統合する"
    )
    track_parser.add_argument("--json", action="store_true", help="JSON形式で出力")
    track_parser.set_defaults(func=cmd_track)

    result_parser = subparsers.add_parser(
        "result",
        help="確定した1レースの着順と払戻を取得（馬の近走成績ではない）",
        description=(
            "確定した1レースの着順と払戻。マーク×着順の事後ログ用。"
            "cli horse の近走成績ではなく、レース前の期待値の入力に混ぜない。"
        ),
    )
    result_parser.add_argument("race_id", help="レースID")
    result_parser.add_argument("--json", action="store_true", help="JSON形式で出力")
    result_parser.set_defaults(func=cmd_result)

    odds_parser = subparsers.add_parser(
        "odds",
        help="単勝・ワイド・馬連・三連複のオッズを取得",
        description=(
            "単勝・ワイド・馬連・三連複のオッズ引用。"
            "encoding は utf-8（オッズAPIのJSON。馬成績ページの EUC-JP ではない）。"
            "combination は ASCII の馬番（1、3-7、1-2-5）。"
            "race_phase は pre（API status の yoso と middle。レース前）か "
            "post（API status の result。レース後の確定）。"
            "HTTP 400、空の確定オッズ、壊れた JSON は quotes を空にし reason を返す。"
            "価格は作らない。prefer は kind を書き換えない。"
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
