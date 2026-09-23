"""Date utility functions."""

import re
from datetime import date, datetime, timedelta


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
