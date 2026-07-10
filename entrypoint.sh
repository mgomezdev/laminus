#!/bin/bash
set -e

ORCA_VERSION="${ORCA_VERSION:-2.4.1}"
ORCA_INSTALL_DIR="/opt/orcaslicer"
ORCA_VERSION_FILE="${ORCA_INSTALL_DIR}/.version"

# Install OrcaSlicer only when the version on disk doesn't match the requested one.
# The install dir is expected to be a named volume so the download is cached across restarts.
if [ -f "$ORCA_VERSION_FILE" ] && [ "$(cat "$ORCA_VERSION_FILE")" = "$ORCA_VERSION" ]; then
    echo "[laminus] OrcaSlicer v${ORCA_VERSION} already installed, skipping download."
else
    echo "[laminus] Installing OrcaSlicer v${ORCA_VERSION}..."
    APPIMAGE_URL="https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v${ORCA_VERSION}/OrcaSlicer_Linux_AppImage_Ubuntu2404_V${ORCA_VERSION}.AppImage"

    # Clear contents but not the directory itself (it's a named-volume mount point)
    find "${ORCA_INSTALL_DIR}" -mindepth 1 -delete 2>/dev/null || true
    mkdir -p "$ORCA_INSTALL_DIR"

    curl -fsSL -o /tmp/OrcaSlicer.AppImage "$APPIMAGE_URL"
    chmod +x /tmp/OrcaSlicer.AppImage
    cd /tmp && ./OrcaSlicer.AppImage --appimage-extract
    mv /tmp/squashfs-root/* "$ORCA_INSTALL_DIR/"
    rm -rf /tmp/squashfs-root /tmp/OrcaSlicer.AppImage

    echo "$ORCA_VERSION" > "$ORCA_VERSION_FILE"
    echo "[laminus] OrcaSlicer v${ORCA_VERSION} installed."
fi

# Symlink may be absent if the volume was freshly mounted; always (re)create it.
ln -sf "${ORCA_INSTALL_DIR}/AppRun" /usr/local/bin/orcaslicer

exec "$@"
