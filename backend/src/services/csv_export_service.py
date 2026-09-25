"""Export one day's race information to per-venue CSV files."""

import csv
import json
import logging
import re
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from backend.models.course import Course
from backend.models.export import CsvExportFile, DailyRaceCsvExport, VenueCsvExport
from backend.models.horse import Horse
from backend.models.race import Race, RaceCard, RaceResult
from backend.models.track import TrackInfo
from backend.services.race_service import RaceService
from backend.services.track_info_service import TrackInfoService

logger = logging.getLogger(__name__)

# backend/src/services/csv_export_service.py -> backend/
DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parents[2] / "csv_output"

RACE_FIELDS = [
    "race_id",
    "race_name",
    "race_number",
    "race_date",
    "post_time",
    "venue",
    "venue_code",
    "round_number",
    "day_number",
    "distance",
    "track_type",
    "direction",
    "track_condition",
    "weather",
    "race_class",
    "race_grade",
    "age_condition",
    "sex_condition",
    "weight_condition",
    "prize_money",
    "entry_count",
]

ENTRY_FIELDS = [
    "race_id",
    "race_date",
    "venue",
    "race_number",
    "race_name",
    "bracket_number",
    "horse_number",
    "horse_name",
    "horse_id",
    "sex",
    "age",
    "weight",
    "jockey_name",
    "jockey_id",
    "trainer_name",
    "trainer_id",
    "owner_name",
    "horse_weight",
    "horse_weight_diff",
    "odds",
    "popularity",
]

RESULT_FIELDS = [
    "race_id",
    "race_date",
    "venue",
    "race_number",
    "race_name",
    "rank",
    "bracket_number",
    "horse_number",
    "horse_name",
    "horse_id",
    "finish_time",
    "margin",
    "passing_order",
    "last_3f",
    "jockey_name",
    "odds",
    "popularity",
]

HORSE_FIELDS = [
    "horse_id",
    "horse_name",
    "sex",
    "age",
    "trainer_name",
    "trainer_id",
    "owner_name",
    "sire_name",
    "dam_name",
    "damsire_name",
    "training_center",
    "race_ids",
]

COURSE_FIELDS = [
    "venue_code",
    "venue_name",
    "location",
    "established",
    "capacity",
    "turf_circumference",
    "dirt_circumference",
    "turf_straight",
    "dirt_straight",
    "turf_condition",
    "turf_moisture",
    "dirt_condition",
    "dirt_moisture",
    "weather",
    "temperature",
    "condition_date",
]

TRACK_FIELDS = [
    "racecourse",
    "date",
    "weather",
    "source",
    "scraped_at",
    "turf_condition",
    "turf_moisture_goal",
    "turf_moisture_corner",
    "turf_cushion",
    "turf_cushion_hardness",
    "dirt_condition",
    "dirt_moisture_goal",
    "dirt_moisture_corner",
    "grass_course",
    "grass_native_cm",
    "grass_western_cm",
    "grass_notes",
    "weekly_weather",
]


class RaceCsvExportError(Exception):
    """Base error for CSV export."""


class NoRacesFoundError(RaceCsvExportError):
    """Raised when the requested date has no races."""

    def __init__(self, target_date: date):
        self.target_date = target_date
        super().__init__(f"No races found for {target_date.isoformat()}")


class CsvExportService:
    """Write all race information for one date into date/venue CSV files."""

    def __init__(
        self,
        race_service: RaceService,
        track_info_service: TrackInfoService | None = None,
        output_root: Path | None = None,
        include_horse_details: bool = False,
    ):
        """Initialize the exporter.

        Args:
            race_service: Source of races, cards, results, and courses
            track_info_service: Source of track (baba) information
            output_root: Directory that contains one folder per date
            include_horse_details: Fetch pedigree pages for each runner
        """
        self.race_service = race_service
        self.track_info_service = track_info_service
        self.output_root = output_root or DEFAULT_OUTPUT_ROOT
        self.include_horse_details = include_horse_details

    def export_date(self, target_date: date) -> DailyRaceCsvExport:
        """Export every race on ``target_date`` to CSV files.

        Layout::

            {output_root}/{YYYY-MM-DD}/{venue}/races.csv
            {output_root}/{YYYY-MM-DD}/{venue}/entries.csv
            {output_root}/{YYYY-MM-DD}/{venue}/results.csv
            {output_root}/{YYYY-MM-DD}/{venue}/horses.csv
            {output_root}/{YYYY-MM-DD}/{venue}/courses.csv
            {output_root}/{YYYY-MM-DD}/{venue}/tracks.csv

        Args:
            target_date: Date whose races should be exported

        Returns:
            Summary of the directories and files that were written

        Raises:
            NoRacesFoundError: When no race is available for the date
        """
        races = self.race_service.get_races_for_date(target_date)
        if not races:
            raise NoRacesFoundError(target_date)

        grouped: dict[str, list[Race]] = defaultdict(list)
        for race in races:
            grouped[race.venue or "不明"].append(race)

        date_dir = self.output_root / target_date.isoformat()
        date_dir.mkdir(parents=True, exist_ok=True)

        warnings: list[str] = []
        venues: list[VenueCsvExport] = []
        for venue_name in sorted(grouped):
            venue_races = sorted(grouped[venue_name], key=lambda race: race.race_number)
            venue_dir = date_dir / _safe_dirname(venue_name)
            venue_dir.mkdir(parents=True, exist_ok=True)
            files, venue_warnings = self._write_venue(
                venue_dir, target_date, venue_name, venue_races
            )
            warnings.extend(venue_warnings)
            venues.append(
                VenueCsvExport(
                    venue=venue_name,
                    directory=str(venue_dir),
                    race_count=len(venue_races),
                    files=files,
                )
            )

        return DailyRaceCsvExport(
            race_date=target_date,
            output_directory=str(date_dir),
            race_count=len(races),
            venue_count=len(venues),
            venues=venues,
            warnings=warnings,
        )

    def _write_venue(
        self,
        venue_dir: Path,
        target_date: date,
        venue_name: str,
        races: list[Race],
    ) -> tuple[list[CsvExportFile], list[str]]:
        warnings: list[str] = []
        race_rows: list[dict] = []
        entry_rows: list[dict] = []
        result_rows: list[dict] = []
        horses: dict[str, dict] = {}

        for race in races:
            card, results = self.race_service.get_race_export_bundle(race.race_id)
            race_obj = card.race if card else race
            race_rows.append(_model_row(race_obj, RACE_FIELDS))
            if card:
                self._collect_entries(card, entry_rows, horses)
            else:
                warnings.append(f"{venue_name} {race.race_id}: 出馬表を取得できませんでした")
            if results:
                self._collect_results(race_obj, results, result_rows)
            else:
                warnings.append(f"{venue_name} {race.race_id}: レース結果を取得できませんでした")

        if self.include_horse_details:
            self._fill_horse_details(horses, warnings)

        course_rows, course_warnings = self._course_rows(venue_name)
        warnings.extend(course_warnings)
        track_rows, track_warnings = self._track_rows(venue_name, target_date)
        warnings.extend(track_warnings)

        written = [
            _write_csv(venue_dir / "races.csv", RACE_FIELDS, race_rows),
            _write_csv(venue_dir / "entries.csv", ENTRY_FIELDS, entry_rows),
            _write_csv(venue_dir / "results.csv", RESULT_FIELDS, result_rows),
            _write_csv(venue_dir / "horses.csv", HORSE_FIELDS, list(horses.values())),
            _write_csv(venue_dir / "courses.csv", COURSE_FIELDS, course_rows),
            _write_csv(venue_dir / "tracks.csv", TRACK_FIELDS, track_rows),
        ]
        return written, warnings

    def _collect_entries(
        self,
        card: RaceCard,
        entry_rows: list[dict],
        horses: dict[str, dict],
    ) -> None:
        race = card.race
        for entry in card.entries:
            row = _model_row(entry, ENTRY_FIELDS)
            row.update(
                {
                    "race_id": race.race_id,
                    "race_date": race.race_date.isoformat(),
                    "venue": race.venue,
                    "race_number": race.race_number,
                    "race_name": race.race_name,
                }
            )
            entry_rows.append(row)
            key = entry.horse_id or f"{race.race_id}:{entry.horse_number}:{entry.horse_name}"
            horse_row = horses.get(key)
            if horse_row is None:
                horse_row = dict.fromkeys(HORSE_FIELDS, "")
                horse_row.update(
                    {
                        "horse_id": entry.horse_id or "",
                        "horse_name": entry.horse_name,
                        "sex": entry.sex or "",
                        "age": _cell(entry.age),
                        "trainer_name": entry.trainer_name or "",
                        "trainer_id": entry.trainer_id or "",
                        "owner_name": entry.owner_name or "",
                        "race_ids": race.race_id,
                    }
                )
                horses[key] = horse_row
            elif race.race_id not in horse_row["race_ids"].split(";"):
                horse_row["race_ids"] = f"{horse_row['race_ids']};{race.race_id}"

    def _collect_results(
        self,
        race: Race,
        results: list[RaceResult],
        result_rows: list[dict],
    ) -> None:
        for result in results:
            row = _model_row(result, RESULT_FIELDS)
            row.update(
                {
                    "race_id": race.race_id,
                    "race_date": race.race_date.isoformat(),
                    "venue": race.venue,
                    "race_number": race.race_number,
                    "race_name": race.race_name,
                }
            )
            result_rows.append(row)

    def _fill_horse_details(self, horses: dict[str, dict], warnings: list[str]) -> None:
        for key, row in horses.items():
            horse_id = row.get("horse_id") or ""
            if not horse_id:
                continue
            try:
                detail: Horse | None = self.race_service.get_horse_info(horse_id)
            except Exception as e:
                warnings.append(f"競走馬 {horse_id} の詳細取得に失敗しました: {e}")
                continue
            if not detail:
                warnings.append(f"競走馬 {key} の詳細を取得できませんでした")
                continue
            row["sire_name"] = detail.sire_name or ""
            row["dam_name"] = detail.dam_name or ""
            row["damsire_name"] = detail.damsire_name or ""
            row["training_center"] = detail.training_center or ""
            if detail.trainer_name:
                row["trainer_name"] = detail.trainer_name

    def _course_rows(self, venue_name: str) -> tuple[list[dict], list[str]]:
        try:
            course = self.race_service.get_course_info(venue_name)
        except Exception as e:
            return [], [f"{venue_name}: コース情報の取得に失敗しました: {e}"]
        if course is None:
            return [], [f"{venue_name}: コース情報を取得できませんでした"]
        return [_course_row(course)], []

    def _track_rows(
        self, venue_name: str, target_date: date
    ) -> tuple[list[dict], list[str]]:
        if self.track_info_service is None:
            return [], [f"{venue_name}: 馬場情報サービスが未設定です"]
        try:
            tracks = self.track_info_service.get_track_info(
                racecourse=venue_name,
                date=target_date.strftime("%Y%m%d"),
                merge=True,
            )
        except Exception as e:
            return [], [f"{venue_name}: 馬場情報の取得に失敗しました: {e}"]
        if not tracks:
            return [], [f"{venue_name}: 馬場情報を取得できませんでした"]
        return [_track_row(info) for info in tracks.values()], []


def _course_row(course: Course) -> dict:
    row = _model_row(course, COURSE_FIELDS)
    condition = course.conditions[-1] if course.conditions else None
    if condition:
        row["turf_condition"] = condition.turf_condition or ""
        row["turf_moisture"] = _cell(condition.turf_moisture)
        row["dirt_condition"] = condition.dirt_condition or ""
        row["dirt_moisture"] = _cell(condition.dirt_moisture)
        row["weather"] = condition.weather or ""
        row["temperature"] = _cell(condition.temperature)
        row["condition_date"] = condition.condition_date.isoformat()
    return row


def _track_row(info: TrackInfo) -> dict:
    turf = info.turf
    dirt = info.dirt
    grass = info.grass_info
    weekly = [
        {
            "date": item.date.isoformat(),
            "weather": item.weather.value,
            "rainfall_mm": item.rainfall_mm,
            "watering": item.watering,
        }
        for item in info.weekly_info
    ]
    cushion = turf.cushion if turf and turf.cushion else None
    return {
        "racecourse": info.racecourse,
        "date": info.date.date().isoformat()
        if isinstance(info.date, datetime)
        else str(info.date),
        "weather": info.weather.value,
        "source": info.source,
        "scraped_at": info.scraped_at.isoformat(),
        "turf_condition": turf.condition.value if turf else "",
        "turf_moisture_goal": _cell(turf.moisture.goal_front) if turf and turf.moisture else "",
        "turf_moisture_corner": _cell(turf.moisture.fourth_corner)
        if turf and turf.moisture
        else "",
        "turf_cushion": _cell(cushion.value) if cushion else "",
        "turf_cushion_hardness": cushion.hardness_level if cushion else "",
        "dirt_condition": dirt.condition.value if dirt else "",
        "dirt_moisture_goal": _cell(dirt.moisture.goal_front) if dirt and dirt.moisture else "",
        "dirt_moisture_corner": _cell(dirt.moisture.fourth_corner)
        if dirt and dirt.moisture
        else "",
        "grass_course": grass.course_name if grass and grass.course_name else "",
        "grass_native_cm": _pair(grass.native_grass_cm) if grass else "",
        "grass_western_cm": _pair(grass.western_grass_cm) if grass else "",
        "grass_notes": grass.condition_notes if grass and grass.condition_notes else "",
        "weekly_weather": json.dumps(weekly, ensure_ascii=False) if weekly else "",
    }


def _model_row(model, fields: list[str]) -> dict:
    dumped = model.model_dump(mode="json")
    return {field: _cell(dumped.get(field)) for field in fields}


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _pair(value: tuple[float, float] | None) -> str:
    if not value:
        return ""
    return f"{value[0]}-{value[1]}"


def _safe_dirname(name: str) -> str:
    cleaned = name.strip() or "unknown"
    cleaned = cleaned.replace("\\", "_").replace("/", "_")
    cleaned = re.sub(r"\s+", "_", cleaned)
    if cleaned in {".", ".."}:
        return "unknown"
    return cleaned


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> CsvExportFile:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _cell(row.get(field)) for field in fieldnames})
    return CsvExportFile(name=path.name, path=str(path), rows=len(rows))
