"""
Centralized datetime utilities for consistent timezone handling.

All datetime operations in the application should use these utilities
to ensure consistent UTC timezone handling and avoid naive datetime issues.

Note: Lambda packages (infrastructure/terraform/*_package/) cannot import from
this module as they are deployed as isolated ZIP files. If datetime handling
logic changes here, the following Lambda packages should also be updated:
  - free_manager_package/free_manager.py
  - free_manager_package/free_user_utils.py
  - free_cleanup_package/free_cleanup.py
  - premium_manager_package/premium_manager.py
  - cost_tracker_package/cost_tracker.py
  - common_user_manager_package/common_user_manager.py
"""

import logging
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

# =============================================================================
# Timezone Constants
# =============================================================================

TIMEZONE_UTC = "UTC"
"""Default fallback timezone string. Used when no timezone is specified or invalid."""

TIMEZONE_KEY = "timezone"
"""Config/param key for user timezone. Used in nwbParam, ExptConfig, etc."""

# Timezone objects for datetime.now() calls - centralized here for easy maintenance
TZ_UTC = timezone.utc
"""UTC timezone object for datetime.now() calls in system operations."""


def get_current_datetime() -> datetime:
    """Get the current UTC datetime."""
    return datetime.now(TZ_UTC)


def get_current_timestamp() -> float:
    """Get the current UTC timestamp as a float (seconds since epoch)."""
    return get_current_datetime().timestamp()


def get_current_datetime_formatted(format_string: str = "%Y/%m/%d %H:%M:%S") -> str:
    """Get the current UTC datetime as a formatted string."""
    return get_current_datetime().strftime(format_string)


def datetime_from_timestamp(timestamp: float) -> datetime:
    """Convert a Unix timestamp to a UTC-aware datetime."""
    return datetime.fromtimestamp(timestamp, tz=TZ_UTC)


def get_datetime_for_timezone(timezone_str: str = None) -> datetime:
    """
    Get the current datetime in the user's timezone.

    Used for scientific data (NWB files, experiment logs) where the user's local
    timezone matters for correlating with lab work.

    Args:
        timezone_str: IANA timezone string from browser
            (via Intl.DateTimeFormat().resolvedOptions().timeZone).
            Falls back to UTC if None or invalid.
    """
    if not timezone_str:
        return get_current_datetime()

    try:
        tz = ZoneInfo(timezone_str)
        return datetime.now(tz)
    except (ZoneInfoNotFoundError, KeyError) as e:
        logger.warning(
            f"Invalid timezone '{timezone_str}', falling back to {TIMEZONE_UTC}: {e}"
        )
        return get_current_datetime()


def get_datetime_for_timezone_formatted(
    timezone_str: str = None, format_string: str = "%Y/%m/%d %H:%M:%S"
) -> str:
    """
    Get the current datetime in the user's timezone as a formatted string.

    Used for experiment started_at/finished_at timestamps.

    Args:
        timezone_str: IANA timezone string from browser. Falls back to UTC if None.
        format_string: strftime format string (default: "%Y/%m/%d %H:%M:%S")
    """
    dt = get_datetime_for_timezone(timezone_str)
    return dt.strftime(format_string)


def parse_datetime_for_timezone(
    value: str, format_string: str, timezone_str: str = None
) -> datetime:
    """
    Inverse of get_datetime_for_timezone_formatted.

    The stored string carries no offset, so reading it back naively resolves it
    against the container's TZ instead of the zone it was written in - which
    skews the result by that offset wherever the two differ.

    Args:
        value: the formatted timestamp to parse
        format_string: the strftime format it was written with
        timezone_str: the IANA timezone it was written in. Falls back to UTC,
            matching get_datetime_for_timezone.
    """
    tz = TZ_UTC
    if timezone_str:
        try:
            tz = ZoneInfo(timezone_str)
        except (ZoneInfoNotFoundError, KeyError) as e:
            logger.warning(
                f"Invalid timezone '{timezone_str}', "
                f"falling back to {TIMEZONE_UTC}: {e}"
            )
    return datetime.strptime(value, format_string).replace(tzinfo=tz)


def format_date_for_display(dt: datetime, format_string: str = "%Y-%m-%d") -> str:
    """
    Format a datetime for display, appending "(UTC)" indicator.

    Used for subscription dates and other user-facing UTC timestamps.
    """
    return f"{dt.strftime(format_string)} ({TIMEZONE_UTC})"


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """
    Ensure a datetime is UTC-aware.

    Naive datetimes are assumed to already be in UTC and are made timezone-aware.
    Aware datetimes are converted to UTC.

    Args:
        dt: A datetime object (naive or aware) or None.

    Returns:
        A UTC-aware datetime or None if input was None.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ_UTC)
    return dt.astimezone(TZ_UTC)


def is_datetime_aware(dt: Optional[datetime]) -> bool:
    """Check if a datetime is timezone-aware."""
    return dt is not None and dt.tzinfo is not None
