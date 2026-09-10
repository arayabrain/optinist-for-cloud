"""Where a premium subscription stops being premium.

The bug this pins: the status came from `(expiration - now).days`, and
`timedelta.days` truncates toward zero, so an expiration 23h59m away gave 0,
failed the `> 0` test and reported the user as being in the grace period.
`subscription_type` is premium only when this label is "Premium", and that
property drives /users/me/premium/status and /users/me/routing-info - so a
paying user lost premium routing for the final day of every billing period.
Dev bills Premium daily, which put accounts there inside the window almost
always.
"""

from datetime import timedelta

import pytest

from studio.app.common.core.subscription.constants import (
    PlanName,
    SubscriptionPeriods,
    SubscriptionPlanIds,
    SubscriptionStatus,
)
from studio.app.common.core.subscription.subscription_service import (
    derive_subscription_status,
)
from studio.app.common.core.utils.datetime_utils import get_current_datetime

GRACE = SubscriptionPeriods.GRACE_PERIOD_DAYS
NOW = get_current_datetime()


def status_at(offset: timedelta, plan_id=SubscriptionPlanIds.PREMIUM, name=None):
    """Derive the status for an expiration `offset` away from a frozen now."""
    return derive_subscription_status(NOW + offset, plan_id, name, NOW)


@pytest.mark.parametrize(
    "offset",
    [
        timedelta(seconds=1),
        timedelta(hours=1),
        # The regression: under 24 hours used to truncate to 0 days and report
        # the user as being in grace
        timedelta(hours=23, minutes=59),
        timedelta(days=1),
        timedelta(days=400),
    ],
    ids=["+1s", "+1h", "+23h59m", "+1d", "+400d"],
)
def test_premium_until_the_moment_it_expires(offset):
    status, days = status_at(offset)
    assert status == SubscriptionStatus.PREMIUM.value
    # A display value, rounded up: "expires later today" is 1 day, never 0
    assert days is not None and days >= 1


@pytest.mark.parametrize(
    "offset",
    [
        timedelta(seconds=-1),
        timedelta(hours=-1),
        timedelta(days=-1),
        timedelta(days=-GRACE) + timedelta(minutes=1),
    ],
    ids=["-1s", "-1h", "-1d", "grace-end-1m"],
)
def test_grace_from_the_moment_it_expires_until_the_grace_period_ends(offset):
    status, days = status_at(offset)
    assert status == SubscriptionStatus.LIMIT_GRACE.value
    # Days left in the grace window, not days since expiry
    assert days is not None and 0 <= days <= GRACE


@pytest.mark.parametrize(
    "offset",
    [
        timedelta(days=-GRACE) - timedelta(minutes=1),
        timedelta(days=-GRACE - 1),
        timedelta(days=-3650),
    ],
    ids=["past-grace", "grace+1d", "long-past"],
)
def test_expired_once_the_grace_period_has_passed(offset):
    status, days = status_at(offset)
    assert status == SubscriptionStatus.EXPIRED.value
    assert days is None


def test_the_boundary_is_the_expiry_instant_not_a_day_count():
    """A minute either side of expiry must land on either side of the line.

    Stated separately from the parametrised cases because it is the whole point:
    the old implementation put both of these in the same bucket.
    """
    before, _ = status_at(timedelta(minutes=-1))
    after, _ = status_at(timedelta(minutes=1))
    assert (after, before) == (
        SubscriptionStatus.PREMIUM.value,
        SubscriptionStatus.LIMIT_GRACE.value,
    )


def test_a_free_plan_is_free_whatever_its_expiration_says():
    status, days = status_at(timedelta(days=5), plan_id=SubscriptionPlanIds.FREE)
    assert (status, days) == (SubscriptionStatus.FREE.value, None)


def test_no_expiration_or_no_plan_reads_as_free():
    assert derive_subscription_status(None, SubscriptionPlanIds.PREMIUM, None, NOW) == (
        SubscriptionStatus.FREE.value,
        None,
    )
    assert derive_subscription_status(NOW + timedelta(days=5), None, None, NOW) == (
        SubscriptionStatus.FREE.value,
        None,
    )


def test_an_unrecognised_plan_falls_back_to_its_name():
    status, days = status_at(timedelta(days=5), plan_id=99, name="Enterprise")
    assert status == "Enterprise"
    assert days == 5
    # Past its expiration there is no sensible countdown to show
    assert status_at(timedelta(days=-5), plan_id=99, name="Enterprise") == (
        "Enterprise",
        None,
    )
    # And with no name at all it is reported as unknown rather than crashing
    assert status_at(timedelta(days=5), plan_id=99)[0] == PlanName.UNKNOWN.value
