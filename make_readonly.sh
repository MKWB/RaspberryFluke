#!/usr/bin/env bash
# ============================================================
# make_readonly.sh
#
# One-time script to make the RaspberryFluke SD card read-only
# and create a writable /data area for port history logging.
#
# Run this ONCE after install.sh has completed successfully and
# the device is confirmed working. Use a stable power source
# (not PoE) when running this script.
#
# How /data works (no partition resizing needed):
#   A 256MB ext4 image file is created at /boot/firmware/rfdata.img
#   This file is mounted as a loop device at /data on every boot.
#   The /boot/firmware partition (vfat) has plenty of free space and
#   remains writable even when the root filesystem is read-only.
#   This avoids any need to resize or repartition the SD card.
#
# What this script does:
#   1. Creates /boot/firmware/rfdata.img (256MB ext4 image)
#   2. Updates /etc/fstab to:
#        - Mount root (/) read-only
#        - Mount rfdata.img as /data via loop device
#        - Redirect /tmp, /var/log, /var/tmp to tmpfs (RAM)
#   3. Adds 'ro' to /boot/firmware/cmdline.txt
#   4. Installs remount-rw and remount-ro helper scripts
#   5. Reboots
#
# After rebooting:
#   - Root filesystem is read-only and protected from corruption
#   - /data is writable for port history logging
#   - /tmp, /var/log, /var/tmp are in RAM (lost on power cut — safe)
#
# To update device code after read-only is enabled:
#   sudo remount-rw
#   cd /opt/raspberryfluke && sudo git pull
#   sudo remount-ro
#   sudo reboot
# ============================================================

set -euo pipefail

RED="\033[0;31m"
GREEN="\033[0;32m"
YELLOW="\033[1;33m"
RESET="\033[0m"

info()  { echo -e "${GREEN}[INFO]${RESET}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error() { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
die()   { error "$*"; exit 1; }

# ---- Configuration ----------------------------------------
BOOT_DIR="/boot/firmware"
DATA_IMG="${BOOT_DIR}/rfdata.img"
DATA_MOUNT="/data"
DATA_SIZE_MB=256
ROOT_PART="/dev/mmcblk0p2"
# -----------------------------------------------------------

# ---- 1. Validation ----------------------------------------
info "Starting RaspberryFluke read-only filesystem setup..."

if [[ $EUID -ne 0 ]]; then
    die "This script must be run as root. Use: sudo bash make_readonly.sh"
fi

if [[ ! -d "$BOOT_DIR" ]]; then
    die "Boot directory $BOOT_DIR not found."
fi

# Check free space on /boot/firmware.
BOOT_FREE_MB=$(df -m "$BOOT_DIR" | awk 'NR==2{print $4}')
if (( BOOT_FREE_MB < DATA_SIZE_MB + 10 )); then
    die "Not enough free space on $BOOT_DIR.
Need ${DATA_SIZE_MB}MB, only ${BOOT_FREE_MB}MB available."
fi

info "Boot partition has ${BOOT_FREE_MB}MB free — sufficient for ${DATA_SIZE_MB}MB image."

# Confirm the user knows what they're doing.
echo ""
warn "This script will make the root filesystem READ-ONLY."
warn "Run this only on a fully working RaspberryFluke installation."
warn "Use a stable power source — NOT PoE — while this script runs."
echo ""
read -r -p "Type YES to continue: " confirm
if [[ "$confirm" != "YES" ]]; then
    die "Aborted."
fi

# ---- 2. Create data image file ----------------------------
if [[ -f "$DATA_IMG" ]]; then
    info "Data image $DATA_IMG already exists — skipping creation."
else
    info "Creating ${DATA_SIZE_MB}MB ext4 data image at $DATA_IMG..."
    dd if=/dev/zero of="$DATA_IMG" bs=1M count="$DATA_SIZE_MB" status=progress
    mkfs.ext4 -L "rfdata" -F "$DATA_IMG"
    info "Data image created and formatted."
fi

# ---- 3. Mount image and populate /data --------------------
info "Mounting data image to verify and populate..."
mkdir -p "$DATA_MOUNT"
mount -o loop "$DATA_IMG" "$DATA_MOUNT"
mkdir -p "$DATA_MOUNT/raspberryfluke"
chmod 755 "$DATA_MOUNT/raspberryfluke"

# Copy any existing history data if present.
if [[ -d /data/raspberryfluke ]] && mountpoint -q "$DATA_MOUNT"; then
    # We just mounted fresh — copy from original /data if it had content.
    true
fi

umount "$DATA_MOUNT"
info "Data image verified."

# ---- 4. Update /etc/fstab ---------------------------------
info "Updating /etc/fstab..."
cp /etc/fstab /etc/fstab.bak
info "Original fstab backed up to /etc/fstab.bak"

# Get the PARTUUID of the root partition.
ROOT_PARTUUID=$(blkid -s PARTUUID -o value "$ROOT_PART")
if [[ -z "$ROOT_PARTUUID" ]]; then
    die "Could not read PARTUUID of $ROOT_PART."
fi
info "Root partition PARTUUID: $ROOT_PARTUUID"

# Preserve the boot partition entry exactly as it was.
BOOT_ENTRY=$(grep "/boot/firmware" /etc/fstab.bak || echo "")

cat > /etc/fstab << EOF
proc            /proc           proc    defaults          0       0

# Boot partition — always writable (hosts rfdata.img)
$BOOT_ENTRY

# Root filesystem — READ ONLY
# Protects the SD card from corruption on hard power cuts.
PARTUUID=$ROOT_PARTUUID  /  ext4  ro,noatime  0  1

# Writable data image mounted as loop device.
# Stores port history and debug logs persistently.
$BOOT_DIR/rfdata.img  /data  ext4  loop,rw,noatime  0  2

# RAM-based temporary filesystems.
# All runtime writes go here. Lost on power cut — safe by design.
tmpfs  /tmp      tmpfs  defaults,noatime,size=32m    0  0
tmpfs  /var/log  tmpfs  defaults,noatime,size=32m    0  0
tmpfs  /var/tmp  tmpfs  defaults,noatime,size=16m    0  0
EOF

info "fstab updated."

# ---- 5. Add 'ro' to cmdline.txt ---------------------------
CMDLINE_FILE=""
if [[ -f /boot/firmware/cmdline.txt ]]; then
    CMDLINE_FILE="/boot/firmware/cmdline.txt"
elif [[ -f /boot/cmdline.txt ]]; then
    CMDLINE_FILE="/boot/cmdline.txt"
fi

if [[ -n "$CMDLINE_FILE" ]]; then
    cp "$CMDLINE_FILE" "${CMDLINE_FILE}.bak"
    info "Original cmdline.txt backed up to ${CMDLINE_FILE}.bak"

    if ! grep -qw "ro" "$CMDLINE_FILE"; then
        sed -i 's/rootwait/ro rootwait/' "$CMDLINE_FILE"
        info "Read-only root (ro) added to $CMDLINE_FILE."
    else
        info "Read-only root already present in $CMDLINE_FILE."
    fi

    info "Updated cmdline.txt: $(cat "$CMDLINE_FILE")"
else
    warn "Could not locate cmdline.txt — add 'ro' manually before rootwait."
fi

# ---- 6. Install helper scripts ----------------------------
info "Installing remount helper scripts..."

cat > /usr/local/bin/remount-rw << 'SCRIPT'
#!/bin/bash
# Remount the root filesystem as read-write for code updates.
# Always run remount-ro and reboot when finished.
echo "Remounting root filesystem as read-write..."
mount -o remount,rw /
echo "Done. Root is now writable."
echo ""
echo "Make your changes, then run:"
echo "  sudo remount-ro"
echo "  sudo reboot"
SCRIPT

cat > /usr/local/bin/remount-ro << 'SCRIPT'
#!/bin/bash
# Remount the root filesystem as read-only after making changes.
echo "Remounting root filesystem as read-only..."
mount -o remount,ro /
echo "Done. Root is read-only again."
echo "Run 'sudo reboot' to apply all changes cleanly."
SCRIPT

chmod +x /usr/local/bin/remount-rw
chmod +x /usr/local/bin/remount-ro

info "Helper scripts installed:"
info "  sudo remount-rw  — make root writable for updates"
info "  sudo remount-ro  — make root read-only again"

# ---- Done -------------------------------------------------
echo ""
info "================================================"
info "  Read-only filesystem setup complete."
info "================================================"
echo ""
info "The device will reboot in 5 seconds."
info ""
info "After reboot:"
info "  - Root filesystem is READ ONLY (SD card protected)"
info "  - /data is WRITABLE (port history stored at /data/raspberryfluke)"
info "  - /tmp and /var/log are in RAM"
echo ""
info "To update device code in future:"
info "  sudo remount-rw"
info "  cd /opt/raspberryfluke && sudo git pull"
info "  sudo remount-ro"
info "  sudo reboot"
echo ""
info "To read port history:"
info "  cat /data/raspberryfluke/history.jsonl"
echo ""

sleep 5
reboot