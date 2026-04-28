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
#   1.  Checks that it is running as root
#   2.  Installs required system packages (including snmp tools)
#   3.  Applies boot time optimizations (BT, WiFi, HDMI, silent boot,
#       serial console, cloud-init, NetworkManager → systemd-networkd)
#   4.  Enables SPI for the e-paper display
#   5.  Installs udev rule for instant eth0 activation on link-up
#   6.  Creates systemd-networkd config for eth0
#   7.  Configures systemd journal persistence
#   8.  Clones the Waveshare e-Paper library and copies the driver
#   9.  Sets correct file permissions
#   10. Installs and enables the systemd service
# ============================================================

set -euo pipefail

# ---- Configuration ----------------------------------------
INSTALL_DIR="/opt/raspberryfluke"
WAVESHARE_REPO="https://github.com/waveshare/e-Paper.git"
WAVESHARE_CLONE_DIR="/opt/waveshare-epaper"
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
    fonts-dejavu-core \
    snmp

info "System packages installed."

# ---- 4. Boot time optimizations ---------------------------
info "Applying boot time optimizations..."

# Locate the boot config file (path varies by OS version).
BOOT_CONFIG=""
if [[ -f /boot/firmware/config.txt ]]; then
    BOOT_CONFIG="/boot/firmware/config.txt"
elif [[ -f /boot/config.txt ]]; then
    BOOT_CONFIG="/boot/config.txt"
fi

if [[ -n "$BOOT_CONFIG" ]]; then

    # -- Disable Bluetooth --
    # The Pi Zero 2W has Bluetooth built in. We have no use for it.
    # Disabling it frees the UART and saves ~3-5 seconds of boot time.
    if ! grep -q "dtoverlay=disable-bt" "$BOOT_CONFIG" 2>/dev/null; then
        echo "dtoverlay=disable-bt" >> "$BOOT_CONFIG"
        info "Bluetooth disabled in $BOOT_CONFIG."
    else
        info "Bluetooth already disabled."
    fi

    # Stop and mask the Bluetooth services so they never start.
    systemctl disable hciuart   2>/dev/null || true
    systemctl disable bluetooth 2>/dev/null || true
    systemctl mask    hciuart   2>/dev/null || true
    systemctl mask    bluetooth 2>/dev/null || true

    # -- Disable WiFi --
    # RaspberryFluke uses the wired Ethernet port exclusively.
    # Disabling WiFi saves ~2-3 seconds of boot time.
    if ! grep -q "dtoverlay=disable-wifi" "$BOOT_CONFIG" 2>/dev/null; then
        echo "dtoverlay=disable-wifi" >> "$BOOT_CONFIG"
        info "WiFi disabled in $BOOT_CONFIG."
    else
        info "WiFi already disabled."
    fi

    # -- Disable HDMI --
    # The device runs headless. Disabling HDMI saves ~1-2 seconds.
    if ! grep -q "hdmi_blanking=2" "$BOOT_CONFIG" 2>/dev/null; then
        echo "hdmi_blanking=2" >> "$BOOT_CONFIG"
        info "HDMI blanking enabled in $BOOT_CONFIG."
    else
        info "HDMI blanking already set."
    fi

    # -- Disable boot splash and remove boot delay --
    # Eliminates the rainbow splash screen and the artificial 1-second delay.
    if ! grep -q "disable_splash=1" "$BOOT_CONFIG" 2>/dev/null; then
        echo "disable_splash=1" >> "$BOOT_CONFIG"
        info "Boot splash disabled in $BOOT_CONFIG."
    else
        info "Boot splash already disabled."
    fi

    if ! grep -q "boot_delay=0" "$BOOT_CONFIG" 2>/dev/null; then
        echo "boot_delay=0" >> "$BOOT_CONFIG"
        info "Boot delay disabled in $BOOT_CONFIG."
    else
        info "Boot delay already set to 0."
    fi

else
    warn "Could not locate boot config.txt — skipping hardware optimizations."
fi

# -- Silent boot: suppress kernel messages on console --
# Redirects boot output to tty3 (unused) and sets loglevel=0 to silence
# kernel messages. Saves 1-2 seconds of visible boot spam and makes the
# transition from boot to the RaspberryFluke display cleaner.
CMDLINE_FILE=""
if [[ -f /boot/firmware/cmdline.txt ]]; then
    CMDLINE_FILE="/boot/firmware/cmdline.txt"
elif [[ -f /boot/cmdline.txt ]]; then
    CMDLINE_FILE="/boot/cmdline.txt"
fi

if [[ -n "$CMDLINE_FILE" ]]; then
    # Replace console=tty1 with console=tty3 if present.
    if grep -q "console=tty1" "$CMDLINE_FILE" 2>/dev/null; then
        sed -i 's/console=tty1/console=tty3/' "$CMDLINE_FILE"
        info "Console redirected to tty3 in $CMDLINE_FILE."
    fi

    # Add quiet and loglevel=0 if not already present.
    if ! grep -q "\bquiet\b" "$CMDLINE_FILE" 2>/dev/null; then
        sed -i 's/$/ quiet loglevel=0/' "$CMDLINE_FILE"
        info "Silent boot (quiet loglevel=0) added to $CMDLINE_FILE."
    else
        info "Silent boot already configured in $CMDLINE_FILE."
    fi
else
    warn "Could not locate cmdline.txt — skipping silent boot configuration."
fi

# -- Mask unused system services --
# These services have no role in a headless appliance and add boot latency.
MASK_SERVICES=(
    "triggerhappy.service"
    "apt-daily.timer"
    "apt-daily-upgrade.timer"
    "man-db.timer"
    "NetworkManager.service"
    "NetworkManager-wait-online.service"
    "cloud-init-main.service"
    "cloud-init-local.service"
    "cloud-init-network.service"
    "cloud-config.service"
    "cloud-final.service"
)
for svc in "${MASK_SERVICES[@]}"; do
    systemctl mask "$svc" 2>/dev/null || true
done

# Disable cloud-init via its own flag file — belt and suspenders approach.
touch /etc/cloud/cloud-init.disabled 2>/dev/null || true

# Switch from NetworkManager to systemd-networkd.
# systemd-networkd is built into systemd, requires no extra packages,
# and starts in under 200ms vs NetworkManager's 10-14 seconds.
systemctl disable NetworkManager 2>/dev/null || true
systemctl enable  systemd-networkd 2>/dev/null || true

# Create network config for eth0 if it doesn't exist.
NETWORKD_CONF="/etc/systemd/network/10-eth0.network"
if [[ ! -f "$NETWORKD_CONF" ]]; then
    mkdir -p /etc/systemd/network
    cat > "$NETWORKD_CONF" << 'EOF'
[Match]
Name=eth0

[Network]
DHCP=yes

[DHCP]
RouteMetric=100
EOF
    info "systemd-networkd config created at $NETWORKD_CONF."
else
    info "systemd-networkd config already exists."
fi

# Remove serial console from cmdline.txt — it adds latency on boot
# since we disabled Bluetooth which shared the UART.
if [[ -n "$CMDLINE_FILE" ]]; then
    if grep -q "console=serial0" "$CMDLINE_FILE" 2>/dev/null; then
        sed -i 's/console=serial0,[0-9]* //' "$CMDLINE_FILE"
        info "Serial console removed from $CMDLINE_FILE."
    else
        info "Serial console already removed from $CMDLINE_FILE."
    fi
fi

info "Unused services masked. NetworkManager replaced with systemd-networkd."

info "Boot optimizations applied. Reboot required for hardware changes to take effect."

# ---- 5. Enable SPI ----------------------------------------
info "Checking SPI status..."

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
        SPI_ENABLED=false   # still needs reboot
    else
        warn "Could not locate boot config.txt."
        warn "Enable SPI manually: sudo raspi-config"
        warn "  -> Interface Options -> SPI -> Enable"
        warn "Then reboot and re-run this script."
    fi
fi

# ---- 6. udev rule for instant eth0 activation --------------------
info "Installing udev rule for eth0 fast activation..."

UDEV_RULE_FILE="/etc/udev/rules.d/99-raspberryfluke-eth0.rules"

cat > "$UDEV_RULE_FILE" << 'EOF'
# RaspberryFluke: Bring eth0 up in promiscuous mode the instant the kernel
# detects a carrier signal. This fires before dhcpcd gets a chance to act,
# ensuring the raw capture socket can be opened immediately on link-up.
# Promiscuous mode is required to receive LLDP and CDP multicast frames.
# dhcpcd monitors interface state changes independently and will still fire
# its DHCP request normally after this rule runs.
ACTION=="change", SUBSYSTEM=="net", KERNEL=="eth0", \
    ATTR{carrier}=="1", \
    RUN+="/sbin/ip link set eth0 up promisc on"
EOF

udevadm control --reload-rules 2>/dev/null || true
info "udev rule installed at $UDEV_RULE_FILE."

# ---- 7. Journal persistence -------------------------------
info "Configuring systemd journal persistence..."

# Create the persistent journal directory.
mkdir -p /var/log/journal

# Enable persistent storage and reduce sync interval so logs survive
# hard power cuts (which happen every time PoE is unplugged).
JOURNAL_CONF="/etc/systemd/journald.conf.d/raspberryfluke.conf"
mkdir -p "$(dirname "$JOURNAL_CONF")"

cat > "$JOURNAL_CONF" << 'EOF'
# RaspberryFluke journal configuration.
# Persistent storage ensures logs survive hard power cuts from PoE.
# SyncIntervalSec=10s means at most 10 seconds of logs are lost on power cut.
[Journal]
Storage=persistent
SyncIntervalSec=10s
EOF

systemctl restart systemd-journald
info "Journal persistence configured."

# ---- 8. Waveshare e-Paper library -------------------------
info "Setting up Waveshare e-Paper library..."

if [[ -d "$WAVESHARE_CLONE_DIR" ]]; then
    info "Updating existing Waveshare repository..."
    git -C "$WAVESHARE_CLONE_DIR" pull origin master 2>/dev/null || \
        warn "Could not update Waveshare repo. Using existing copy."
else
    info "Cloning Waveshare e-Paper repository (this may take a moment)..."
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

# Remove the full Waveshare clone — we only needed the epd driver folder.
info "Cleaning up Waveshare repository clone..."
rm -rf "$WAVESHARE_CLONE_DIR"

# ---- 9. File permissions ----------------------------------
info "Setting file permissions..."

chown -R root:root "$INSTALL_DIR"
chmod 755 "$INSTALL_DIR/main.py"

info "Permissions set."

# ---- 10. Systemd service -----------------------------------
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
warn "IMPORTANT: A reboot is required for the following changes to take effect:"
warn "  - Bluetooth disabled"
warn "  - WiFi disabled"
warn "  - HDMI blanking"
warn "  - Silent boot (quiet loglevel=0)"
warn "  - Boot splash and delay disabled"
warn "  - Serial console removed"
warn "  - SPI enabled (if newly configured)"
warn "  - udev rule for eth0 fast activation"
warn "  - NetworkManager replaced with systemd-networkd"
warn ""
warn "Run: sudo reboot"