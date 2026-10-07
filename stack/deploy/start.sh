#!/usr/bin/env bash
set -euo pipefail
source /stack/config/stack.env
export REPO

# The image supervises its own Hermes gateway; the container command only
# needs to run the Slack intake process.
exec python /stack/scripts/slack_intake.py
