"""Tests for daily race CSV export."""

import csv
from datetime import date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.models.course import Course, CourseCondition
from backend.models.race import Race, RaceCard, RaceEntry, RaceResult
from backend.models.track import (
    CourseInfo,
    CourseType,
    CushionValue,
    MoistureContent,
    TrackCondition,
    TrackInfo,
    Weather,
)
from backend.scrapers.netkeiba_scraper import NetkeibaScraper
from backend.services.csv_export_service import (
    CsvExportService,
    NoRacesFoundError,
)
from backend.services.race_service import RaceService

TARGET = date(2026, 9, 27)


def _race(race_id: str, venue: str, number: int, name: str) -> Race:
    return Race(
        race_id=race_id,
        race_name=name,
        race_number=number,
        race_date=TARGET,
        venue=venue,
        venue_code="05" if venue == "東京" else "09",
        distance=1600,
        track_type="芝",
        weather="晴",
    )


def _card(race: Race) -> RaceCard:
    return RaceCard(
        race=race,
        entries=[
            RaceEntry(
                bracket_number=1,
                horse_number=1,
                horse_name="テストホース",
                horse_id="h1",
                sex="牡",
                age=4,
                jockey_name="騎手A",
                trainer_name="調教師A",
                odds=2.5,
                popularity=1,
            )
        ],
    )


def _results() -> list[RaceResult]:
    return [
        RaceResult(
            rank=1,
            bracket_number=1,
            horse_number=1,
            horse_name="テストホース",
            horse_id="h1",
            finish_time="1:33.4",
            jockey_name="騎手A",
            odds=2.5,
            popularity=1,
        )
    ]


class FakeRaceService:
    """In-memory race source for export tests."""

    def __init__(self, races: list[Race]):
        self.races = races
        self.cards = {race.race_id: _card(race) for race in races}

    def get_races_for_date(self, target_date: date) -> list[Race]:
        return [race for race in self.races if race.race_date == target_date]

    def get_race_export_bundle(self, race_id: str):
        card = self.cards.get(race_id)
        return card, _results() if card else []

    def close(self) -> None:
        return None

    def get_course_info(self, venue: str) -> Course:
        return Course(
            venue_code="05" if venue == "東京" else "09",
            venue_name=venue,
            turf_circumference=2083,
            turf_straight=525,
            conditions=[
                CourseCondition(
                    venue=venue,
                    condition_date=TARGET,
                    turf_condition="良",
                    weather="晴",
                )
            ],
        )


class FakeTrackService:
    """In-memory track source for export tests."""

    def get_track_info(self, racecourse: str, date: str, merge: bool = False):
        info = TrackInfo(
            racecourse=racecourse,
            date=datetime(2026, 9, 27, 10, 0),
            weather=Weather.SUNNY,
            turf=CourseInfo(
                course_type=CourseType.TURF,
                condition=TrackCondition.GOOD,
                moisture=MoistureContent(goal_front=10.2, fourth_corner=11.0),
                cushion=CushionValue(value=9.5),
            ),
            source="JRA",
        )
        return {"jra": info}

    def close(self) -> None:
        return None


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_export_splits_date_and_venue_directories(tmp_path: Path):
    tokyo = _race("202609270501", "東京", 1, "テストステークス")
    hanshin = _race("202609270901", "阪神", 1, "阪神記念")
    service = CsvExportService(
        FakeRaceService([tokyo, hanshin]),
        FakeTrackService(),
        output_root=tmp_path,
    )

    result = service.export_date(TARGET)

    assert result.race_count == 2
    assert result.venue_count == 2
    assert (tmp_path / "2026-09-27" / "東京" / "races.csv").is_file()
    assert (tmp_path / "2026-09-27" / "阪神" / "races.csv").is_file()

    tokyo_races = _read_csv(tmp_path / "2026-09-27" / "東京" / "races.csv")
    hanshin_races = _read_csv(tmp_path / "2026-09-27" / "阪神" / "races.csv")
    assert [row["race_name"] for row in tokyo_races] == ["テストステークス"]
    assert [row["venue"] for row in hanshin_races] == ["阪神"]

    entries = _read_csv(tmp_path / "2026-09-27" / "東京" / "entries.csv")
    assert entries[0]["horse_name"] == "テストホース"
    assert entries[0]["race_id"] == tokyo.race_id

    results = _read_csv(tmp_path / "2026-09-27" / "東京" / "results.csv")
    assert results[0]["rank"] == "1"
    assert results[0]["finish_time"] == "1:33.4"

    horses = _read_csv(tmp_path / "2026-09-27" / "東京" / "horses.csv")
    assert horses[0]["horse_id"] == "h1"
    assert horses[0]["race_ids"] == tokyo.race_id

    courses = _read_csv(tmp_path / "2026-09-27" / "東京" / "courses.csv")
    assert courses[0]["venue_name"] == "東京"
    assert courses[0]["turf_condition"] == "良"
    assert courses[0]["turf_circumference"] == "2083"

    tracks = _read_csv(tmp_path / "2026-09-27" / "東京" / "tracks.csv")
    assert tracks[0]["racecourse"] == "東京"
    assert tracks[0]["turf_condition"] == "良"
    assert tracks[0]["turf_cushion"] == "9.5"
    assert tracks[0]["turf_cushion_hardness"] == ""
    assert tracks[0]["weather"] == "晴"


def test_export_writes_one_csv_per_information_type(tmp_path: Path):
    service = CsvExportService(
        FakeRaceService([_race("202609270501", "東京", 1, "毎日杯")]),
        FakeTrackService(),
        output_root=tmp_path,
    )
    result = service.export_date(TARGET)
    names = {item.name for item in result.venues[0].files}
    assert names == {
        "races.csv",
        "entries.csv",
        "results.csv",
        "horses.csv",
        "courses.csv",
        "tracks.csv",
    }


def test_export_raises_when_date_has_no_races(tmp_path: Path):
    service = CsvExportService(FakeRaceService([]), FakeTrackService(), output_root=tmp_path)
    with pytest.raises(NoRacesFoundError):
        service.export_date(TARGET)
    assert not (tmp_path / "2026-09-27").exists()


def test_get_races_for_date_filters_and_merges():
    weekly_only = _race("202609270502", "東京", 2, "週次のみ")
    shared = _race("202609270501", "東京", 1, "短")
    detailed = shared.model_copy(update={"race_name": "詳細なレース名", "post_time": "15:40"})
    other_day = shared.model_copy(
        update={"race_id": "202609260501", "race_date": date(2026, 9, 26), "race_name": "前日"}
    )

    class Jra:
        def get_weekly_race_schedule(self):
            from backend.models.race import RaceSchedule

            return RaceSchedule(
                week_start=date(2026, 9, 26),
                week_end=date(2026, 9, 27),
                races=[weekly_only, shared, other_day],
            )

    class Netkeiba:
        def get_weekly_races(self):
            from backend.models.race import RaceSchedule

            return RaceSchedule(
                week_start=date(2026, 9, 26),
                week_end=date(2026, 9, 27),
                races=[],
            )

        def get_races_by_date(self, target_date: date):
            assert target_date == TARGET
            return [detailed]

    service = RaceService()
    service._jra_scraper = Jra()
    service._netkeiba_scraper = Netkeiba()

    races = service.get_races_for_date(TARGET)
    by_id = {race.race_id: race for race in races}
    assert set(by_id) == {"202609270501", "202609270502"}
    assert by_id["202609270501"].race_name == "詳細なレース名"
    assert by_id["202609270501"].post_time == "15:40+09:00"


def test_netkeiba_races_by_date_uses_requested_date():
    html = """
    <div class="RaceList_DataList">
      <a href="/race/shutuba.html?race_id=202605030811">11R 芝1600m</a>
      <a href="/race/result.html?race_id=202609020611">阪神 6R ダート1800m</a>
    </div>
    """

    class _HTTP:
        def __init__(self):
            self.urls: list[str] = []

        def get(self, url, params=None, headers=None, encoding=None):
            self.urls.append(url)
            assert params == {"kaisai_date": "20260927"}
            assert encoding == "utf-8"
            return html

    http = _HTTP()
    scraper = NetkeibaScraper(http_client=http, headless_render="off")  # type: ignore[arg-type]

    races = scraper.get_races_by_date(TARGET)
    assert http.urls == ["https://race.netkeiba.com/top/race_list.html"]
    by_id = {race.race_id: race for race in races}
    assert set(by_id) == {"202605030811", "202609020611"}
    assert all(race.race_date == TARGET for race in races)
    assert by_id["202609020611"].venue == "阪神"
    assert by_id["202605030811"].venue == "東京"
    assert by_id["202605030811"].race_number == 11
    assert by_id["202605030811"].distance == 1600


def test_export_api_writes_csv_and_returns_summary(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "backend.services.csv_export_service.DEFAULT_OUTPUT_ROOT",
        tmp_path,
    )

    with TestClient(app) as client:
        import backend.main as main_module

        main_module.race_service = FakeRaceService([_race("202609270501", "東京", 1, "API杯")])
        main_module.track_info_service = FakeTrackService()
        response = client.post("/api/races/export", params={"date": "2026-09-27"})

    assert response.status_code == 200
    body = response.json()
    assert body["race_count"] == 1
    assert body["venue_count"] == 1
    assert body["venues"][0]["venue"] == "東京"
    assert (tmp_path / "2026-09-27" / "東京" / "races.csv").is_file()
    races = _read_csv(tmp_path / "2026-09-27" / "東京" / "races.csv")
    assert races[0]["race_name"] == "API杯"


def test_export_api_returns_404_when_empty(monkeypatch):
    with TestClient(app) as client:
        import backend.main as main_module

        main_module.race_service = FakeRaceService([])
        main_module.track_info_service = FakeTrackService()
        response = client.post("/api/races/export", params={"date": "2026-09-27"})

    assert response.status_code == 404


def test_gitignore_covers_csv_and_output_directory():
    gitignore = Path(__file__).resolve().parents[2] / ".gitignore"
    text = gitignore.read_text(encoding="utf-8")
    assert "*.csv" in text
    assert "backend/csv_output/" in text
