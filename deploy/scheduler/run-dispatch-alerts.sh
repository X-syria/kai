#!/usr/bin/env sh
#
# Scheduled entrypoint for the durable alert-outbox dispatcher.
#
# The application never owns a schedule (see src/kai/maintenance/alert_dispatch.py).
# An external scheduler runs this script every minute and the process exits.
#
# What this script adds on top of calling the command directly:
#
#   * a hard timeout, so a run wedged on an unreachable destination or database
#     cannot outlive its minute and overlap the next one;
#   * the credentials and the Compose project resolved from the environment the
#     deployment already uses, with no secret written into this repository;
#   * a scheduler-level outcome record on stdout for every run, including the
#     ones that succeed, because "the worker stopped running" is only detectable
#     if something records that it ran.
#
# Exit codes are the scheduler's contract:
#
#   0  the sweep completed (even if individual deliveries were refused), or
#      alerting is disabled so nothing was claimed
#   1  the sweep failed: database error, command crash, or timeout
#
# A refused delivery is deliberately 0. It is an outcome the dispatcher records
# as a permanent failure and exposes on kai_alerts_permanently_failed, not a
# fault of the job; paging the scheduler for it would double-report and train
# people to ignore this job. See docs/ALERTING.md.

set -eu

#: Hard ceiling on one run. Kept below the one-minute cadence so a slow sweep is
#: killed and its claims reclaimed by lease expiry rather than piling up. Any
#: rows still DELIVERING when the process dies are recovered on the next sweep.
#: Overridable so an operator can raise it without editing this file.
: "${KAI_DISPATCH_TIMEOUT_SECONDS:=55}"

#: Compose project name. Must match the deployment's project so this script
#: reaches the same database the app does rather than creating a second stack.
: "${KAI_COMPOSE_PROJECT_NAME:=${COMPOSE_PROJECT_NAME:-kai}}"

JOB="kai_dispatch_alerts"

log() {
    # One JSON object per line, same shape as KAI's own structured logs, so the
    # deployment's existing log pipeline parses scheduler and application events
    # with one parser. The level is always log/event, never a message field.
    printf '{"event":"%s","job":"%s","status":"%s","exit_code":"%s","duration_seconds":"%s","pid":"%s","project":"%s"}\n' \
        "$1" "$JOB" "$2" "$3" "$4" "$$" "$KAI_COMPOSE_PROJECT_NAME"
}

if ! command -v timeout >/dev/null 2>&1; then
    log scheduler_run error 127 0
    echo "kai-dispatch-alerts: GNU timeout is required but not installed" >&2
    exit 1
fi

started="$(date +%s 2>/dev/null || echo 0)"

set +e
# `docker compose run --rm` is the same image and the same command the service is
# built from, so a scheduled run cannot drift from the code it delivers.
# --rm keeps the one-shot container out of the way; -T disables TTY allocation,
# which cron has no use for and which would make run fail without a terminal.
timeout --signal=TERM --kill-after=15 "${KAI_DISPATCH_TIMEOUT_SECONDS}" \
    docker compose --project-name "$KAI_COMPOSE_PROJECT_NAME" \
    run --rm -T dispatch-alerts
code=$?
set -e

ended="$(date +%s 2>/dev/null || echo 0)"
duration=$((ended - started))

case "$code" in
    0)
        # Includes the disabled sweep. Reported as ok so a deployment with
        # alerting off is visible in the logs without being counted as a failure.
        log scheduler_run ok 0 "$duration"
        ;;
    124 | 137)
        # 124 is timeout's own code; 137 is SIGKILL, which is what --kill-after
        # escalates to when the process ignores TERM.
        log scheduler_run timeout "$code" "$duration"
        ;;
    *)
        log scheduler_run failed "$code" "$duration"
        ;;
esac

exit "$code"
