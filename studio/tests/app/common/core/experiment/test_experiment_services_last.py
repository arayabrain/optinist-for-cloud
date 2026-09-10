"""Tests for ExperimentService.get_last_experiment ordering."""

from unittest.mock import patch

from studio.app.common.core.experiment.experiment_reader import ExptConfigReader
from studio.app.common.core.experiment.experiment_services import ExperimentService


def _config(unique_id: str, started_at: str, timezone: str):
    config = ExptConfigReader.create_empty_experiment_config()
    config.unique_id = unique_id
    config.started_at = started_at
    config.timezone = timezone
    return config


def test_get_last_experiment_orders_by_absolute_time_not_wall_clock():
    """Tokyo 10:00 is 01:00 UTC, so the UTC 09:00 run is genuinely later.
    A naive wall-clock comparison picks the Tokyo one."""
    tokyo = _config("tokyo_run", "2024-01-15 10:00:00", "Asia/Tokyo")
    utc = _config("utc_run", "2024-01-15 09:00:00", "UTC")

    with patch(
        "studio.app.common.core.experiment.experiment_services.glob",
        return_value=["/out/ws1/tokyo_run/experiment.yaml", "/out/ws1/utc_run/e.yaml"],
    ), patch.object(ExptConfigReader, "read_from_path", side_effect=[tokyo, utc]):
        last = ExperimentService.get_last_experiment("ws1")

    assert last.unique_id == "utc_run"


def test_get_last_experiment_missing_timezone_falls_back_to_utc():
    """Legacy configs with no timezone still order against each other."""
    older = _config("older", "2024-01-15 09:00:00", None)
    newer = _config("newer", "2024-01-15 11:00:00", None)

    with patch(
        "studio.app.common.core.experiment.experiment_services.glob",
        return_value=["/out/ws1/older/experiment.yaml", "/out/ws1/newer/e.yaml"],
    ), patch.object(ExptConfigReader, "read_from_path", side_effect=[older, newer]):
        last = ExperimentService.get_last_experiment("ws1")

    assert last.unique_id == "newer"
