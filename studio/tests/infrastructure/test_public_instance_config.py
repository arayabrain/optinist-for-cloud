"""Configuration assertions for the public tier, read from the terraform source.

These are *configuration* assertions, not behavioral ones: terraform declaring
``AFTER_7_DAYS`` is not proof AWS applied it, so the deployed check stays
manual. What they do catch is a PR that changes the declaration - which is where
our own regressions live.

The ALB priority band check is the exception and is a real invariant test: the
premium band is allocated at runtime by the premium-manager Lambda while the
public and free bands are static terraform, so nothing but this test connects
the two halves.
"""

import re
from pathlib import Path

import pytest
from premium_manager import MAX_PREMIUM_PRIORITY, get_next_available_priority

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
TERRAFORM_DIR = PROJECT_ROOT / "infrastructure" / "terraform"

PUBLIC_ALB_RULES_TF = TERRAFORM_DIR / "public_alb_rules.tf"
PUBLIC_SERVICE_TF = TERRAFORM_DIR / "public_service.tf"
PUBLIC_CLEANUP_TF = TERRAFORM_DIR / "public_cleanup.tf"
INFRASTRUCTURE_TF = TERRAFORM_DIR / "infrastructure.tf"
MAIN_TF = TERRAFORM_DIR / "main.tf"
COMPUTE_TF = TERRAFORM_DIR / "compute.tf"
MONITORING_TF = TERRAFORM_DIR / "monitoring.tf"

PREMIUM_BAND_START = 100

PUBLISHED_DATA_VOLUME = "${local.env_prefix}-public-published-data-volume"

EXPECTED_ALB_ROUTING = {
    "sync_experiment_to_public": (
        200,
        ("/system-internal/sync-experiment/*",),
        "public",
    ),
    "sync_experiments_to_free": (
        210,
        ("/system-internal/sync-experiments/*",),
        "autoscaling",
    ),
    "visualizations_public_header": (280, ("/api/visualizations/*",), "public"),
    "public_dataview_api": (
        300,
        ("/api/public/dataview", "/api/public/dataview/*"),
        "public",
    ),
    "auth_to_public": (305, ("/auth/*",), "public"),
    "users_me_to_public": (306, ("/users/me", "/users/me/*"), "public"),
    "log_report_to_public": (307, ("/log-report/*",), "public"),
    "static_assets_to_public": (
        310,
        ("/static/*", "/images/*", "/favicon.ico", "/manifest.json", "/robots.txt"),
        "public",
    ),
    "docs_to_public": (
        311,
        ("/docs", "/docs/*", "/openapi", "/redoc", "/health"),
        "public",
    ),
    "asset_manifest_to_public": (312, ("/asset-manifest.json",), "public"),
    "visualizations_authenticated_to_free": (
        315,
        ("/api/visualizations/*",),
        "autoscaling",
    ),
    "anonymous_flows_to_free": (
        316,
        (
            "/api/register",
            "/api/register/*",
            "/api/subsc/webhooks",
            "/api/subsc/webhooks/*",
        ),
        "autoscaling",
    ),
    "authenticated_to_free": (320, None, "autoscaling"),
}


def _read(path: Path) -> str:
    assert path.exists(), f"terraform source not found at {path}"
    return path.read_text()


def _resource_block(tf: str, resource_type: str, name: str) -> str:
    """Return the source of one ``resource "<type>" "<name>"`` block.

    Terminates at the next top-level ``resource`` / ``data`` / ``output``
    declaration, matching the technique in ``test_compute_config.py``.
    """
    match = re.search(
        r'resource\s+"%s"\s+"%s"\s*\{.*?(?=^(?:resource|data|output|locals)\s|\Z)'
        % (re.escape(resource_type), re.escape(name)),
        tf,
        re.DOTALL | re.MULTILINE,
    )
    assert match, f'resource "{resource_type}" "{name}" not found'
    return match.group()


def _variable_default(tf: str, name: str) -> str:
    """Return the ``default`` of one ``variable "<name>"`` block."""
    block = re.search(
        r'variable\s+"%s"\s*\{.*?^\}' % re.escape(name), tf, re.DOTALL | re.MULTILINE
    )
    assert block, f'variable "{name}" not found'
    default = re.search(r"^\s*default\s*=\s*(.+)$", block.group(), re.MULTILINE)
    assert default, f'variable "{name}" has no default'
    return default.group(1).strip()


def _terraform_rule_priorities() -> dict:
    """Parse the ``local.alb_priority`` map from public_alb_rules.tf."""
    tf = _read(PUBLIC_ALB_RULES_TF)
    block = re.search(r"alb_priority\s*=\s*\{(.*?)\n\s*\}", tf, re.DOTALL)
    assert block, "local.alb_priority map not found in public_alb_rules.tf"
    priorities = {
        name: int(value)
        for name, value in re.findall(
            r"^\s*(\w+)\s*=\s*(\d+)\s*$", block.group(1), re.MULTILINE
        )
    }
    assert priorities, "no named priorities parsed from local.alb_priority"
    return priorities


def _listener_rule_blocks() -> dict:
    """Return ``{rule_name: block_source}`` for every ALB listener rule.

    Every ``.tf`` file is read, not only the public rules file: a rule declared
    anywhere else routes on the same listener and can shadow public routing.
    """
    blocks = {}
    for path in sorted(TERRAFORM_DIR.glob("*.tf")):
        for match in re.finditer(
            r'resource\s+"aws_lb_listener_rule"\s+"(\w+)"\s*\{'
            r".*?(?=^(?:resource|data|output|locals)\s|\Z)",
            _read(path),
            re.DOTALL | re.MULTILINE,
        ):
            blocks[match.group(1)] = match.group()
    assert blocks, "no aws_lb_listener_rule resources parsed"
    return blocks


def _rule_path_patterns(block: str):
    """Return the rule's ``path_pattern`` values, or None if it has none."""
    match = re.search(r"path_pattern\s*\{\s*values\s*=\s*\[(.*?)\]", block, re.DOTALL)
    if not match:
        return None
    values = tuple(re.findall(r'"([^"]+)"', match.group(1)))
    assert values, "path_pattern block has an empty values list"
    return values


def _rule_forward_target(block: str) -> str:
    """Return the target group a rule forwards to, e.g. ``public``."""
    match = re.search(
        r'action\s*\{\s*type\s*=\s*"forward"\s*\n\s*'
        r"target_group_arn\s*=\s*aws_lb_target_group\.(\w+)\.arn",
        block,
    )
    assert match, "rule has no forward action onto a named target group"
    return match.group(1)


def _rule_priority_key(block: str) -> str:
    match = re.search(r"priority\s*=\s*local\.alb_priority\.(\w+)", block)
    assert match, "rule priority is not a local.alb_priority reference"
    return match.group(1)


def _nested_block(source: str, name: str) -> str:
    """Return the body of one leaf block, e.g. ``health_check { ... }``."""
    match = re.search(r"%s\s*\{([^{}]*)\}" % re.escape(name), source)
    assert match, f"no {name} block found"
    return match.group(1)


def _attribute(source: str, name: str) -> str:
    """Return one ``<name> = <value>`` right-hand side, anchored at line start
    so ``healthy_threshold`` cannot match inside ``unhealthy_threshold``."""
    match = re.search(
        r"^\s*%s\s*=\s*(.+?)\s*(?:#.*)?$" % re.escape(name), source, re.MULTILINE
    )
    assert match, f"attribute {name} not found"
    return match.group(1)


class TestAlbPriorityBandsAreDisjoint:
    """Premium ALB rules cannot collide with the static terraform rules.

    The premium band is [100, MAX_PREMIUM_PRIORITY] and is allocated by the
    Lambda at assignment time. The public and free bands are terraform. A
    collision means a premium user's dedicated rule either fails to create or
    displaces the rule that routes ``/api/public/*`` to the public tier.
    """

    def test_every_terraform_priority_sits_above_the_premium_band(self):
        priorities = _terraform_rule_priorities()
        colliding = {
            name: value
            for name, value in priorities.items()
            if value <= MAX_PREMIUM_PRIORITY
        }
        assert not colliding, (
            f"terraform ALB rule priorities inside the premium band "
            f"[{PREMIUM_BAND_START}, {MAX_PREMIUM_PRIORITY}]: {colliding}"
        )

    def test_terraform_priorities_are_unique(self):
        priorities = _terraform_rule_priorities()
        seen = {}
        for name, value in priorities.items():
            seen.setdefault(value, []).append(name)
        duplicates = {v: names for v, names in seen.items() if len(names) > 1}
        assert not duplicates, f"duplicate ALB rule priorities: {duplicates}"

    def test_every_declared_priority_is_referenced_by_a_rule(self):
        """An orphaned entry in the map is a rule someone deleted without
        freeing its number, which silently shrinks the usable band."""
        tf = _read(PUBLIC_ALB_RULES_TF)
        referenced = set(re.findall(r"local\.alb_priority\.(\w+)", tf))
        declared = set(_terraform_rule_priorities())
        assert declared == referenced, (
            f"declared but unused: {declared - referenced}; "
            f"used but undeclared: {referenced - declared}"
        )

    def test_allocator_refuses_to_leave_the_premium_band(self):
        """The cap is enforced, not merely declared.

        With every priority in the band taken, the allocator must raise rather
        than return the next integer - which would be the lowest terraform
        priority and would steal a public-tier route.
        """
        band_is_full = {
            "Rules": [
                {"Priority": str(p)}
                for p in range(PREMIUM_BAND_START, MAX_PREMIUM_PRIORITY + 1)
            ]
        }

        with pytest.raises(Exception) as excinfo:
            with _fake_elbv2_rules(band_is_full):
                get_next_available_priority(
                    "arn:aws:elasticloadbalancing:region:account:listener/test"
                )

        assert "No available ALB rule priorities" in str(excinfo.value)

    def test_the_bands_are_adjacent_so_exhaustion_is_the_only_guard(self):
        """There is no numeric buffer between the premium cap and the first
        terraform rule, which is why the exhaustion case above must raise
        rather than fall through to the next integer."""
        assert min(_terraform_rule_priorities().values()) == MAX_PREMIUM_PRIORITY + 1

    def test_allocator_returns_a_priority_inside_the_band(self):
        """Sanity companion to the exhaustion case: the happy path must stay
        in-band, so the exhaustion test above is not passing for the wrong
        reason."""
        one_taken = {"Rules": [{"Priority": "100"}, {"Priority": "default"}]}

        with _fake_elbv2_rules(one_taken):
            priority = get_next_available_priority(
                "arn:aws:elasticloadbalancing:region:account:listener/test"
            )

        assert PREMIUM_BAND_START <= priority <= MAX_PREMIUM_PRIORITY
        assert priority == 101


class TestAlbListenerDefaultAction:
    """Unmatched traffic lands on the public tier, not the free tier.

    The free tier is scaled to zero whenever no one is signed in, so a default
    action pointing at it takes the SPA shell down with it - every route the
    listener rules do not name would 503 for anonymous visitors.
    """

    @pytest.fixture(autouse=True)
    def _load(self):
        listener = _resource_block(
            _read(COMPUTE_TF), "aws_lb_listener", "autoscaling_https"
        )
        self.default_action = _nested_block(listener, "default_action")

    def test_default_action_forwards_to_the_public_target_group(self):
        assert _attribute(self.default_action, "type") == '"forward"'
        assert (
            _attribute(self.default_action, "target_group_arn")
            == "aws_lb_target_group.public.arn"
        )

    def test_default_action_does_not_reference_the_free_target_group(self):
        assert "aws_lb_target_group.autoscaling" not in self.default_action


class TestAlbRuleRouting:
    """Every listener rule's (path pattern, priority, target group) triple.

    A rule that keeps its priority but swaps target group moves a whole path
    family between tiers, and stays invisible until the receiving tier is the
    one that is stopped.
    """

    @pytest.fixture(autouse=True)
    def _load(self):
        self.blocks = _listener_rule_blocks()
        self.priorities = _terraform_rule_priorities()

    def test_the_declared_rule_set_is_exactly_the_pinned_set(self):
        """A rule added or removed without updating this table would otherwise
        go uncovered by the per-rule cases below."""
        assert set(self.blocks) == set(EXPECTED_ALB_ROUTING)

    @pytest.mark.parametrize("name", sorted(EXPECTED_ALB_ROUTING))
    def test_rule_routes_its_paths_to_the_intended_tier(self, name):
        priority, patterns, target = EXPECTED_ALB_ROUTING[name]
        block = self.blocks[name]

        key = _rule_priority_key(block)
        assert key == name, f"{name} takes its priority from alb_priority.{key}"
        assert self.priorities[key] == priority
        assert _rule_path_patterns(block) == patterns
        assert _rule_forward_target(block) == target

    def test_every_rule_attaches_to_the_main_https_listener(self):
        """A rule on the port-80 redirect listener is never evaluated."""
        for name, block in self.blocks.items():
            assert re.search(
                r"listener_arn\s*=\s*aws_lb_listener\.autoscaling_https\.arn", block
            ), f"{name} is not attached to the https listener"

    def test_the_public_visualization_rule_is_gated_on_its_header(self):
        """p280 and p315 carry the same path pattern. Drop the header condition
        and p280 shadows p315, sending every authenticated visualization read
        to the public tier, which cannot see the caller's own data."""
        header = _nested_block(
            self.blocks["visualizations_public_header"], "http_header"
        )
        assert _attribute(header, "http_header_name") == '"DATAVIEW_PUBLIC_REQUEST"'
        assert _attribute(header, "values") == '["true"]'

    def test_the_bearer_catch_all_matches_on_the_authorization_header(self):
        block = self.blocks["authenticated_to_free"]
        header = _nested_block(block, "http_header")
        assert _attribute(header, "http_header_name") == '"Authorization"'
        assert _attribute(header, "values") == '["Bearer *"]'


class TestAnonymousRegistrationPath:
    """``/api/register`` is pinned without a trailing slash.

    ALB path patterns are exact. ``/api/register/`` matches nothing the SPA
    sends, so the POST misses this rule, misses the p320 Bearer catch-all
    (an anonymous caller has no token) and falls through to the listener
    default onto the public tier - which does not mount the registration
    router at all.
    """

    @pytest.fixture(autouse=True)
    def _load(self):
        self.patterns = _rule_path_patterns(
            _listener_rule_blocks()["anonymous_flows_to_free"]
        )
        assert self.patterns is not None, "anonymous_flows_to_free has no path_pattern"

    def test_the_bare_register_path_is_present(self):
        assert "/api/register" in self.patterns

    def test_no_anonymous_path_carries_a_trailing_slash(self):
        trailing = [p for p in self.patterns if p.endswith("/")]
        assert not trailing, f"path patterns with a trailing slash: {trailing}"


class TestPublicTargetGroupHealthCheck:
    """The public target group's health check.

    A slower interval or a higher unhealthy threshold widens the window in
    which the ALB keeps sending SPA requests to a dead task.
    """

    @pytest.fixture(autouse=True)
    def _load(self):
        target_group = _resource_block(
            _read(PUBLIC_SERVICE_TF), "aws_lb_target_group", "public"
        )
        self.health_check = _nested_block(target_group, "health_check")

    def test_health_check_is_enabled(self):
        assert _attribute(self.health_check, "enabled") == "true"

    def test_health_check_path_and_matcher(self):
        assert _attribute(self.health_check, "path") == '"/health"'
        assert _attribute(self.health_check, "matcher") == '"200"'
        assert _attribute(self.health_check, "protocol") == '"HTTP"'
        assert _attribute(self.health_check, "port") == '"traffic-port"'

    def test_health_check_interval_and_timeout(self):
        assert int(_attribute(self.health_check, "interval")) == 30
        assert int(_attribute(self.health_check, "timeout")) == 5

    def test_health_check_thresholds(self):
        assert int(_attribute(self.health_check, "healthy_threshold")) == 2
        assert int(_attribute(self.health_check, "unhealthy_threshold")) == 3


class TestPublishedDataSurvivesTaskReplacement:
    """Published experiments outlive the task that wrote them.

    The output cache lives on EFS rather than the task's own storage, so the
    three things that have to hold are: the filesystem keeps its identity
    across applies, the container mounts it where the app writes output, and
    in-flight reads get drained rather than cut.
    """

    def test_efs_creation_token_is_pinned(self):
        """The creation token is the filesystem's identity - changing it makes
        terraform replace the volume and drop every published experiment."""
        file_system = _resource_block(
            _read(INFRASTRUCTURE_TF), "aws_efs_file_system", "published_data"
        )
        token = _attribute(file_system, "creation_token")
        assert token == '"${local.env_prefix}-public-published-data"'

    def test_published_data_volume_mounts_at_the_output_directory(self):
        task = _resource_block(
            _read(PUBLIC_SERVICE_TF), "aws_ecs_task_definition", "public"
        )
        match = re.search(
            r'sourceVolume\s*=\s*"%s"\s*\n\s*containerPath\s*=\s*"([^"]+)"'
            % re.escape(PUBLISHED_DATA_VOLUME),
            task,
        )
        assert match, "the published-data volume is absent from public mountPoints"
        assert match.group(1) == "/app/studio_data/output"

    def test_that_volume_resolves_to_the_published_data_access_point(self):
        task = _resource_block(
            _read(PUBLIC_SERVICE_TF), "aws_ecs_task_definition", "public"
        )
        match = re.search(
            r'volume\s*\{\s*name\s*=\s*"%s".*?'
            r"access_point_id\s*=\s*aws_efs_access_point\.(\w+)\.id"
            % re.escape(PUBLISHED_DATA_VOLUME),
            task,
            re.DOTALL,
        )
        assert match, f"no efs_volume_configuration for {PUBLISHED_DATA_VOLUME}"
        assert match.group(1) == "published_data"

    def test_target_group_drains_for_600_seconds(self):
        target_group = _resource_block(
            _read(PUBLIC_SERVICE_TF), "aws_lb_target_group", "public"
        )
        assert int(_attribute(target_group, "deregistration_delay")) == 600


class TestPublicUnhealthyHostsAlarm:
    """The public target group has its own UnHealthyHostCount alarm.

    The free tier's alarm cannot cover it: an all-unhealthy public target
    group takes SPA delivery down for every tier at once, so this key has to
    exist separately and has to notify on recovery as well as on breach.
    """

    @pytest.fixture(autouse=True)
    def _load(self):
        self.alarm = _resource_block(
            _read(MONITORING_TF), "aws_cloudwatch_metric_alarm", "tg_unhealthy_hosts"
        )

    def test_a_public_key_watches_the_public_target_group(self):
        match = re.search(
            r"^\s*public\s*=\s*\{\s*\n\s*"
            r"tg_arn_suffix\s*=\s*aws_lb_target_group\.(\w+)\.arn_suffix",
            self.alarm,
            re.MULTILINE,
        )
        assert match, "tg_unhealthy_hosts has no public for_each key"
        assert match.group(1) == "public"

    def test_alarm_name_is_keyed_per_target_group(self):
        name = _attribute(self.alarm, "alarm_name")
        assert name == '"${local.env_prefix}-${each.key}-tg-unhealthy-hosts"'

    def test_alarm_watches_unhealthy_host_count(self):
        assert _attribute(self.alarm, "metric_name") == '"UnHealthyHostCount"'
        assert _attribute(self.alarm, "namespace") == '"AWS/ApplicationELB"'
        assert _attribute(self.alarm, "statistic") == '"Maximum"'

    def test_alarm_period_and_thresholds(self):
        assert int(_attribute(self.alarm, "period").strip('"')) == 60
        assert int(_attribute(self.alarm, "evaluation_periods").strip('"')) == 2
        assert int(_attribute(self.alarm, "threshold").strip('"')) == 0
        assert _attribute(self.alarm, "comparison_operator") == '"GreaterThanThreshold"'

    def test_breach_and_recovery_are_both_wired(self):
        assert (
            _attribute(self.alarm, "alarm_actions") == "local.critical_alerts_actions"
        )
        assert _attribute(self.alarm, "ok_actions") == "local.critical_alerts_actions"


class TestPublicServicePlacement:
    """Public tasks are constrained to public-tier instances.

    The cluster is shared with the free and premium ASGs. Without the
    constraint, a public task can be scheduled onto a free instance, where it
    would be registered into the public target group and serve nothing.
    """

    @pytest.fixture(autouse=True)
    def _load(self):
        service = _resource_block(_read(PUBLIC_SERVICE_TF), "aws_ecs_service", "public")
        self.constraint = _nested_block(service, "placement_constraints")

    def test_placement_constraint_is_a_member_of_expression(self):
        assert _attribute(self.constraint, "type") == '"memberOf"'

    def test_placement_constraint_selects_the_public_tier_attribute(self):
        assert _attribute(self.constraint, "expression") == '"attribute:tier == public"'


class TestPublishedDataEfsLifecycle:
    """The published-data EFS transitions to IA after 7 days.

    Configuration assertion. The deployed check (that AWS actually moved the
    objects) stays manual.
    """

    def test_published_data_transitions_to_ia_after_7_days(self):
        block = _resource_block(
            _read(INFRASTRUCTURE_TF), "aws_efs_file_system", "published_data"
        )
        match = re.search(
            r"lifecycle_policy\s*\{[^}]*?transition_to_ia\s*=\s*\"([^\"]+)\"", block
        )
        assert match, "no lifecycle_policy/transition_to_ia on the published_data EFS"
        assert match.group(1) == "AFTER_7_DAYS"


class TestPublicCleanupSchedule:
    """The input-cache cleanup Lambda runs once a day, and is wired.

    The cache is unbounded between runs, so a rule that is disabled or
    retargeted stops being visible until EFS fills up.
    """

    @pytest.fixture(autouse=True)
    def _load(self):
        self.tf = _read(PUBLIC_CLEANUP_TF)
        self.rule = _resource_block(
            self.tf, "aws_cloudwatch_event_rule", "public_cleanup_schedule"
        )

    def test_schedule_fires_once_a_day(self):
        match = re.search(r"schedule_expression\s*=\s*\"([^\"]+)\"", self.rule)
        assert match, "public_cleanup_schedule has no schedule_expression"
        expression = match.group(1)

        cron = re.fullmatch(r"cron\((.+)\)", expression)
        assert cron, f"expected a cron() schedule, got {expression!r}"

        minute, hour, day_of_month, month, day_of_week, year = cron.group(1).split()
        assert minute.isdigit(), f"minute {minute!r} is not a single fixed minute"
        assert hour.isdigit(), f"hour {hour!r} is not a single fixed hour"
        assert (day_of_month, month, year) == ("*", "*", "*")
        assert day_of_week == "?", f"day_of_week {day_of_week!r} restricts the days"

    def test_schedule_fires_at_1900_utc(self):
        """19:00 UTC is 04:00 JST: the wipe has to land outside working hours
        because a concurrent read of a swept path re-syncs from S3."""
        match = re.search(r'schedule_expression\s*=\s*"cron\(([^)]+)\)"', self.rule)
        assert match, "public_cleanup_schedule has no cron() schedule_expression"
        minute, hour = match.group(1).split()[:2]
        assert (minute, hour) == ("0", "19")

    def test_rule_name_is_environment_scoped(self):
        """A name without the environment prefix collides across environments,
        and the target and lambda permission both resolve it by name."""
        assert _attribute(self.rule, "name") == (
            '"${var.environment}-public-cleanup-schedule"'
        )

    def test_schedule_is_enabled(self):
        assert re.search(r'state\s*=\s*"ENABLED"', self.rule), (
            "public_cleanup_schedule is not ENABLED, so the input cache is "
            "never wiped"
        )

    def test_schedule_targets_the_cleanup_lambda_and_may_invoke_it(self):
        target = _resource_block(
            self.tf, "aws_cloudwatch_event_target", "public_cleanup_target"
        )
        assert "aws_lambda_function.public_cleanup.arn" in target

        permission = _resource_block(
            self.tf, "aws_lambda_permission", "allow_cloudwatch_public_cleanup"
        )
        assert "aws_lambda_function.public_cleanup.function_name" in permission
        assert (
            "aws_cloudwatch_event_rule.public_cleanup_schedule.arn" in permission
        ), "the rule cannot invoke the Lambda without a matching source_arn"


class TestPublicLogGroup:
    """The public log group's name and retention, and that the public
    container actually logs to it."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.tf = _read(PUBLIC_SERVICE_TF)
        self.log_group = _resource_block(
            self.tf, "aws_cloudwatch_log_group", "public_optinist"
        )

    def test_log_group_name(self):
        match = re.search(r'name\s*=\s*"([^"]+)"', self.log_group)
        assert match, "public log group has no name"
        assert match.group(1) == "/ecs/${var.environment}-public-optinist-cloud-taskdef"

    def test_log_group_retention_is_30_days(self):
        match = re.search(r"retention_in_days\s*=\s*(\d+)", self.log_group)
        assert match, "public log group has no retention_in_days"
        assert int(match.group(1)) == 30

    def test_public_task_logs_into_that_group(self):
        """A correct log group nothing writes to is the same as no log group."""
        task = _resource_block(self.tf, "aws_ecs_task_definition", "public")
        match = re.search(r'"awslogs-group"\s*=\s*([^\n]+?)\s*$', task, re.MULTILINE)
        assert match, "public task definition has no awslogs-group"
        assert match.group(1) == "aws_cloudwatch_log_group.public_optinist.name"


class TestPublicAsgCapacity:
    """The public ASG's min_size and desired_capacity.

    The public tier serves the SPA shell for every tier, including while free
    is stopped, so a single instance makes SPA delivery a single point of
    failure across two AZs.
    """

    @pytest.fixture(autouse=True)
    def _load(self):
        self.asg = _resource_block(
            _read(PUBLIC_SERVICE_TF), "aws_autoscaling_group", "public"
        )
        main_tf = _read(MAIN_TF)
        self.min_size = int(_variable_default(main_tf, "public_asg_min_size"))
        self.max_size = int(_variable_default(main_tf, "public_asg_max_size"))
        self.desired = int(_variable_default(main_tf, "public_asg_desired_capacity"))

    def test_asg_reads_the_public_capacity_variables(self):
        """Not the free tier's ``asg_*`` variables, and not a literal."""
        assert re.search(r"min_size\s*=\s*var\.public_asg_min_size", self.asg)
        assert re.search(r"max_size\s*=\s*var\.public_asg_max_size", self.asg)
        assert re.search(
            r"desired_capacity\s*=\s*var\.public_asg_desired_capacity", self.asg
        )

    def test_capacity_defaults_keep_two_azs_covered(self):
        assert self.min_size >= 2, (
            f"public_asg_min_size is {self.min_size}; the public tier spans two "
            f"subnets and serves the SPA for every tier"
        )
        assert self.min_size <= self.desired <= self.max_size

    def test_asg_replaces_instances_the_load_balancer_reports_unhealthy(self):
        """``EC2`` health checks only see the instance, not the container, so a
        task that stops serving would never be replaced."""
        assert _attribute(self.asg, "health_check_type") == '"ELB"'

    def test_asg_grace_period_covers_a_cold_boot(self):
        """The ECR pull plus container start plus startup S3 warm runs past
        five minutes; a shorter grace kills instances mid-boot in a loop."""
        assert int(_attribute(self.asg, "health_check_grace_period")) == 900

    def test_asg_terminates_the_oldest_instance_first(self):
        assert _attribute(self.asg, "termination_policies") == '["OldestInstance"]'

    def test_ecs_desired_count_tracks_the_asg(self):
        """One task per instance. If these desync, instances run no container
        and the target group reports them unhealthy."""
        service = _resource_block(_read(PUBLIC_SERVICE_TF), "aws_ecs_service", "public")
        assert re.search(
            r"desired_count\s*=\s*var\.public_asg_desired_capacity", service
        )


def _fake_elbv2_rules(describe_rules_response):
    """Patch the boto3 client premium_manager builds, returning fixed rules."""
    from unittest.mock import MagicMock, patch

    client = MagicMock()
    client.describe_rules.return_value = describe_rules_response
    return patch("premium_manager.boto3.client", return_value=client)
