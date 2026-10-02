#!/usr/bin/env sh
#
# Scheduled entrypoint for the expired-revocation purge.
#
# The application never owns a schedule (see src/kai/maintenance/revocation_purge.py).
# An external scheduler runs this script every 15 minutes and the process exits.
#
# What this script adds on top of calling the command directly:
#
#   * a hard timeout, so a run wedged on an unreachable database cannot outlive
#     its window and overlap the next one;
#   * the credentials and the Compose project resolved from the environment the
#     deployment already uses, with no secret written into this repository;
#   * a scheduler-level outcome record on stdout for every run, including the
#     ones that succeed, because "the job stopped running" is only detectable
#     if something records that it ran.
#
# Exit codes are the scheduler's contract:
#
#   0  the run completed, or another run held the advisory lock (overlap)
#   1  the purge failed: database error, command crash, or timeout
#
# `lock_not_acquired` is deliberately 0. Two runs overlapping is the scheduler
# working as designed, not a fault, and paging someone for it trains people to
# ignore this job. See docs/ALERTING.md.

set -eu

#: Hard ceiling on one run. The purge is a single DELETE of already-expired
#: rows; anything near this is a stuck connection, not a large table. Overridable
#: so an operator can raise it without editing this file.
: "${KAI_PURGE_TIMEOUT_SECONDS:=120}"

#: Compose project name. Must match the deployment's project so this script
#: reaches the same database the app does rather than creating a second stack.
: "${KAI_COMPOSE_PROJECT_NAME:=${COMPOSE_PROJECT_NAME:-kai}}"

JOB="kai_purge_revocations"

log() {
    # One JSON object per line, same shape as KAI's own structured logs, so the
    # deployment's existing log pipeline parses scheduler and application events
    # with one parser. The level is always log/event, never a message field.
    printf '{"event":"%s","job":"%s","status":"%s","exit_code":"%s","duration_seconds":"%s","pid":"%s","project":"%s"}\n' \
        "$1" "$JOB" "$2" "$3" "$4" "$$" "$KAI_COMPOSE_PROJECT_NAME"
}

if ! command -v timeout >/dev/null 2>&1; then
    log scheduler_run error 127 0
    echo "kai-purge: GNU timeout is required but not installed" >&2
    exit 1
fi

started="$(date +%s 2>/dev/null || echo 0)"

set +e
# `docker compose run --rm` is the same image and the same command the service
# is built from, so a scheduled run cannot drift from the code it purges.
# --rm keeps the one-shot container out of the way; -T disables TTY allocation,
# which cron has no use for and which would make run fail without a terminal.
timeout --signal=TERM --kill-after=15 "${KAI_PURGE_TIMEOUT_SECONDS}" \
    docker compose --project-name "$KAI_COMPOSE_PROJECT_NAME" \
    run --rm -T purge-revocations
code=$?
set -e

ended="$(date +%s 2>/dev/null || echo 0)"
duration=$((ended - started))

case "$code" in
    0)
        # Includes lock_not_acquired. Reported as ok so an overlap is visible in
        # the logs without being counted as a failure anywhere.
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
