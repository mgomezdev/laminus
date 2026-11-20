#!/usr/bin/env bash
set -euo pipefail

v1_version=2.4.2
v2_version=${ORCA_V2_VERSION:-2.5.1}
if [[ ! "$v2_version" =~ ^2\.5\.[0-9]+$ ]]; then
    echo "[laminus] ORCA_V2_VERSION must select a 2.5.x release" >&2
    exit 1
fi

export LAMINUS_SUPPORTED_API_VERSIONS=v1,v2

start_worker() {
    local api_version=$1 orca_version=$2 port=$3
    LAMINUS_API_VERSION="$api_version" ORCA_VERSION="$orca_version" \
        ORCA_INSTALL_DIR="/opt/orcaslicer/$api_version" \
        CONFIG_DIR="/config/$api_version" DATA_DIR="/data/$api_version" \
        USER_CONFIG_DIR="/config/$api_version/user" \
        SYSTEM_PROFILES_DIR="/opt/orcaslicer/$api_version/resources/profiles" \
        ORCA_DATADIR="/var/lib/laminus/$api_version" JOBS_DIR="/tmp/jobs/$api_version" \
        ARRANGE_DIR="/tmp/arrange/$api_version" CATALOG_CACHE_FILE="/tmp/laminus-$api_version-catalog.json" \
        /entrypoint.sh uvicorn app.main:app --host 127.0.0.1 --port "$port" &
    children+=("$!")
}

children=()
shutdown() {
    trap - TERM INT
    kill "${children[@]}" 2>/dev/null || true
    wait "${children[@]}" 2>/dev/null || true
}
trap 'shutdown; exit 0' TERM INT

start_worker v1 "$v1_version" 5001
start_worker v2 "$v2_version" 5002
nginx -c /etc/laminus/nginx.conf -g 'daemon off;' &
children+=("$!")

status=0
wait -n "${children[@]}" || status=$?
shutdown
exit "$status"
