#!/usr/bin/env bash
# ApplyPilot launcher — starts the bridge and keeps it running.
#
# Usage:
#   ./start.sh            start the sync bridge (panel Start button goes live)
#   ./start.sh check      run setup + connectivity checks and exit
#   ./start.sh once       run one pipeline + apply cycle locally, no panel
#
# The bridge is the only thing that needs to stay running. Everything else is
# driven from the web panel.

set -uo pipefail

APP_DIR="${APPLYPILOT_DIR:-$HOME/.applypilot}"
LOG_DIR="$APP_DIR/logs"
RESTART_DELAY=15

mkdir -p "$LOG_DIR"

have() { command -v "$1" >/dev/null 2>&1; }

if ! have applypilot; then
    echo "applypilot is not installed. Install it with:"
    echo "  pip install applypilot"
    echo "  pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex"
    exit 1
fi

case "${1:-run}" in
    check)
        applypilot doctor
        echo
        applypilot verify
        exit $?
        ;;

    once)
        echo "Running one full cycle locally (no panel)..."
        applypilot run all || echo "Pipeline finished with errors — continuing to apply stage"
        applypilot apply --limit 25
        exit $?
        ;;

    run)
        # Refuse to start until setup is genuinely complete: a bridge running
        # against an empty profile just reports 'blocked' forever.
        if [ ! -f "$APP_DIR/profile.json" ] || [ ! -f "$APP_DIR/resume.txt" ]; then
            echo "Setup incomplete — run 'applypilot init' first."
            echo "  profile.json: $([ -f "$APP_DIR/profile.json" ] && echo present || echo MISSING)"
            echo "  resume.txt:   $([ -f "$APP_DIR/resume.txt" ] && echo present || echo MISSING)"
            exit 1
        fi

        if ! grep -q "SUPABASE_URL" "$APP_DIR/.env" 2>/dev/null; then
            echo "SUPABASE_URL is not set in $APP_DIR/.env — the panel cannot connect."
            echo "Add SUPABASE_URL and SUPABASE_SERVICE_KEY, then rerun."
            exit 1
        fi

        echo "Starting ApplyPilot bridge. Press Ctrl+C to stop."
        echo "Logs: $LOG_DIR/sync.log"
        echo

        # Auto-restart on crash so a transient network failure doesn't leave
        # the panel dark until you notice.
        trap 'echo; echo "Stopped."; exit 0' INT TERM
        while true; do
            applypilot sync 2>&1 | tee -a "$LOG_DIR/sync.log"
            code=${PIPESTATUS[0]}
            [ "$code" -eq 0 ] && break
            echo "Bridge exited (code $code). Restarting in ${RESTART_DELAY}s..."
            sleep "$RESTART_DELAY"
        done
        ;;

    *)
        echo "Usage: $0 [run|check|once]"
        exit 1
        ;;
esac
