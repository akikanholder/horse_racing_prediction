"""Main entry point for the horse racing prediction backend."""

import logging
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from backend.models.course import Course, CourseCondition, WeeklyVenueSchedule
from backend.models.export import DailyRaceCsvExport
from backend.models.horse import Horse, HorseBasicInfo
from backend.models.odds import RaceOdds
from backend.models.race import Race, RaceCard, RaceSchedule, SettledRaceResult
from backend.models.track import TrackInfo
from backend.services.csv_export_service import CsvExportService, NoRacesFoundError
from backend.services.race_service import RaceService
from backend.services.track_info_service import TrackInfoService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

race_service: RaceService | None = None
track_info_service: TrackInfoService | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    global race_service, track_info_service
    race_service = RaceService()
    track_info_service = TrackInfoService()
    logger.info("Race service initialized")
    logger.info("Track info service initialized")
    yield
    if race_service:
        race_service.close()
    if track_info_service:
        track_info_service.close()
    logger.info("Services closed")


app = FastAPI(
    title="競馬予測 API",
    description="JRA競馬のレース情報、競走馬情報、コース情報を提供するAPI",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", tags=["General"])
async def root():
    """API root endpoint."""
    return {
        "message": "競馬予測 API",
        "version": "1.0.0",
        "endpoints": {
            "races": "/api/races",
            "race_detail": "/api/races/{race_id}",
            "race_card": "/api/races/{race_id}/card",
            "race_result": "/api/races/{race_id}/result",
            "race_odds": (
                "/api/races/{race_id}/odds?bet_types=win,wide,umaren,sanrenpuku&prefer=live"
            ),
            "horses": "/api/horses/{horse_id}",
            "courses": "/api/courses",
            "course_detail": "/api/courses/{venue}",
            "venues": "/api/venues",
            "tracks": "/api/tracks",
            "race_csv_export": "/api/races/export?date=YYYY-MM-DD",
        },
    }


@app.get("/api/races", response_model=RaceSchedule, tags=["Races"])
async def get_weekly_races():
    """今週のレース情報を取得する。

    JRAとnetkeibaから今週のレーススケジュールを取得します。

    Returns:
        RaceSchedule: 今週のレース一覧
    """
    if not race_service:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        schedule = race_service.get_weekly_races()
        return schedule
    except Exception as e:
        logger.error(f"Failed to get weekly races: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/races/{race_id}", response_model=Race, tags=["Races"])
async def get_race(race_id: str):
    """特定のレース情報を取得する。

    Args:
        race_id: レースID

    Returns:
        Race: レース情報
    """
    if not race_service:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        race_card = race_service.get_race_card(race_id)
        if not race_card:
            raise HTTPException(status_code=404, detail=f"Race {race_id} not found")
        return race_card.race
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get race {race_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/races/{race_id}/card", response_model=RaceCard, tags=["Races"])
async def get_race_card(race_id: str):
    """レースの出馬表を取得する。

    Args:
        race_id: レースID

    Returns:
        RaceCard: 出馬表（レース情報と出走馬リスト）
    """
    if not race_service:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        race_card = race_service.get_race_card(race_id)
        if not race_card:
            raise HTTPException(status_code=404, detail=f"Race card for {race_id} not found")
        return race_card
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get race card for {race_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/races/{race_id}/result", response_model=SettledRaceResult, tags=["Races"])
async def get_race_result(race_id: str):
    """確定した1レースの着順と払戻を取得する。

    ``cli horse`` や ``/api/horses/{horse_id}`` の近走成績ではない。
    レース前の期待値の入力に混ぜない。
    未確定、取得失敗、表が読めないときは着順と払戻は空で ``warning`` が入る。
    払戻の金額は結果ページにあるものだけ。無い馬券種は作らない。

    Args:
        race_id: レースID

    Returns:
        SettledRaceResult: このレースの確定結果
    """
    if not race_service:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        return race_service.get_settled_race_result(race_id)
    except Exception as e:
        logger.error(f"Failed to get race result for {race_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/races/{race_id}/odds", response_model=RaceOdds, tags=["Races"])
async def get_race_odds(
    race_id: str,
    bet_types: Annotated[
        str,
        Query(
            description=(
                "カンマ区切り。win（単勝）、wide（ワイド）、umaren（馬連）、"
                "sanrenpuku（三連複）。"
                "応答の encoding は utf-8、race_phase は pre（レース前）か post（レース後）。"
                "prefer=live が unavailable でも yoso へは自動で切り替えない"
            )
        ),
    ] = "win",
    prefer: Annotated[
        Literal["live", "yoso"],
        Query(
            description=(
                "希望する種別。kind は書き換えない。"
                "live が取れないときに予想が必要なら prefer=yoso を明示する"
            )
        ),
    ] = "live",
):
    """レースの単勝・ワイド・馬連・三連複オッズを取得する。

    期待値計算はここで返す OddsQuote を使う。RaceEntry.odds は使わない。
    発売後の市場オッズは source=netkeiba_live / kind=live。
    発売前のAI予想オッズが API に載っているときだけ source=netkeiba_yoso / kind=yoso。
    prefer は kind を書き換えない。prefer=live で市場オッズが無いとき、
    予想オッズへは自動で切り替えない。予想が必要なら prefer=yoso を明示する。
    数値が無いとき、HTTP 400、壊れた JSON のときは quotes が空で status=unavailable。
    失敗理由は reason（着順の warning に相当）。価格は作らない。
    encoding は常に utf-8。組み合わせは ASCII の馬番で、馬成績ページの EUC-JP では読まない。
    race_phase は pre（API status の yoso と middle。レース前）か
    post（API status の result。レース後の確定）。判別できない失敗は null。
    レース後のオッズが HTTP 400 や空でも quotes は空のまま。
    ワイドの組み合わせは ``3-7``（馬番の小さい順）で、価格は odds_min と odds_max。
    馬連の組み合わせも ``3-7``（馬番の小さい順）で、価格は単一の odds。
    馬連の変動値は範囲ではないので odds_min / odds_max は空のまま。
    三連複の組み合わせは ``1-2-5``（馬番3つの小さい順）で、価格は単一の odds。
    三連複の変動値も範囲ではないので odds_min / odds_max は空のまま。

    Args:
        race_id: レースID
        bet_types: 馬券種。win、wide、umaren、sanrenpuku。カンマ区切りで同時に取得できる
        prefer: live または yoso。kind は書き換えない

    Returns:
        RaceOdds: 単勝・ワイド・馬連・三連複のオッズ
    """
    if not race_service:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        return race_service.get_race_odds(race_id, bet_types=bet_types, prefer=prefer)
    except Exception as e:
        logger.error(f"Failed to get odds for {race_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/races/{race_id}/horses", response_model=list[HorseBasicInfo], tags=["Races"])
async def get_race_horses(race_id: str):
    """レースに出走する競走馬の情報を取得する。

    ``recent_performances`` は各馬の成績ページにある過去走で、このレースの確定結果ではない。

    Args:
        race_id: レースID

    Returns:
        list[HorseBasicInfo]: 出走馬の基本情報リスト
    """
    if not race_service:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        horses = race_service.get_horses_for_race(race_id)
        return horses
    except Exception as e:
        logger.error(f"Failed to get horses for race {race_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/races/export", response_model=DailyRaceCsvExport, tags=["Races"])
async def export_races_csv(
    race_date: Annotated[
        date, Query(alias="date", description="対象日 (YYYY-MM-DD)")
    ],
    include_horse_details: Annotated[
        bool, Query(description="出走馬の血統など詳細情報も取得する")
    ] = False,
):
    """指定日の全レース情報を日付・競馬場ごとのCSVに出力する。

    出力先は `backend/csv_output/{日付}/{競馬場}/` です。
    レース、出馬表、レース結果、競走馬、コース、馬場をそれぞれ別のCSVに書き出します。

    Args:
        race_date: 対象日
        include_horse_details: True のとき競走馬の詳細ページも取得する

    Returns:
        DailyRaceCsvExport: 出力したディレクトリとファイル
    """
    if not race_service:
        raise HTTPException(status_code=503, detail="Service not initialized")

    exporter = CsvExportService(
        race_service,
        track_info_service,
        include_horse_details=include_horse_details,
    )
    try:
        return exporter.export_date(race_date)
    except NoRacesFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Failed to export races for {race_date}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/horses/{horse_id}", response_model=Horse, tags=["Horses"])
async def get_horse(horse_id: str):
    """競走馬の近走履歴を取得する。

    ``performances`` は ``/horse/result/{horse_id}/`` の過去走であり、
    今走の確定結果ではない。着順の精算には使わず、近走の能力評価にだけ使う。
    成績を取れないときは ``performances`` は空で、``warning`` に理由が入る。

    Args:
        horse_id: 馬ID

    Returns:
        Horse: 馬名、通算成績、過去走。今走のレース結果は含まない
    """
    if not race_service:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        horse = race_service.get_horse_info(horse_id)
        if not horse:
            raise HTTPException(status_code=404, detail=f"Horse {horse_id} not found")
        return horse
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get horse {horse_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/courses", response_model=list[Course], tags=["Courses"])
async def get_all_courses():
    """全競馬場のコース情報を取得する。

    Returns:
        list[Course]: 全競馬場のコース情報
    """
    if not race_service:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        courses = race_service.get_all_course_info()
        return courses
    except Exception as e:
        logger.error(f"Failed to get courses: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/courses/{venue}", response_model=Course, tags=["Courses"])
async def get_course(venue: str):
    """特定競馬場のコース情報を取得する。

    Args:
        venue: 競馬場名 (東京/中山/阪神/京都/中京/小倉/新潟/福島/札幌/函館)

    Returns:
        Course: コース情報
    """
    if not race_service:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        course = race_service.get_course_info(venue)
        if not course:
            raise HTTPException(status_code=404, detail=f"Course {venue} not found")
        return course
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get course {venue}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/courses/{venue}/conditions", response_model=CourseCondition, tags=["Courses"])
async def get_course_conditions(venue: str):
    """競馬場の現在の馬場状態を取得する。

    Args:
        venue: 競馬場名

    Returns:
        CourseCondition: 馬場状態
    """
    if not race_service:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        conditions = race_service.get_course_conditions(venue)
        if not conditions:
            raise HTTPException(status_code=404, detail=f"Conditions for {venue} not found")
        return conditions
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get conditions for {venue}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/tracks", response_model=dict[str, TrackInfo], tags=["Tracks"])
async def get_track_info(
    venue: str | None = None,
    date: str | None = None,
    merge: bool = False,
):
    """馬場情報（馬場状態、含水率、クッション値）を取得する。

    JRAの馬場情報ページとnetkeibaの馬場情報ページを参照する。

    Args:
        venue: 競馬場名（例: 中山）。省略時は掲載中の競馬場
        date: netkeiba用の日付 (YYYYMMDD)
        merge: Trueのとき両ソースを統合したデータを merged キーで返す
    """
    if not track_info_service:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        results = track_info_service.get_track_info(
            racecourse=venue, date=date, merge=merge
        )
        if not results:
            raise HTTPException(status_code=404, detail="Track info not found")
        return results
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get track info: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/venues", response_model=list[WeeklyVenueSchedule], tags=["Venues"])
async def get_venue_schedules():
    """今週の開催競馬場スケジュールを取得する。

    Returns:
        list[WeeklyVenueSchedule]: 開催競馬場のスケジュール
    """
    if not race_service:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        schedules = race_service.get_venue_schedules()
        return schedules
    except Exception as e:
        logger.error(f"Failed to get venue schedules: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
