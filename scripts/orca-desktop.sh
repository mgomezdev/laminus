#!/usr/bin/env bash
set -euo pipefail

case "${1:-}" in
    session)
        if [[ "${ORCA_DESKTOP_VNC:-0}" == 1 ]]; then
            x11vnc -quiet -rfbport 5900 -rfbauth "${ORCA_DESKTOP_ROOT}/vnc.pass" -forever -shared &
            vnc_pid=$!
            trap 'kill "$vnc_pid" 2>/dev/null || true' EXIT
            orcaslicer --datadir "${ORCA_DESKTOP_ROOT}/state" --debug 3
        else
            exec orcaslicer --datadir "${ORCA_DESKTOP_ROOT}/state" --debug 3
        fi
        ;;
    "")
        export ORCA_DESKTOP_ROOT="${ORCA_DESKTOP_ROOT:-/config/orca-desktop}"
        if [[ "${ORCA_DESKTOP_VNC:-0}" == 1 && ! -r "${ORCA_DESKTOP_ROOT}/vnc.pass" ]]; then
            echo "[laminus] VNC requires a readable ${ORCA_DESKTOP_ROOT}/vnc.pass" >&2
            exit 1
        fi
        orca-diagnostics seed
        orca-diagnostics plugins
        exec xvfb-run -a -s "-screen 0 1600x1000x24" "$0" session
        ;;
    *)
        echo "Usage: orca-desktop" >&2
        exit 2
        ;;
esac
