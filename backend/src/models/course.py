"""Course data models."""

from datetime import date, datetime

from pydantic import BaseModel, Field


class CourseCondition(BaseModel):
    """Track condition information."""

    venue: str = Field(..., description="競馬場")
    condition_date: date = Field(..., description="日付")
    turf_condition: str | None = Field(None, description="芝馬場状態")
    turf_moisture: float | None = Field(None, description="芝含水率")
    dirt_condition: str | None = Field(None, description="ダート馬場状態")
    dirt_moisture: float | None = Field(None, description="ダート含水率")
    weather: str | None = Field(None, description="天候")
    temperature: float | None = Field(None, description="気温")
    wind_direction: str | None = Field(None, description="風向")
    wind_speed: float | None = Field(None, description="風速")


class CourseLayout(BaseModel):
    """Course layout information."""

    distance: int = Field(..., description="距離")
    track_type: str = Field(..., description="コース種別 (芝/ダート)")
    direction: str = Field(..., description="回り (右/左)")
    inner_outer: str | None = Field(None, description="内/外回り")
    start_position: str | None = Field(None, description="スタート位置")
    straight_length: int | None = Field(None, description="直線距離 (m)")
    elevation_diff: float | None = Field(None, description="高低差 (m)")
    course_width: int | None = Field(None, description="コース幅 (m)")
    description: str | None = Field(None, description="コース説明")


class Course(BaseModel):
    """Racecourse information."""

    venue_code: str = Field(..., description="競馬場コード")
    venue_name: str = Field(..., description="競馬場名")
    location: str | None = Field(None, description="所在地")
    established: int | None = Field(None, description="設立年")
    capacity: int | None = Field(None, description="収容人数")
    turf_circumference: int | None = Field(None, description="芝コース周回距離 (m)")
    dirt_circumference: int | None = Field(None, description="ダートコース周回距離 (m)")
    turf_straight: int | None = Field(None, description="芝直線距離 (m)")
    dirt_straight: int | None = Field(None, description="ダート直線距離 (m)")
    layouts: list[CourseLayout] = Field(default_factory=list, description="コースレイアウト")
    conditions: list[CourseCondition] = Field(
        default_factory=list, description="最新馬場状態"
    )


class VenueInfo(BaseModel):
    """JRA venue constants."""

    VENUES: dict[str, str] = Field(
        default={
            "01": "札幌",
            "02": "函館",
            "03": "福島",
            "04": "新潟",
            "05": "東京",
            "06": "中山",
            "07": "中京",
            "08": "京都",
            "09": "阪神",
            "10": "小倉",
        },
        description="競馬場コードと名前のマッピング",
    )

    @classmethod
    def get_venue_name(cls, code: str) -> str | None:
        """Get venue name from code."""
        return cls.VENUES.default.get(code)

    @classmethod
    def get_venue_code(cls, name: str) -> str | None:
        """Get venue code from name."""
        for code, venue_name in cls.VENUES.default.items():
            if venue_name == name:
                return code
        return None


class WeeklyVenueSchedule(BaseModel):
    """Weekly schedule for a specific venue."""

    venue: str = Field(..., description="競馬場")
    venue_code: str | None = Field(None, description="競馬場コード")
    dates: list[date] = Field(default_factory=list, description="開催日")
    round_number: int | None = Field(None, description="開催回次")
    conditions: list[CourseCondition] = Field(
        default_factory=list, description="馬場状態"
    )
    fetched_at: datetime = Field(default_factory=datetime.now, description="取得日時")
