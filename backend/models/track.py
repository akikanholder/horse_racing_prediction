"""Detailed track (baba) information models."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


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
    """含水率 (Moisture content)."""

    goal_front: float | None = Field(None, description="ゴール前の含水率")
    fourth_corner: float | None = Field(None, description="4コーナーの含水率")
    measured_at: datetime | None = Field(None, description="測定時刻")


class CushionValue(BaseModel):
    """クッション値 (Cushion value)."""

    value: float | None = Field(None, description="クッション値")
    measured_at: datetime | None = Field(None, description="測定時刻")

    @property
    def hardness_level(self) -> str:
        """クッション値の硬さレベルを判定."""
        if self.value is None:
            return "不明"
        if self.value >= 12:
            return "硬め"
        if self.value >= 10:
            return "やや硬め"
        if self.value >= 8:
            return "標準"
        if self.value >= 7:
            return "やや軟らか"
        return "軟らか"


class CourseInfo(BaseModel):
    """芝またはダートの馬場情報."""

    course_type: CourseType = Field(..., description="コースタイプ (芝/ダート)")
    condition: TrackCondition = Field(
        default=TrackCondition.UNKNOWN, description="馬場状態"
    )
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
    """競馬場の馬場情報."""

    racecourse: str = Field(..., description="競馬場名")
    date: datetime = Field(..., description="開催日")
    weather: Weather = Field(default=Weather.UNKNOWN, description="天候")
    turf: CourseInfo | None = Field(None, description="芝コース情報")
    dirt: CourseInfo | None = Field(None, description="ダートコース情報")
    grass_info: GrassInfo | None = Field(None, description="芝丈・コース情報")
    weekly_info: list[WeeklyInfo] = Field(
        default_factory=list, description="週間天候情報"
    )
    source: str = Field(..., description="データソース (JRA/Netkeiba)")
    scraped_at: datetime = Field(
        default_factory=datetime.now, description="スクレイピング時刻"
    )

    model_config = ConfigDict(
        ser_json_timedelta="iso8601",
    )
