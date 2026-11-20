#!/bin/bash
set -e

ORCA_VERSION="${ORCA_VERSION:-2.4.2}"
ORCA_INSTALL_DIR="/opt/orcaslicer"
ORCA_VERSION_FILE="${ORCA_INSTALL_DIR}/.version"

# Install OrcaSlicer only when the version on disk doesn't match the requested one.
# The install dir is expected to be a named volume so the download is cached across restarts.
if [ -f "$ORCA_VERSION_FILE" ] && [ "$(cat "$ORCA_VERSION_FILE")" = "$ORCA_VERSION" ]; then
    echo "[laminus] OrcaSlicer v${ORCA_VERSION} already installed, skipping download."
else
    echo "[laminus] Installing OrcaSlicer v${ORCA_VERSION}..."
    APPIMAGE_URL="https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v${ORCA_VERSION}/OrcaSlicer_Linux_AppImage_Ubuntu2404_V${ORCA_VERSION}.AppImage"

    staging=$(mktemp -d)
    trap 'rm -rf "$staging"' EXIT
    curl --retry 3 -fsSL -o "$staging/OrcaSlicer.AppImage" "$APPIMAGE_URL"
    chmod +x "$staging/OrcaSlicer.AppImage"
    (cd "$staging" && ./OrcaSlicer.AppImage --appimage-extract > /dev/null)
    test -x "$staging/squashfs-root/AppRun"

    # Keep the previous version usable if downloading or extracting fails.
    mkdir -p "$ORCA_INSTALL_DIR"
    find "$ORCA_INSTALL_DIR" -mindepth 1 -delete
    cp -a "$staging/squashfs-root/." "$ORCA_INSTALL_DIR/"
    rm -rf "$staging"
    trap - EXIT

    echo "$ORCA_VERSION" > "$ORCA_VERSION_FILE"
    echo "[laminus] OrcaSlicer v${ORCA_VERSION} installed."
fi

# Symlink may be absent if the volume was freshly mounted; always (re)create it.
ln -sf "${ORCA_INSTALL_DIR}/AppRun" /usr/local/bin/orcaslicer

# The desktop command manages its own persistent datadir.
if [ "${1:-}" = "orca-desktop" ]; then
    exec "$@"
fi

# OrcaSlicer is given its own --datadir (ORCA_DATADIR, read by main.py), not the
# durable /config volume directly. Left to itself Orca fills --datadir with cache/
# and log/ alongside user/ and plugins/ — cache/ is pure rebuildable scratch and
# log/ grows without bound, neither belongs in a volume that gets backed up. Orca
# gets a disposable datadir instead, and only the two subtrees worth keeping are
# symlinked back into /config, so the durable volume never contains anything but
# user presets and installed plugins.
export ORCA_DATADIR="/var/lib/laminus/orca-scratch"
mkdir -p "$ORCA_DATADIR" /config/user /config/plugins
ln -sfn /config/user "$ORCA_DATADIR/user"
ln -sfn /config/plugins "$ORCA_DATADIR/plugins"

exec "$@"
