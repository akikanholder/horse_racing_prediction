"""Date utility functions."""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TOKYO = ZoneInfo("Asia/Tokyo")


def get_current_week_dates() -> tuple[date, date]:
    """Get the start and end dates of the current week (Saturday to Sunday).

    JRA races typically run on weekends (Saturday and Sunday).

    Returns:
        Tuple of (week_start, week_end) dates
    """
    today = date.today()
    weekday = today.weekday()

    if weekday == 5:
        week_start = today
        week_end = today + timedelta(days=1)
    elif weekday == 6:
        week_start = today - timedelta(days=1)
        week_end = today
    else:
        days_until_saturday = (5 - weekday) % 7
        if days_until_saturday == 0:
            days_until_saturday = 7
        week_start = today + timedelta(days=days_until_saturday)
        week_end = week_start + timedelta(days=1)

    return week_start, week_end


def get_this_week_race_dates() -> list[date]:
    """Get all race dates for this week (upcoming or current weekend).

    Returns:
        List of dates when races are held
    """
    week_start, week_end = get_current_week_dates()
    dates = []

    current = week_start
    while current <= week_end:
        dates.append(current)
        current += timedelta(days=1)

    return dates


def parse_japanese_date(date_str: str, year: int | None = None) -> date | None:
    """Parse Japanese date string.

    Supports formats:
    - "2024年1月1日"
    - "2024/1/1"
    - "1月1日" (requires year parameter)
    - "1/1" (requires year parameter)
    - "01/01" (requires year parameter)

    Args:
        date_str: Date string to parse
        year: Default year if not specified in string

    Returns:
        Parsed date or None if parsing failed
    """
    date_str = date_str.strip()

    patterns = [
        (r"(\d{4})年(\d{1,2})月(\d{1,2})日", lambda m: (int(m[1]), int(m[2]), int(m[3]))),
        (r"(\d{4})/(\d{1,2})/(\d{1,2})", lambda m: (int(m[1]), int(m[2]), int(m[3]))),
        (r"(\d{4})-(\d{1,2})-(\d{1,2})", lambda m: (int(m[1]), int(m[2]), int(m[3]))),
        (r"(\d{1,2})月(\d{1,2})日", lambda m: (year or datetime.now().year, int(m[1]), int(m[2]))),
        (r"(\d{1,2})/(\d{1,2})", lambda m: (year or datetime.now().year, int(m[1]), int(m[2]))),
    ]

    for pattern, extractor in patterns:
        match = re.search(pattern, date_str)
        if match:
            try:
                y, m, d = extractor(match)
                return date(y, m, d)
            except ValueError:
                continue

    return None


def format_race_date(d: date) -> str:
    """Format date for race display.

    Args:
        d: Date to format

    Returns:
        Formatted string like "2024年1月1日(月)"
    """
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    weekday = weekdays[d.weekday()]
    return f"{d.year}年{d.month}月{d.day}日({weekday})"


def parse_time_string(time_str: str) -> float | None:
    """Parse race time string to seconds.

    Supports formats:
    - "1:23.4" (minutes:seconds.tenths)
    - "1.23.4" (minutes.seconds.tenths)
    - "83.4" (just seconds.tenths)

    Args:
        time_str: Time string to parse

    Returns:
        Total seconds or None if parsing failed
    """
    time_str = time_str.strip()

    minutes_match = re.match(r"(\d+)[:\.](\d+)\.(\d+)", time_str)
    if minutes_match:
        minutes = int(minutes_match.group(1))
        seconds = int(minutes_match.group(2))
        tenths = int(minutes_match.group(3))
        return minutes * 60 + seconds + tenths / 10

    seconds_match = re.match(r"(\d+)\.(\d+)", time_str)
    if seconds_match:
        seconds = int(seconds_match.group(1))
        tenths = int(seconds_match.group(2))
        return seconds + tenths / 10

    return None


def get_race_id_date(race_id: str) -> date | None:
    """Extract date from race ID.

    Race ID format: YYYYMMDDJJRRNN
    - YYYY: Year
    - MM: Month
    - DD: Day
    - JJ: Venue code
    - RR: Round
    - NN: Race number

    Args:
        race_id: Race ID string

    Returns:
        Extracted date or None
    """
    if len(race_id) >= 8:
        try:
            year = int(race_id[:4])
            month = int(race_id[4:6])
            day = int(race_id[6:8])
            return date(year, month, day)
        except ValueError:
            pass
    return None


def tokyo_now() -> datetime:
    """Current time in Asia/Tokyo."""
    return datetime.now(TOKYO)


def format_tokyo_clock(hour: int, minute: int) -> str | None:
    """Clock as printed, with the Asia/Tokyo offset. Invalid clocks stay unset."""
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return f"{hour:02d}:{minute:02d}+09:00"


def normalize_tokyo_clock(value: str) -> str:
    """Turn ``15:30`` into ``15:30+09:00``. Other text is left as printed."""
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?:\+09:00)?", value.strip())
    if match is None:
        return value.strip()
    clock = format_tokyo_clock(int(match.group(1)), int(match.group(2)))
    return clock if clock is not None else value.strip()


def parse_post_time(text: str) -> str | None:
    """発走時刻 only. A measurement clock without 発走 is not a post time."""
    patterns = (
        r"(\d{1,2})\s*[:：]\s*(\d{2})\s*発走",
        r"発走時刻\s*[：:]\s*(\d{1,2})\s*時\s*(\d{1,2})\s*分",
        r"(\d{1,2})\s*時\s*(\d{1,2})\s*分\s*発走",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match is not None:
            return format_tokyo_clock(int(match.group(1)), int(match.group(2)))
    return None


def parse_measurement_time(text: str, year: int | None) -> datetime | None:
    """Measurement clock printed next to a reading. A missing year stays unset."""
    dated = re.search(
        r"(\d{4})\s*[/\-]\s*(\d{1,2})\s*[/\-]\s*(\d{1,2})\s+(\d{1,2})\s*[:：]\s*(\d{2})",
        text,
    )
    if dated is not None:
        return datetime(
            int(dated.group(1)),
            int(dated.group(2)),
            int(dated.group(3)),
            int(dated.group(4)),
            int(dated.group(5)),
            tzinfo=TOKYO,
        )
    partial = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日.*?(\d{1,2})\s*時\s*(\d{1,2})\s*分", text)
    if partial is None or year is None:
        return None
    return datetime(
        year,
        int(partial.group(1)),
        int(partial.group(2)),
        int(partial.group(3)),
        int(partial.group(4)),
        tzinfo=TOKYO,
    )
