"""Shared EC2 instance-id resolution.

Both FreeUserActivityMiddleware (writer of ``FreeUserAssignment.instance_id``)
and ``DataCleanupJob`` (which filters cleanup by ``instance_id``) must resolve
the same value; otherwise the per-instance cleanup filter silently breaks.
This module is the single resolver shared by both.
"""

import os

from studio.app.common.core.logger import AppLogger

logger = AppLogger.get_logger()


def resolve_instance_id() -> str:
    """Resolve the current EC2 instance id.

    Resolution order:
    - env ``INSTANCE_ID`` (set by cloud-startup.sh)
    - EC2 IMDSv2 metadata (token-based)
    - EC2 IMDSv1 metadata (fallback)
    - ``"local"`` (local dev / metadata unavailable)

    Not cached: callers that need caching (per-request middleware) wrap this.
    """
    instance_id = os.environ.get("INSTANCE_ID")
    if instance_id:
        return instance_id

    # IMDSv2 (token required)
    try:
        import urllib.request

        token_req = urllib.request.Request(
            "http://169.254.169.254/latest/api/token",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
            method="PUT",
        )
        with urllib.request.urlopen(token_req, timeout=1) as response:
            token = response.read().decode("utf-8")

        metadata_req = urllib.request.Request(
            "http://169.254.169.254/latest/meta-data/instance-id",
            headers={"X-aws-ec2-metadata-token": token},
        )
        with urllib.request.urlopen(metadata_req, timeout=1) as response:
            return response.read().decode("utf-8")
    except Exception:
        pass

    # IMDSv1 (no token)
    try:
        import urllib.request

        with urllib.request.urlopen(
            "http://169.254.169.254/latest/meta-data/instance-id", timeout=1
        ) as response:
            return response.read().decode("utf-8")
    except Exception:
        pass

    logger.warning(
        "Could not retrieve EC2 instance ID from metadata service. "
        "Using 'local' as instance ID. This is OK for local development "
        "but indicates a problem in production."
    )
    return "local"
