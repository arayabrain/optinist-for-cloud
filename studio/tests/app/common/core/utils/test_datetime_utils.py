"""Tests for datetime_utils module.

Tests cover:
- UTC datetime functions
- User timezone handling (valid and invalid)
- Timestamp conversion
- Formatted output
"""

from datetime import datetime, timezone

from studio.app.common.core.utils.datetime_utils import (
    TIMEZONE_KEY,
    TIMEZONE_UTC,
    TZ_UTC,
    datetime_from_timestamp,
    ensure_utc,
    format_date_for_display,
    get_current_datetime,
    get_current_datetime_formatted,
    get_current_timestamp,
    get_datetime_for_timezone,
    get_datetime_for_timezone_formatted,
    is_datetime_aware,
    parse_datetime_for_timezone,
)


class TestUTCDatetimeFunctions:
    """Tests for UTC-based datetime functions."""

    def test_get_current_datetime_returns_utc(self):
        """get_current_datetime should return a UTC-aware datetime."""
        dt = get_current_datetime()
        assert dt.tzinfo == timezone.utc

    def test_get_current_datetime_is_recent(self):
        """get_current_datetime should return a time close to now."""
        before = datetime.now(timezone.utc)
        dt = get_current_datetime()
        after = datetime.now(timezone.utc)
        assert before <= dt <= after

    def test_get_current_timestamp_returns_float(self):
        """get_current_timestamp should return a float timestamp."""
        ts = get_current_timestamp()
        assert isinstance(ts, float)
        assert ts > 0

    def test_get_current_datetime_formatted_default_format(self):
        """get_current_datetime_formatted should use default format."""
        formatted = get_current_datetime_formatted()
        # Format: "2024/01/15 10:30:45"
        parts = formatted.split()
        assert len(parts) == 2
        assert "/" in parts[0]  # Date part
        assert ":" in parts[1]  # Time part

    def test_get_current_datetime_formatted_custom_format(self):
        """get_current_datetime_formatted should accept custom format."""
        formatted = get_current_datetime_formatted("%Y-%m-%d")
        # Format: "2024-01-15"
        assert "-" in formatted
        assert "/" not in formatted


class TestTimestampConversion:
    """Tests for timestamp conversion functions."""

    def test_datetime_from_timestamp_returns_utc(self):
        """datetime_from_timestamp should return a UTC-aware datetime."""
        # Unix timestamp for 2024-01-15 10:30:45 UTC
        timestamp = 1705314645.0
        dt = datetime_from_timestamp(timestamp)
        assert dt.tzinfo == timezone.utc

    def test_datetime_from_timestamp_correct_value(self):
        """datetime_from_timestamp should convert correctly."""
        timestamp = 1705314645.0
        dt = datetime_from_timestamp(timestamp)
        expected = datetime(2024, 1, 15, 10, 30, 45, tzinfo=timezone.utc)
        assert dt == expected

    def test_datetime_from_timestamp_roundtrip(self):
        """Converting datetime to timestamp and back should be consistent."""
        original = get_current_datetime()
        timestamp = original.timestamp()
        recovered = datetime_from_timestamp(timestamp)
        # Allow for microsecond rounding
        assert abs((recovered - original).total_seconds()) < 0.001


class TestUserTimezoneHandling:
    """Tests for user timezone functions (for scientific data)."""

    def test_get_datetime_for_timezone_none_returns_utc(self):
        """get_datetime_for_timezone(None) should return UTC datetime."""
        dt = get_datetime_for_timezone(None)
        assert dt.tzinfo == timezone.utc

    def test_get_datetime_for_timezone_empty_string_returns_utc(self):
        """get_datetime_for_timezone('') should return UTC datetime."""
        dt = get_datetime_for_timezone("")
        assert dt.tzinfo == timezone.utc

    def test_get_datetime_for_timezone_valid_timezone(self):
        """get_datetime_for_timezone should handle valid IANA timezones."""
        dt = get_datetime_for_timezone("America/New_York")
        assert dt.tzinfo is not None
        assert str(dt.tzinfo) == "America/New_York"

    def test_get_datetime_for_timezone_utc_string(self):
        """get_datetime_for_timezone('UTC') should work."""
        dt = get_datetime_for_timezone("UTC")
        assert dt.tzinfo is not None
        assert str(dt.tzinfo) == "UTC"

    def test_get_datetime_for_timezone_invalid_falls_back_to_utc(self):
        """get_datetime_for_timezone should fall back to UTC for invalid timezone."""
        dt = get_datetime_for_timezone("Invalid/Timezone")
        assert dt.tzinfo == timezone.utc

    def test_get_datetime_for_timezone_invalid_logs_warning(self, caplog):
        """get_datetime_for_timezone should log warning for invalid timezone."""
        get_datetime_for_timezone("NotA/RealTimezone")
        assert "Invalid timezone" in caplog.text
        assert "NotA/RealTimezone" in caplog.text
        assert TIMEZONE_UTC in caplog.text

    def test_get_datetime_for_timezone_formatted_none_returns_utc_time(self):
        """get_datetime_for_timezone_formatted(None) should format UTC time."""
        formatted = get_datetime_for_timezone_formatted(None)
        assert isinstance(formatted, str)
        assert "/" in formatted

    def test_get_datetime_for_timezone_formatted_valid_timezone(self):
        """get_datetime_for_timezone_formatted should format in user's timezone."""
        formatted = get_datetime_for_timezone_formatted(
            "America/New_York", "%Y-%m-%d %H:%M:%S"
        )
        assert isinstance(formatted, str)
        assert "-" in formatted

    def test_get_datetime_for_timezone_formatted_custom_format(self):
        """get_datetime_for_timezone_formatted should accept custom format."""
        formatted = get_datetime_for_timezone_formatted("UTC", "%Y-%m-%d")
        # Should only have date, no time
        assert " " not in formatted
        assert formatted.count("-") == 2


class TestDSTHandling:
    """Tests for Daylight Saving Time edge cases."""

    def test_timezone_handles_dst_timezone(self):
        """Timezones with DST should be handled correctly."""
        # America/New_York has DST
        dt = get_datetime_for_timezone("America/New_York")
        assert dt.tzinfo is not None
        # ZoneInfo handles DST automatically

    def test_timezone_handles_non_dst_timezone(self):
        """Timezones without DST should be handled correctly."""
        # Asia/Tokyo does not have DST
        dt = get_datetime_for_timezone("Asia/Tokyo")
        assert dt.tzinfo is not None
        assert str(dt.tzinfo) == "Asia/Tokyo"


class TestDisplayFormatting:
    """Tests for display formatting functions."""

    def test_format_date_for_display_default_format(self):
        """format_date_for_display should use default format with UTC indicator."""
        dt = datetime(2024, 1, 15, 10, 30, 45, tzinfo=timezone.utc)
        result = format_date_for_display(dt)
        assert result == "2024-01-15 (UTC)"

    def test_format_date_for_display_custom_format(self):
        """format_date_for_display should accept custom format."""
        dt = datetime(2024, 1, 15, 10, 30, 45, tzinfo=timezone.utc)
        result = format_date_for_display(dt, "%Y/%m/%d")
        assert result == "2024/01/15 (UTC)"


class TestConstants:
    """Tests for module constants."""

    def test_timezone_utc_constant(self):
        """TIMEZONE_UTC should be 'UTC'."""
        assert TIMEZONE_UTC == "UTC"

    def test_timezone_key_constant(self):
        """TIMEZONE_KEY should be 'timezone'."""
        assert TIMEZONE_KEY == "timezone"

    def test_tz_utc_constant(self):
        """TZ_UTC should be timezone.utc."""
        assert TZ_UTC == timezone.utc


class TestEnsureUtc:
    """Tests for ensure_utc function."""

    def test_ensure_utc_none_returns_none(self):
        """ensure_utc(None) should return None."""
        assert ensure_utc(None) is None

    def test_ensure_utc_naive_datetime_becomes_utc_aware(self):
        """ensure_utc should make naive datetime UTC-aware."""
        naive = datetime(2024, 1, 15, 10, 30, 45)
        result = ensure_utc(naive)
        assert result.tzinfo == timezone.utc
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15
        assert result.hour == 10

    def test_ensure_utc_utc_aware_returns_same(self):
        """ensure_utc should return UTC datetime unchanged."""
        utc_dt = datetime(2024, 1, 15, 10, 30, 45, tzinfo=timezone.utc)
        result = ensure_utc(utc_dt)
        assert result == utc_dt
        assert result.tzinfo == timezone.utc

    def test_ensure_utc_converts_other_timezone_to_utc(self):
        """ensure_utc should convert non-UTC timezone to UTC."""
        from zoneinfo import ZoneInfo

        # 10:30 in New York during winter (UTC-5)
        ny_tz = ZoneInfo("America/New_York")
        ny_dt = datetime(2024, 1, 15, 10, 30, 45, tzinfo=ny_tz)
        result = ensure_utc(ny_dt)
        assert result.tzinfo == timezone.utc
        # 10:30 EST = 15:30 UTC
        assert result.hour == 15

    def test_ensure_utc_preserves_microseconds(self):
        """ensure_utc should preserve microseconds."""
        naive = datetime(2024, 1, 15, 10, 30, 45, 123456)
        result = ensure_utc(naive)
        assert result.microsecond == 123456


class TestIsDatetimeAware:
    """Tests for is_datetime_aware function."""

    def test_is_datetime_aware_none_returns_false(self):
        """is_datetime_aware(None) should return False."""
        assert is_datetime_aware(None) is False

    def test_is_datetime_aware_naive_returns_false(self):
        """is_datetime_aware should return False for naive datetime."""
        naive = datetime(2024, 1, 15, 10, 30, 45)
        assert is_datetime_aware(naive) is False

    def test_is_datetime_aware_utc_returns_true(self):
        """is_datetime_aware should return True for UTC datetime."""
        utc_dt = datetime(2024, 1, 15, 10, 30, 45, tzinfo=timezone.utc)
        assert is_datetime_aware(utc_dt) is True

    def test_is_datetime_aware_other_timezone_returns_true(self):
        """is_datetime_aware should return True for any timezone-aware datetime."""
        from zoneinfo import ZoneInfo

        ny_dt = datetime(2024, 1, 15, 10, 30, 45, tzinfo=ZoneInfo("America/New_York"))
        assert is_datetime_aware(ny_dt) is True


class TestParseDatetimeForTimezone:
    """Tests for parse_datetime_for_timezone (inverse of the formatted writer)."""

    FORMAT = "%Y-%m-%d %H:%M:%S"

    def test_parse_is_aware_and_utc_by_default(self):
        """No timezone means UTC, matching get_datetime_for_timezone."""
        parsed = parse_datetime_for_timezone("2024-01-15 10:30:45", self.FORMAT)
        assert is_datetime_aware(parsed)
        assert parsed.utcoffset().total_seconds() == 0

    def test_parse_honours_the_recorded_timezone(self):
        """A Tokyo string resolves to Tokyo, not to the host's zone."""
        parsed = parse_datetime_for_timezone(
            "2024-01-15 10:30:45", self.FORMAT, "Asia/Tokyo"
        )
        assert parsed.utcoffset().total_seconds() == 9 * 3600

    def test_invalid_timezone_falls_back_to_utc(self):
        parsed = parse_datetime_for_timezone(
            "2024-01-15 10:30:45", self.FORMAT, "Not/AZone"
        )
        assert parsed.utcoffset().total_seconds() == 0

    def test_roundtrip_elapsed_is_not_skewed_by_the_host_zone(self):
        """The regression: a UTC-written stamp read back must not gain the host's
        offset, which expired the workflow startup grace on the first poll."""
        written = get_datetime_for_timezone_formatted(None, self.FORMAT)
        parsed = parse_datetime_for_timezone(written, self.FORMAT)
        elapsed = get_current_timestamp() - parsed.timestamp()
        assert abs(elapsed) < 60, f"elapsed skewed by {elapsed} sec"

    def test_roundtrip_elapsed_for_a_non_utc_writer(self):
        """Same guarantee when the stamp was written in the user's own zone."""
        written = get_datetime_for_timezone_formatted("Asia/Tokyo", self.FORMAT)
        parsed = parse_datetime_for_timezone(written, self.FORMAT, "Asia/Tokyo")
        elapsed = get_current_timestamp() - parsed.timestamp()
        assert abs(elapsed) < 60, f"elapsed skewed by {elapsed} sec"
