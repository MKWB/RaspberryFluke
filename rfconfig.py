"""
rfconfig.py

RaspberryFluke configuration.

Edit this file to adjust behavior. All values have safe defaults
that work without any changes on most networks.

What this file does:
- Define which display type to use
- Configure the network interface
- Set SNMP community strings to try
- Tune timing values

What this file does NOT do:
- Initialize hardware
- Capture packets
- Parse data
- Draw anything on screen
"""

import os


# ============================================================
# ================= USER DISPLAY SELECTION ===================
# ============================================================
# Valid values:
#   "epaper"  = Waveshare 2.13" V3 e-paper display (default)
#   "lcd"     = Waveshare 1.44" LCD HAT display
#
# The environment variable RF_DISPLAY_TYPE overrides this value.
# ============================================================

DISPLAY_TYPE = os.environ.get("RF_DISPLAY_TYPE", "epaper")


# ============================================================
# -------------------- NETWORK SETTINGS ----------------------
# ============================================================

# Network interface to monitor.
NETWORK_INTERFACE = "eth0"

# How long to wait for any discovery method before giving up.
# 120 seconds allows CDP's 60-second cycle to be caught up to 2 times.
DISCOVERY_TIMEOUT = 120.0

# How long after the "Scanning..." screen appears before port data is
# allowed to replace it. This ensures the user sees the screen for at
# least this many seconds even if SNMP responds almost instantly.
# The e-paper draw time (~3s) is additional buffer on top of this.
RESULT_REVEAL_DELAY = 1.5

# How long to wait before displaying a partial result (one where switch_name
# is present but port is missing, or vice versa). Some switches such as
# FortiSwitch omit optional LLDP TLVs like the port VLAN ID. Showing partial
# data after this delay gives the user something useful rather than a blank
# screen while the device continues trying to get complete information.
# The background passive listener will upgrade the display to full data
# the moment a complete advertisement is received.
# Set to 0 to disable partial display entirely (wait for complete data only).
PARTIAL_DISPLAY_DELAY = 30.0

# How long to block waiting for a raw LLDP/CDP frame on each receive call.
# 2.0 seconds keeps the passive listener responsive to link-down events.
RAW_RECEIVE_TIMEOUT = 2.0


# ============================================================
# -------------------- SNMP SETTINGS -------------------------
# ============================================================

# User-defined SNMP community string.
# If set, this is tried FIRST before the built-in list below.
# Leave as empty string "" if you do not have a specific string.
SNMP_USER_COMMUNITY = ""

# Built-in community strings tried in order after SNMP_USER_COMMUNITY.
# Covers the vast majority of network environments without configuration.
SNMP_COMMUNITY_STRINGS = [
    "public",
    "cisco",
    "community",
    "private",
    "manager",
    "snmp",
    "monitor",
    "readonly",
]

# Seconds to wait for each individual SNMP response.
# 1.0 second is sufficient for most switches while keeping the race
# responsive — if passive wins, the SNMP thread stops within 1 second.
SNMP_TIMEOUT = 1.0

# Number of SNMP retries per query before moving to the next community string.
SNMP_RETRIES = 1

# Seconds to wait for a DHCP lease before falling back to ARP observation.
SNMP_DHCP_WAIT = 8.0

# Seconds to listen for ARP traffic when DHCP is unavailable.
SNMP_ARP_WAIT = 3.0


# ============================================================
# -------------------- DISPLAY SETTINGS ----------------------
# ============================================================

# Optional font path used by both display types.
DISPLAY_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


# ============================================================
# ------------------- E-PAPER SETTINGS -----------------------
# ============================================================

# Minimum seconds between normal e-paper refreshes.
EPAPER_MIN_REFRESH_INTERVAL = 10

# If True, the panel sleeps after each update (image stays visible).
EPAPER_AUTO_SLEEP = True

# Full refresh after this many partial refreshes to clear ghosting.
EPAPER_PARTIAL_REFRESH_LIMIT = 8


# ============================================================
# --------------------- LCD SETTINGS -------------------------
# ============================================================

LCD_ROTATE_180          = True
LCD_CLEAR_ON_START      = True
LCD_BACKGROUND_COLOR    = (0, 0, 0)
LCD_TEXT_COLOR          = (255, 255, 255)
LCD_BACKLIGHT_BRIGHTNESS = 100


# ============================================================
# ---------------------- LOG LEVEL ---------------------------
# ============================================================
# "WARNING" for normal appliance use (minimizes SD card writes).
# "DEBUG"   for troubleshooting.
#
# Override without editing this file:
#   RF_LOG_LEVEL=DEBUG python3 main.py
# ============================================================

LOG_LEVEL = os.environ.get("RF_LOG_LEVEL", "WARNING")


# ============================================================
# ------------------- HISTORY SETTINGS -----------------------
# ============================================================
# Controls whether and how port discovery results are saved to disk.
#
# PORT_HISTORY_MODE:
#   0 = Off (default) — nothing written, fully compatible with read-only
#   1 = Port History  — saves last PORT_HISTORY_LIMIT results to
#                       PORT_HISTORY_PATH/history.jsonl
#   2 = Debug Log     — writes verbose rotating log to
#                       PORT_HISTORY_PATH/debug.log
#
# Notes:
#   - Modes 1 and 2 require the writable /data partition to be mounted.
#     Run make_readonly.sh to set this up.
#   - Mode 0 is safe on a read-only filesystem with no writable partition.
#   - Each history entry is ~180 bytes. 50 entries = ~9KB total.
#   - The debug log rotates at 5MB and keeps 3 backup files.
#
# Read history log via SSH:
#   cat /data/raspberryfluke/history.jsonl
#   python3 -m json.tool /data/raspberryfluke/history.jsonl
# ============================================================

PORT_HISTORY_MODE  = 0
PORT_HISTORY_LIMIT = 50
PORT_HISTORY_PATH  = "/data/raspberryfluke"
