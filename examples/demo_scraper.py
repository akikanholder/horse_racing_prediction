#!/usr/bin/env python3
"""Demo script showing how to use the track information scrapers."""

import json
import logging
from datetime import datetime

from src.scrapers import (
    JRAScraper,
    NetkeibaScraper,
    TrackInfoService,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


def demo_jra_scraper():
    """Demonstrate JRA scraper usage."""
    print("\n" + "=" * 60)
    print("JRA Scraper Demo")
    print("=" * 60)

    with JRAScraper() as scraper:
        print("\nAvailable racecourses:")
        for code, name in scraper.get_available_racecourses():
            print(f"  - {name} ({code})")

        print("\n--- Scraping current track info ---")
        track_info = scraper.scrape()
        print(f"\nRacecourse: {track_info.racecourse}")
        print(f"Date: {track_info.date}")
        print(f"Weather: {track_info.weather.value}")

        if track_info.turf:
            print(f"\nTurf Track:")
            print(f"  Condition: {track_info.turf.condition.value}")
            if track_info.turf.cushion:
                print(
                    f"  Cushion Value: {track_info.turf.cushion.value} "
                    f"({track_info.turf.cushion.hardness_level})"
                )
            if track_info.turf.moisture:
                print(
                    f"  Moisture - Goal: {track_info.turf.moisture.goal_front}, "
                    f"4th Corner: {track_info.turf.moisture.fourth_corner}"
                )

        if track_info.dirt:
            print(f"\nDirt Track:")
            print(f"  Condition: {track_info.dirt.condition.value}")
            if track_info.dirt.moisture:
                print(
                    f"  Moisture - Goal: {track_info.dirt.moisture.goal_front}, "
                    f"4th Corner: {track_info.dirt.moisture.fourth_corner}"
                )

        if track_info.grass_info:
            print(f"\nGrass Info:")
            if track_info.grass_info.course_name:
                print(f"  Course: {track_info.grass_info.course_name}")
            if track_info.grass_info.native_grass_cm:
                print(
                    f"  Native Grass: "
                    f"{track_info.grass_info.native_grass_cm[0]}-"
                    f"{track_info.grass_info.native_grass_cm[1]} cm"
                )


def demo_netkeiba_scraper():
    """Demonstrate Netkeiba scraper usage."""
    print("\n" + "=" * 60)
    print("Netkeiba Scraper Demo")
    print("=" * 60)

    with NetkeibaScraper() as scraper:
        print("\nAvailable racecourses:")
        for id_, name in scraper.get_available_racecourses():
            print(f"  - {name} (ID: {id_})")

        print("\n--- Scraping current track info ---")
        track_info = scraper.scrape()
        print(f"\nRacecourse: {track_info.racecourse}")
        print(f"Date: {track_info.date}")
        print(f"Weather: {track_info.weather.value}")

        if track_info.turf:
            print(f"\nTurf Track:")
            print(f"  Condition: {track_info.turf.condition.value}")
            if track_info.turf.cushion:
                print(
                    f"  Cushion Value: {track_info.turf.cushion.value} "
                    f"({track_info.turf.cushion.hardness_level})"
                )
            if track_info.turf.moisture:
                print(
                    f"  Moisture - Goal: {track_info.turf.moisture.goal_front}, "
                    f"4th Corner: {track_info.turf.moisture.fourth_corner}"
                )

        if track_info.dirt:
            print(f"\nDirt Track:")
            print(f"  Condition: {track_info.dirt.condition.value}")
            if track_info.dirt.moisture:
                print(
                    f"  Moisture - Goal: {track_info.dirt.moisture.goal_front}, "
                    f"4th Corner: {track_info.dirt.moisture.fourth_corner}"
                )


def demo_track_info_service():
    """Demonstrate TrackInfoService usage."""
    print("\n" + "=" * 60)
    print("Track Info Service Demo (Aggregated Data)")
    print("=" * 60)

    with TrackInfoService() as service:
        print("\nAvailable racecourses:")
        for course in service.get_all_racecourses():
            print(
                f"  - {course['name']}: "
                f"JRA={course['jra_code']}, Netkeiba={course['netkeiba_id']}"
            )

        print("\n--- Fetching from both sources ---")
        results = service.get_track_info(merge=True)

        for source, info in results.items():
            print(f"\n[{source.upper()}]")
            print(f"  Racecourse: {info.racecourse}")
            print(f"  Weather: {info.weather.value}")
            if info.turf:
                print(f"  Turf Condition: {info.turf.condition.value}")
            if info.dirt:
                print(f"  Dirt Condition: {info.dirt.condition.value}")


def demo_json_output():
    """Demonstrate JSON serialization of track info."""
    print("\n" + "=" * 60)
    print("JSON Output Demo")
    print("=" * 60)

    with JRAScraper() as scraper:
        track_info = scraper.scrape()
        json_output = track_info.model_dump_json(indent=2, exclude_none=True)
        print("\nJSON representation:")
        print(json_output)


if __name__ == "__main__":
    print("Horse Racing Track Information Scraper Demo")
    print("============================================")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        demo_jra_scraper()
        demo_netkeiba_scraper()
        demo_track_info_service()
        demo_json_output()
    except Exception as e:
        print(f"\nError occurred: {e}")
        raise

    print("\n" + "=" * 60)
    print("Demo completed successfully!")
    print("=" * 60)
