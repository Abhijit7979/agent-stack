#!/usr/bin/env bash
set -euo pipefail
source /stack/config/stack.env
export REPO TRIAGE_MODEL

# Idempotent: labels + cron jobs on every boot, so a fresh VPS needs no manual exec.
bash /stack/deploy/install.sh

# The image supervises its own Hermes gateway; the container command only
# needs to run the Slack intake process.
exec python /stack/scripts/slack_intake.py
