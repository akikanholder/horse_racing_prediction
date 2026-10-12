"""Detailed track (baba) information models."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.utils.date_utils import normalize_tokyo_clock


class TrackCondition(str, Enum):
    """馬場状態 (Track condition)."""

    GOOD = "良"
    SLIGHTLY_HEAVY = "稍重"
    HEAVY = "重"
    BAD = "不良"
    UNKNOWN = "不明"

    @classmethod
    def from_string(cls, value: str) -> "TrackCondition":
        """Convert string to TrackCondition enum."""
        value = value.strip()
        for condition in cls:
            if condition.value == value:
                return condition
        if "稍" in value:
            return cls.SLIGHTLY_HEAVY
        return cls.UNKNOWN


class CourseType(str, Enum):
    """コースタイプ (Course type)."""

    TURF = "芝"
    DIRT = "ダート"


class Weather(str, Enum):
    """天候 (Weather condition)."""

    SUNNY = "晴"
    CLOUDY = "曇"
    RAINY = "雨"
    SNOWY = "雪"
    SUNNY_THEN_CLOUDY = "晴後曇"
    CLOUDY_THEN_SUNNY = "曇後晴"
    SUNNY_THEN_RAINY = "晴後雨"
    RAINY_THEN_SUNNY = "雨後晴"
    CLOUDY_THEN_RAINY = "曇後雨"
    RAINY_THEN_CLOUDY = "雨後曇"
    UNKNOWN = "不明"

    @classmethod
    def from_string(cls, value: str) -> "Weather":
        """Convert string to Weather enum."""
        value = value.strip()
        for weather in cls:
            if weather.value == value:
                return weather
        if "晴" in value:
            if "後雨" in value:
                return cls.SUNNY_THEN_RAINY
            if "後曇" in value:
                return cls.SUNNY_THEN_CLOUDY
            return cls.SUNNY
        if "雨" in value:
            if "後晴" in value:
                return cls.RAINY_THEN_SUNNY
            if "後曇" in value:
                return cls.RAINY_THEN_CLOUDY
            return cls.RAINY
        if "曇" in value:
            if "後雨" in value:
                return cls.CLOUDY_THEN_RAINY
            if "後晴" in value:
                return cls.CLOUDY_THEN_SUNNY
            return cls.CLOUDY
        if "雪" in value:
            return cls.SNOWY
        return cls.UNKNOWN


class MoistureContent(BaseModel):
    """含水率 (Moisture content).

    ``goal_front`` と ``fourth_corner`` は印字された数値のまま。
    馬場状態や目盛からは作らない。
    """

    goal_front: float | None = Field(None, description="ゴール前の含水率。印字が無ければ null")
    fourth_corner: float | None = Field(None, description="4コーナーの含水率。印字が無ければ null")
    measured_at: datetime | None = Field(None, description="測定時刻。印字が無ければ null")
    source: str | None = Field(
        None,
        description="jra_html / jra_js / netkeiba_html / netkeiba_js / headless。無いときは null",
    )
    fetched_at: datetime | None = Field(None, description="この含水率を得た時刻")


class CushionValue(BaseModel):
    """クッション値 (Cushion value).

    ``value`` は印字された数値。硬度目盛（12・10・8・7）や「硬め」は入れない。
    ``grade`` は同じ欄に印字された区分だけ。数値から区分は作らない。
    """

    value: float | None = Field(None, description="クッション値。印字された数値。無ければ null")
    grade: str | None = Field(
        None, description="印字された硬さ（硬め、やや硬め、標準など）。無ければ null"
    )
    measured_at: datetime | None = Field(None, description="測定時刻。印字が無ければ null")
    source: str | None = Field(
        None,
        description="jra_html / jra_js / netkeiba_html / netkeiba_js / headless。無いときは null",
    )
    fetched_at: datetime | None = Field(None, description="このクッション値を得た時刻")


class CourseInfo(BaseModel):
    """芝またはダートの馬場情報."""

    course_type: CourseType = Field(..., description="コースタイプ (芝/ダート)")
    condition: TrackCondition = Field(
        default=TrackCondition.UNKNOWN, description="馬場状態"
    )
    condition_source: str | None = Field(
        None, description="馬場状態を得た方法。印字が無ければ null"
    )
    condition_fetched_at: datetime | None = Field(None, description="馬場状態を得た時刻")
    moisture: MoistureContent | None = Field(None, description="含水率情報")
    cushion: CushionValue | None = Field(None, description="クッション値 (芝コースのみ)")


class WeeklyInfo(BaseModel):
    """週間天候情報."""

    date: datetime = Field(..., description="日付")
    weather: Weather = Field(default=Weather.UNKNOWN, description="天候")
    rainfall_mm: float | None = Field(None, description="降水量 (mm)")
    watering: bool = Field(default=False, description="散水の有無")


class GrassInfo(BaseModel):
    """芝情報."""

    native_grass_cm: tuple[float, float] | None = Field(
        None, description="野芝の芝丈 (min, max) cm"
    )
    western_grass_cm: tuple[float, float] | None = Field(
        None, description="洋芝の芝丈 (min, max) cm"
    )
    course_name: str | None = Field(None, description="使用コース (A/B/Cコースなど)")
    condition_notes: str | None = Field(None, description="芝の状態メモ")


class TrackInfo(BaseModel):
    """競馬場の馬場情報.

    取得できないときもこの形のまま返す。競馬場・日付・クッション値・含水率は
    null。``warning`` に理由を書く。403 本文や別場のページからは埋めない。
    """

    racecourse: str | None = Field(
        None, description="競馬場名。依頼した場のページでなければ null"
    )
    date: datetime | None = Field(None, description="開催日。ページに無ければ null")
    weather: Weather = Field(default=Weather.UNKNOWN, description="天候")
    weather_source: str | None = Field(None, description="天候を得た方法。印字が無ければ null")
    weather_fetched_at: datetime | None = Field(None, description="天候を得た時刻")
    turf: CourseInfo | None = Field(None, description="芝コース情報")
    dirt: CourseInfo | None = Field(None, description="ダートコース情報")
    grass_info: GrassInfo | None = Field(None, description="芝丈・コース情報")
    weekly_info: list[WeeklyInfo] = Field(
        default_factory=list, description="週間天候情報"
    )
    source: str = Field(..., description="データソース (JRA/Netkeiba)")
    warning: str | None = Field(
        None, description="取得できない理由。取れたときは null"
    )
    post_time: str | None = Field(
        None, description="発走時刻。印字どおり Asia/Tokyo（15:30+09:00）。無ければ null"
    )
    post_time_source: str | None = Field(None, description="発走時刻を得た方法。無ければ null")
    post_time_fetched_at: datetime | None = Field(None, description="発走時刻を得た時刻")
    scraped_at: datetime = Field(
        default_factory=datetime.now, description="スクレイピング時刻"
    )

    @field_validator("post_time", mode="before")
    @classmethod
    def _post_time_has_tokyo_offset(cls, value: object) -> object:
        """Store a printed clock with the Asia/Tokyo offset."""
        if isinstance(value, str):
            return normalize_tokyo_clock(value)
        return value

    model_config = ConfigDict(
        ser_json_timedelta="iso8601",
    )


def empty_track_info(source: str, warning: str) -> TrackInfo:
    """TrackInfo shape when the page cannot be used.

    Cushion and moisture stay null. The racecourse is not filled from a
    403 body, nav chrome, or the requested name alone.
    """
    return TrackInfo(
        racecourse=None,
        date=None,
        weather=Weather.UNKNOWN,
        turf=CourseInfo(course_type=CourseType.TURF),
        dirt=CourseInfo(course_type=CourseType.DIRT),
        source=source,
        warning=warning,
    )
