"""Alert rules must be bound to signals that exist, and to an action.

An alert rule that names a metric KAI does not emit, or a label value outside the
closed set, is the most expensive kind of nothing: it loads cleanly, validates,
and can never fire. Nobody notices until the incident it was written for happens,
which is the one moment its absence costs something. The rules are therefore
checked against the live metric inventory rather than reviewed by eye.

Three properties are enforced.

**Reachability.** Every ``kai_*`` identifier in every expression must be a metric
this service actually exposes. Histogram suffixes are stripped, because
``kai_provider_latency_seconds_bucket`` is the same collector as
``kai_provider_latency_seconds``. ``up`` is Prometheus's own scrape signal.

**Closed label values.** KAI deliberately bounds every label to a closed set
(``docs/OBSERVABILITY.md``). A selector on ``reason="bad_credential"`` matches
nothing at runtime, and nothing at config time either.

**No page-worthy-on-first-glance alerts.** Every rule needs ``for``, ``severity``
and a summary, and ``for`` must be non-zero, so no alert can fire on a single
transient event. Separately, no rule may select ``result="lock_not_acquired"``:
an overlapping purge is exit 0 by design and paging for it is a false positive
by definition.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from kai.observability import metrics as M

REPO_ROOT = Path(__file__).resolve().parent.parent
ALERTING_DIR = REPO_ROOT / "deploy" / "alerting"
ACTIVE_RULES = ALERTING_DIR / "kai-alerts.yml"
PENDING_RULES = ALERTING_DIR / "kai-alerts.scheduler-pending.yml"

#: Every KAI metric starts with this, so no PromQL keyword or function name can
#: be mistaken for a metric by the reachability check.
KAI_METRIC = re.compile(r"\bkai_[a-z0-9_]+")

#: Suffixes Prometheus adds to a histogram's samples. They are the same
#: collector, so they are checked against the base name.
HISTOGRAM_SUFFIXES = ("_bucket", "_sum", "_count")

#: Metrics Prometheus itself provides, which KAI does not declare.
EXTERNAL_METRICS = {"up"}

#: The categories the release criteria require coverage for.
REQUIRED_CATEGORIES = {
    "kai-availability": "availability and observability",
    "kai-database": "database",
    "kai-authentication-abuse": "authentication abuse",
    "kai-providers": "provider",
    "kai-task-execution": "task execution",
}


def _emitted_metrics() -> dict[str, set[str]]:
    """The live inventory: metric name -> label names."""
    inventory: dict[str, set[str]] = {}
    for field in dataclasses.fields(M.get_metrics()):
        collector = getattr(M.get_metrics(), field.name)
        name = getattr(collector, "_name", None)
        if name is None:
            continue
        # A Counter is exposed as <name>_total; the object reports the stem.
        exposed = f"{name}_total" if isinstance(collector, type(collector)) and _is_counter(
            collector
        ) else name
        inventory[exposed] = set(getattr(collector, "_labelnames", ()))
    return inventory


def _is_counter(collector: Any) -> bool:
    return type(collector).__name__ == "Counter"


def _load(path: Path) -> list[dict[str, Any]]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules: list[dict[str, Any]] = []
    for group in document.get("groups") or []:
        for rule in group.get("rules") or []:
            if "alert" in rule:
                rules.append(rule)
    return rules


def _referenced_metrics(expr: str) -> set[str]:
    found = set()
    for raw in KAI_METRIC.findall(expr):
        name = raw
        for suffix in HISTOGRAM_SUFFIXES:
            if name.endswith(suffix):
                name = name[: -len(suffix)]
                break
        found.add(name)
    return found


ALL_RULES = {ACTIVE_RULES: _load(ACTIVE_RULES), PENDING_RULES: _load(PENDING_RULES)}
ACTIVE = ALL_RULES[ACTIVE_RULES]
PENDING = ALL_RULES[PENDING_RULES]


def test_the_rules_files_exist_and_parse() -> None:
    assert ACTIVE, f"{ACTIVE_RULES.name} declares no alert rules"
    assert PENDING, f"{PENDING_RULES.name} declares no alert rules"


@pytest.mark.parametrize("path", sorted(ALL_RULES, key=str), ids=lambda p: p.name)
def test_every_referenced_metric_is_actually_emitted(path: Path) -> None:
    """The rule must be able to fire. A name KAI never emits can never fire."""
    inventory = _emitted_metrics()
    unknown: dict[str, list[str]] = {}
    for rule in ALL_RULES[path]:
        for metric in _referenced_metrics(str(rule["expr"])):
            if metric in inventory or metric in EXTERNAL_METRICS:
                continue
            unknown.setdefault(metric, []).append(rule["alert"])
    assert not unknown, (
        f"{path.name} references metrics KAI does not emit: {unknown}. "
        f"Emitted: {sorted(inventory)}"
    )


@pytest.mark.parametrize("path", sorted(ALL_RULES, key=str), ids=lambda p: p.name)
def test_every_rule_is_actionable(path: Path) -> None:
    """No alert without a severity, a summary, or a settling window.

    ``for`` is what keeps a single 500 or one provider timeout from paging
    anyone, so a zero/absent ``for`` is treated as a defect rather than a style
    preference.
    """
    for rule in ALL_RULES[path]:
        name = rule["alert"]
        assert rule.get("labels", {}).get("severity"), f"{name} has no severity"
        assert rule.get("annotations", {}).get("summary"), f"{name} has no summary"
        assert "for" in rule, f"{name} has no settling window"
        assert str(rule["for"]) not in {"0s", "0", ""}, (
            f"{name} fires immediately; a single transient event would page"
        )
        assert str(rule["for"]) in {"1m", "2m", "3m", "4m", "5m", "10m", "14m", "15m", "30m"}


@pytest.mark.parametrize("path", sorted(ALL_RULES, key=str), ids=lambda p: p.name)
def test_no_alert_selects_the_expected_overlap(path: Path) -> None:
    """``lock_not_acquired`` is exit 0 by design and must never alert."""
    for rule in ALL_RULES[path]:
        assert M.PURGE_RESULT_LOCK_NOT_ACQUIRED not in str(rule["expr"]), (
            f"{rule['alert']} selects on an overlapping purge, which is not a fault"
        )
        assert M.PURGE_RESULT_LOCK_NOT_ACQUIRED not in rule.get(
            "annotations", {}
        ).get("description", "")


def test_active_rules_carry_no_purge_metrics() -> None:
    """The active file must not imply purge coverage that does not exist.

    The purge records its metrics into a registry that dies with the one-shot
    process, so those series are unreachable. A purge rule in the loaded file
    would be permanently blind while appearing to be coverage.
    """
    purge_series = {"kai_revocation_purge_runs_total", "kai_revocation_purged_rows_total"}
    for rule in ACTIVE:
        referenced = _referenced_metrics(str(rule["expr"]))
        assert not (referenced & purge_series), (
            f"{rule['alert']} in the active file reads the purge's unreachable series; "
            f"it belongs in {PENDING_RULES.name}"
        )
    active_alert_names = {rule["alert"] for rule in ACTIVE}
    assert not any("Purge" in name for name in active_alert_names), (
        f"purge alerts belong in {PENDING_RULES.name}: {active_alert_names}"
    )


def test_purge_alerts_exist_but_are_segregated() -> None:
    """The unreachable coverage is written down, not silently absent."""
    pending_names = {rule["alert"] for rule in PENDING}
    assert "KaiPurgeRepeatedlyFailing" in pending_names
    assert "KaiPurgeStoppedRunning" in pending_names
    assert PENDING_RULES.read_text(encoding="utf-8").lstrip().startswith("#")


@pytest.mark.parametrize("path", sorted(ALL_RULES, key=str), ids=lambda p: p.name)
def test_label_values_come_from_the_closed_sets(path: Path) -> None:
    """A value outside KAI's closed sets matches nothing, now or later."""
    closed: dict[str, set[str]] = {
        "reason": set(M.AUTH_FAILURE_REASONS) | set(M.RATE_LIMIT_REJECTION_REASONS),
        "status_class": {"2xx", "3xx", "4xx", "5xx"},
        "provider": set(M.PROVIDERS),
        "operation": set(M.PROVIDER_OPERATIONS) | set(M.DB_ERROR_OPERATIONS),
        "result": set(M.PURGE_RESULTS),
        "profile": set(M.LIVE_PROFILES) | {M.UNKNOWN_LABEL},
    }
    selector = re.compile(r"(\w+)\s*=\s*\"([^\"]*)\"")
    for rule in ALL_RULES[path]:
        expr = str(rule["expr"])
        for label, value in selector.findall(expr):
            allowed = closed.get(label)
            if allowed is None:
                continue
            assert value in allowed, (
                f"{rule['alert']} selects {label}=\"{value}\", which is outside the "
                f"closed set {sorted(allowed)}"
            )


def test_all_required_categories_have_coverage() -> None:
    """Each release-criteria category is represented by at least one alert."""
    document = yaml.safe_load(ACTIVE_RULES.read_text(encoding="utf-8"))
    covered: dict[str, str] = {}
    for group in document["groups"]:
        for rule in group.get("rules", []):
            if "alert" in rule:
                covered.setdefault(group["name"], rule["alert"])
    for group_name in REQUIRED_CATEGORIES:
        assert group_name in covered, (
            f"no alert group {group_name} ({REQUIRED_CATEGORIES[group_name]})"
        )


def test_metrics_endpoint_alerts_on_its_own_absence() -> None:
    """Every other rule is blind while the scrape is broken, so that is its own alert."""
    names = {rule["alert"] for rule in ACTIVE}
    assert "KaiMetricsEndpointUnavailable" in names
    assert any("absent(" in str(rule["expr"]) for rule in ACTIVE)


def test_alert_names_are_unique_across_both_files() -> None:
    """A duplicate name across files means one definition silently wins."""
    seen: dict[str, str] = {}
    for path in ALL_RULES:
        for rule in ALL_RULES[path]:
            name = rule["alert"]
            assert name not in seen, f"{name} defined in both {seen[name]} and {path.name}"
            seen[name] = path.name