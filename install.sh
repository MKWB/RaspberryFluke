#!/usr/bin/env bash
# ============================================================
# install.sh
#
# Automated installer for RaspberryFluke.
#
# Usage:
#   Clone the repository first, then run this script:
#
#   sudo git clone https://github.com/MKWB/RaspberryFluke.git /opt/raspberryfluke
#   cd /opt/raspberryfluke
#   sudo bash install.sh
#
# The script is idempotent — safe to run again after updates.
#
# What this script does:
#   1. Checks that it is running as root
#   2. Installs required system packages (including the font package)
#   3. Enables SPI for the e-paper display
#   4. Clones the Waveshare e-Paper library and copies the driver
#      into the project directory
#   5. Sets correct file permissions
#   6. Installs and enables the systemd service
# ============================================================

set -euo pipefail

# ---- Configuration ----------------------------------------
INSTALL_DIR="/opt/raspberryfluke"
WAVESHARE_REPO="https://github.com/waveshare/e-Paper.git"
WAVESHARE_CLONE_DIR="/tmp/waveshare-epaper"
WAVESHARE_LIB_SRC="$WAVESHARE_CLONE_DIR/RaspberryPi_JetsonNano/python/lib/waveshare_epd"
WAVESHARE_LIB_DST="$INSTALL_DIR/waveshare_epd"
SERVICE_NAME="raspberryfluke"
# -----------------------------------------------------------

RED="\033[0;31m"
GREEN="\033[0;32m"
YELLOW="\033[1;33m"
RESET="\033[0m"

info()  { echo -e "${GREEN}[INFO]${RESET}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error() { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
die()   { error "$*"; exit 1; }

# ---- 1. Root check ----------------------------------------
if [[ $EUID -ne 0 ]]; then
    die "This script must be run as root.
Run it as: sudo bash install.sh"
fi

# ---- Confirm install directory ----------------------------
if [[ ! -f "$INSTALL_DIR/main.py" ]]; then
    die "RaspberryFluke files not found in $INSTALL_DIR.
Clone the repository first:
  sudo git clone https://github.com/MKWB/RaspberryFluke.git $INSTALL_DIR"
fi

info "Starting RaspberryFluke installation from $INSTALL_DIR"

# ---- 2. System packages -----------------------------------
info "Installing system packages..."

apt-get update -qq
apt-get install -y \
    git \
    python3 \
    python3-pip \
    python3-pil \
    python3-lgpio \
    python3-rpi.gpio \
    fonts-dejavu-core

info "System packages installed."

# ---- 3. Enable SPI ----------------------------------------
info "Checking SPI status..."

# Raspberry Pi OS stores boot config in one of two locations
# depending on OS version.
BOOT_CONFIG=""
if [[ -f /boot/firmware/config.txt ]]; then
    BOOT_CONFIG="/boot/firmware/config.txt"
elif [[ -f /boot/config.txt ]]; then
    BOOT_CONFIG="/boot/config.txt"
fi

SPI_ENABLED=false

if [[ -n "$BOOT_CONFIG" ]]; then
    if grep -q "^dtparam=spi=on" "$BOOT_CONFIG" 2>/dev/null; then
        SPI_ENABLED=true
    fi
fi

if $SPI_ENABLED; then
    info "SPI is already enabled."
else
    if [[ -n "$BOOT_CONFIG" ]]; then
        echo "dtparam=spi=on" >> "$BOOT_CONFIG"
        info "SPI enabled in $BOOT_CONFIG."
        warn "A reboot is required for SPI to take effect."
        warn "After this script completes, run: sudo reboot"
    else
        warn "Could not locate boot config.txt."
        warn "Enable SPI manually: sudo raspi-config"
        warn "  -> Interface Options -> SPI -> Enable"
        warn "Then reboot and re-run this script."
    fi
fi

# ---- 4. Waveshare e-Paper library -------------------------
info "Setting up Waveshare e-Paper library..."

# Clone to a temporary location so we do not pollute the install directory.
if [[ -d "$WAVESHARE_CLONE_DIR" ]]; then
    info "Updating existing Waveshare repository..."
    git -C "$WAVESHARE_CLONE_DIR" pull origin master 2>/dev/null || \
        warn "Could not update Waveshare repo. Using existing copy."
else
    info "Cloning Waveshare e-Paper repository..."
    git clone --depth 1 "$WAVESHARE_REPO" "$WAVESHARE_CLONE_DIR"
fi

if [[ ! -d "$WAVESHARE_LIB_SRC" ]]; then
    die "Waveshare driver not found at expected path:
$WAVESHARE_LIB_SRC

The Waveshare repository layout may have changed.
Check: $WAVESHARE_CLONE_DIR"
fi

rm -rf "$WAVESHARE_LIB_DST"
cp -r  "$WAVESHARE_LIB_SRC" "$WAVESHARE_LIB_DST"

info "Waveshare epd library installed to $WAVESHARE_LIB_DST"

# ---- 5. File permissions ----------------------------------
info "Setting file permissions..."

chown -R root:root "$INSTALL_DIR"
chmod 755 "$INSTALL_DIR/main.py"

info "Permissions set."

# ---- 6. Systemd service -----------------------------------
info "Installing systemd service..."

SERVICE_SRC="$INSTALL_DIR/raspberryfluke.service"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ ! -f "$SERVICE_SRC" ]]; then
    die "Service file not found at $SERVICE_SRC.
Check that the repository was cloned correctly."
fi

cp "$SERVICE_SRC" "$SERVICE_DST"
systemctl daemon-reload
systemctl enable  "${SERVICE_NAME}.service"
systemctl restart "${SERVICE_NAME}.service"

info "Service installed and started."

# ---- Done -------------------------------------------------
echo ""
info "================================================"
info "  RaspberryFluke installation complete."
info "================================================"
echo ""
info "Check service status:"
info "  sudo systemctl status ${SERVICE_NAME}.service"
echo ""
info "View live logs:"
info "  sudo journalctl -u ${SERVICE_NAME}.service -f"
echo ""

if ! $SPI_ENABLED; then
    warn "IMPORTANT: Reboot required to activate SPI for the e-paper display."
    warn "Run: sudo reboot"
fi
