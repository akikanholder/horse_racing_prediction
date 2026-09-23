#!/usr/bin/env python3
"""CLI tool for testing horse racing scrapers."""

import argparse
import json
import logging
import sys
from datetime import date

from backend.services.race_service import RaceService

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
        print(f"出走頭数: {len(race_card.entries)}\n")

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
        if horse.birth_year:
            print(f"生年: {horse.birth_year}年")
        if horse.sire_name:
            print(f"父: {horse.sire_name}")
        if horse.dam_name:
            print(f"母: {horse.dam_name}")
        if horse.damsire_name:
            print(f"母父: {horse.damsire_name}")
        if horse.trainer_name:
            print(f"調教師: {horse.trainer_name}")
        if horse.training_center:
            print(f"所属: {horse.training_center}")
        print(f"通算成績: {horse.total_races}戦{horse.total_wins}勝")

        if args.json:
            print_json(horse)
        else:
            print("\n--- 近走成績 ---")
            for perf in horse.performances[:5]:
                result = f"{perf.rank}着" if perf.rank else "---"
                print(
                    f"{perf.race_date} {perf.venue} {perf.race_name} "
                    f"({perf.track_type}{perf.distance}m) → {result}"
                )


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
                            f"  芝: 周回{course.turf_circumference}m / "
                            f"直線{course.turf_straight}m"
                        )
                    if course.dirt_circumference:
                        print(
                            f"  ダート: 周回{course.dirt_circumference}m / "
                            f"直線{course.dirt_straight}m"
                        )


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

    card_parser = subparsers.add_parser("card", help="出馬表を取得")
    card_parser.add_argument("race_id", help="レースID")
    card_parser.add_argument("--json", action="store_true", help="JSON形式で出力")
    card_parser.set_defaults(func=cmd_race_card)

    horse_parser = subparsers.add_parser("horse", help="競走馬情報を取得")
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
