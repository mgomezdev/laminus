FROM ubuntu:24.04

# Avoid prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Update and install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    apt-transport-https \
    ca-certificates \
    curl \
    wget \
    gnupg \
    unzip \
    # GUI and display dependencies (X11, GL, GTK, Webkit)
    xvfb \
    dbus-x11 \
    libglu1-mesa \
    libgl1-mesa-dri \
    libgtk-3-0 \
    libwebkit2gtk-4.1-0 \
    libsecret-1-0 \
    libcurl4 \
    libdbus-1-3 \
    libgstreamer1.0-0 \
    libgstreamer-plugins-base1.0-0 \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    libxkbcommon-x11-0 \
    libxcb-icccm4 \
    libxcb-image0 \
    libxcb-keysyms1 \
    libxcb-render-util0 \
    libxcb-xinerama0 \
    libepoxy0 \
    libtiff6 \
    libbz2-1.0 \
    libmspack0 \
    # Python tools
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Download and extract OrcaSlicer AppImage (Ubuntu 24.04 Build)
# Extracting bypasses the need for FUSE inside the Docker container
RUN mkdir -p /opt/orcaslicer \
    && curl -L -o /opt/OrcaSlicer.AppImage https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v2.4.1/OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.4.1.AppImage \
    && chmod +x /opt/OrcaSlicer.AppImage \
    && cd /opt \
    && ./OrcaSlicer.AppImage --appimage-extract \
    && mv /opt/squashfs-root/* /opt/orcaslicer/ \
    && rm -rf /opt/squashfs-root /opt/OrcaSlicer.AppImage

# Create symlink for OrcaSlicer binary
RUN ln -s /opt/orcaslicer/AppRun /usr/local/bin/orcaslicer

# Create configuration directory
RUN mkdir -p /config

# Install Python requirements (using --break-system-packages for Ubuntu 24.04 system python pip)
COPY requirements.txt /requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /requirements.txt

# Set up working directory for the application
WORKDIR /workspace
COPY app /workspace/app

# Set environment variables for OrcaSlicer execution
ENV DISPLAY=:99
ENV PYTHONUNBUFFERED=1

# Expose port for FastAPI server
EXPOSE 5000

# Start FastAPI application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5000"]
