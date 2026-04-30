#!/usr/bin/env bash
# ============================================================
# make_readonly.sh
#
# One-time script to make the RaspberryFluke SD card read-only
# and create a small writable /data partition for port history.
#
# Run this ONCE after install.sh has completed successfully and
# the device is confirmed working. Use a stable power source
# (not PoE) when running this script — it modifies the partition
# table and boot configuration.
#
# What this script does:
#   1. Validates the environment (root, enough free space, etc.)
#   2. Creates a new 256MB partition (/dev/mmcblk0p3) for /data
#   3. Formats it as ext4 with noatime
#   4. Updates /etc/fstab to:
#        - Mount root (/) read-only
#        - Mount /data read-write
#        - Redirect /tmp, /var/log, /var/tmp, /run to tmpfs (RAM)
#   5. Adds 'ro' to /boot/firmware/cmdline.txt
#   6. Installs remount-rw and remount-ro helper scripts
#   7. Reboots
#
# After rebooting:
#   - The SD card root filesystem is read-only and protected
#   - /data is writable for port history logging
#   - All other runtime writes go to RAM and are lost on power cut
#
# To update the device code after read-only is enabled:
#   sudo remount-rw
#   cd /opt/raspberryfluke && sudo git pull
#   sudo remount-ro
#   sudo reboot
#
# To disable read-only permanently (not recommended):
#   sudo remount-rw
#   # Edit /etc/fstab and /boot/firmware/cmdline.txt manually
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
SD_DEVICE="/dev/mmcblk0"
ROOT_PART="${SD_DEVICE}p2"
DATA_PART="${SD_DEVICE}p3"
DATA_MOUNT="/data"
DATA_SIZE_MB=256
DATA_SIZE_MB_PLUS_ONE=$(( DATA_SIZE_MB + 1 ))
# -----------------------------------------------------------

# ---- 1. Validation ----------------------------------------
info "Starting RaspberryFluke read-only filesystem setup..."

if [[ $EUID -ne 0 ]]; then
    die "This script must be run as root. Use: sudo bash make_readonly.sh"
fi

if [[ ! -b "$SD_DEVICE" ]]; then
    die "SD card device $SD_DEVICE not found.
This script is designed for the Raspberry Pi Zero 2W.
If your device uses a different storage path, edit SD_DEVICE in this script."
fi

if [[ -b "$DATA_PART" ]]; then
    warn "Partition $DATA_PART already exists."
    warn "If this is a re-run, the partition setup will be skipped."
    warn "Continuing with fstab and cmdline configuration only."
    DATA_PART_EXISTS=true
else
    DATA_PART_EXISTS=false
fi

# Confirm the user knows what they're doing.
echo ""
warn "This script will modify your SD card partition table and boot configuration."
warn "Run this only on a fully working RaspberryFluke installation."
warn "Use a stable power source — NOT PoE — while this script runs."
echo ""
read -r -p "Type YES to continue: " confirm
if [[ "$confirm" != "YES" ]]; then
    die "Aborted."
fi

# ---- 2. Create /data partition ----------------------------
if [[ "$DATA_PART_EXISTS" == false ]]; then
    info "Creating ${DATA_SIZE_MB}MB data partition on $SD_DEVICE..."

    # Use negative offsets to place the partition at the end of the disk.
    # This is more reliable than calculating the start position from the
    # end of the last partition, which can vary based on parted output format.
    # -257MB to -1MB creates a ~256MB partition at the end of the SD card.
    parted "$SD_DEVICE" --script unit MB mkpart primary ext4 -- "-${DATA_SIZE_MB_PLUS_ONE}" "-1"

    # Wait for the kernel to see the new partition.
    partprobe "$SD_DEVICE" 2>/dev/null || true
    sleep 2

    if [[ ! -b "$DATA_PART" ]]; then
        die "Partition $DATA_PART was not created. Check that the SD card has free space."
    fi

    info "Partition $DATA_PART created."
fi

# ---- 3. Format /data partition ----------------------------
info "Formatting $DATA_PART as ext4..."
mkfs.ext4 -L "rfdata" -F "$DATA_PART"
info "Partition formatted."

# ---- 4. Mount and populate /data --------------------------
info "Mounting $DATA_PART at $DATA_MOUNT..."
mkdir -p "$DATA_MOUNT"
mount "$DATA_PART" "$DATA_MOUNT"
mkdir -p "$DATA_MOUNT/raspberryfluke"
chmod 755 "$DATA_MOUNT/raspberryfluke"

# Copy any existing history data from the old location if present.
if [[ -d /data/raspberryfluke ]] && [[ "$(ls -A /data/raspberryfluke 2>/dev/null)" ]]; then
    info "Copying existing history data to new partition..."
    cp -r /data/raspberryfluke/. "$DATA_MOUNT/raspberryfluke/" 2>/dev/null || true
fi

umount "$DATA_MOUNT"
info "/data partition ready."

# ---- 5. Update /etc/fstab ---------------------------------
info "Updating /etc/fstab..."

# Back up existing fstab.
cp /etc/fstab /etc/fstab.bak
info "Original fstab backed up to /etc/fstab.bak"

# Get the PARTUUID of the data partition.
DATA_PARTUUID=$(blkid -s PARTUUID -o value "$DATA_PART")
if [[ -z "$DATA_PARTUUID" ]]; then
    die "Could not read PARTUUID of $DATA_PART."
fi
info "Data partition PARTUUID: $DATA_PARTUUID"

# Get the PARTUUID of the root partition.
ROOT_PARTUUID=$(blkid -s PARTUUID -o value "$ROOT_PART")
if [[ -z "$ROOT_PARTUUID" ]]; then
    die "Could not read PARTUUID of $ROOT_PART."
fi
info "Root partition PARTUUID: $ROOT_PARTUUID"

# Get the existing boot partition entry to preserve it exactly.
BOOT_ENTRY=$(grep "/boot/firmware" /etc/fstab.bak || echo "")

cat > /etc/fstab << EOF
proc            /proc           proc    defaults          0       0

# Boot partition — preserved from original fstab
$BOOT_ENTRY

# Root filesystem — READ ONLY
# This protects the SD card from corruption on hard power cuts.
PARTUUID=$ROOT_PARTUUID  /  ext4  ro,noatime  0  1

# Writable data partition for port history and debug logs.
PARTUUID=$DATA_PARTUUID  /data  ext4  rw,noatime  0  2

# RAM-based temporary filesystems.
# These redirect runtime writes away from the SD card.
# Contents are lost on power cut — this is intentional and safe.
tmpfs  /tmp      tmpfs  defaults,noatime,size=32m  0  0
tmpfs  /var/log  tmpfs  defaults,noatime,size=32m  0  0
tmpfs  /var/tmp  tmpfs  defaults,noatime,size=16m  0  0
tmpfs  /run      tmpfs  defaults,noatime,mode=0755,size=16m  0  0
EOF

info "fstab updated."

# ---- 6. Add 'ro' to cmdline.txt ---------------------------
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
        # Insert 'ro' before 'rootwait' so the kernel mounts root read-only.
        sed -i 's/rootwait/ro rootwait/' "$CMDLINE_FILE"
        info "Read-only root (ro) added to $CMDLINE_FILE."
    else
        info "Read-only root already present in $CMDLINE_FILE."
    fi

    info "Updated cmdline.txt: $(cat "$CMDLINE_FILE")"
else
    warn "Could not locate cmdline.txt — add 'ro' manually before rootwait."
fi

# ---- 7. Install helper scripts ----------------------------
info "Installing remount helper scripts..."

cat > /usr/local/bin/remount-rw << 'SCRIPT'
#!/bin/bash
# Remount the root filesystem as read-write for code updates.
# Always run remount-ro when finished, then reboot.
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
info "  remount-rw — make root writable for updates"
info "  remount-ro — make root read-only again"

# ---- Done -------------------------------------------------
echo ""
info "================================================"
info "  Read-only filesystem setup complete."
info "================================================"
echo ""
info "The device will reboot in 5 seconds."
info "After reboot:"
info "  - Root filesystem is READ ONLY (SD card protected)"
info "  - /data is WRITABLE (port history stored here)"
info "  - /tmp, /var/log, /var/tmp, /run are in RAM"
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