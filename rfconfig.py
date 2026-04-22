"""
rfconfig.py

This file stores RaspberryFluke configuration values.

What this file does:
- Define which display type the program should use
- Store shared display settings
- Store e-paper-specific settings
- Store LCD-specific settings
- Provide a simple place for users to adjust behavior

What this file does NOT do:
- Initialize hardware
- Capture packets
- Parse LLDP or CDP data
- Draw anything on the screen
"""

import os


# ============================================================
# ================= USER DISPLAY SELECTION ===================
# ============================================================
# Change DISPLAY_TYPE to match the display you are using.
#
# Valid values:
#   "epaper"  = Waveshare 2.13" V3 e-paper display (recommended)
#   "lcd"     = Waveshare 1.44" LCD HAT display
#
# The environment variable RF_DISPLAY_TYPE overrides this value if set.
# This makes it easy to switch displays during testing without editing
# this file:
#   RF_DISPLAY_TYPE=lcd python3 main.py
# ============================================================

DISPLAY_TYPE = os.environ.get("RF_DISPLAY_TYPE", "epaper")


# ============================================================
# -------------------- SHARED SETTINGS -----------------------
# ============================================================

# Optional font path used by both display types.
# Leave this as-is unless you specifically want to use a different font.
DISPLAY_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Main network interface to monitor.
NETWORK_INTERFACE = "eth0"

# How long to block waiting for a raw LLDP or CDP frame on each receive call.
# The main loop calls receive_frame() repeatedly. Each call blocks for up to
# this many seconds. Lower values make the loop more responsive to carrier
# changes and shutdown signals. Higher values reduce CPU usage.
# 2.0 seconds is a good balance for an appliance with fast port changes.
RAW_RECEIVE_TIMEOUT = 2.0

# How long to wait before treating previously discovered neighbor data as
# stale and clearing the active result.
#
# Reasoning: CDP advertises every 60 seconds by default. Switches hold
# neighbor entries for 3x the advertisement interval (180 seconds) before
# removing them. Matching that hold-time here means RaspberryFluke and
# the switch agree on when a neighbor is considered gone.
DISCOVERY_TIMEOUT = 180

# Log level for the application.
# "WARNING" is recommended for normal appliance use to minimize SD card writes.
# Use "INFO" or "DEBUG" when troubleshooting.
#
# Valid values: "DEBUG", "INFO", "WARNING", "ERROR"
#
# The environment variable RF_LOG_LEVEL overrides this value if set:
# RF_LOG_LEVEL=DEBUG python3 main.py
LOG_LEVEL = os.environ.get("RF_LOG_LEVEL", "WARNING")


# ============================================================
# ------------------- E-PAPER SETTINGS -----------------------
# ============================================================

# Minimum number of seconds between normal e-paper refreshes.
# Helps reduce unnecessary screen updates when VLAN data is stable.
EPAPER_MIN_REFRESH_INTERVAL = 10

# If True, the e-paper panel goes back to sleep after each update.
# The image remains visible on screen while the panel is sleeping.
EPAPER_AUTO_SLEEP = True

# Number of partial refreshes allowed before a full refresh is forced.
# Partial refreshes are faster but accumulate ghosting artifacts over time.
# A full refresh clears ghosting. 8 is a safe value for the Waveshare 2.13" V3.
EPAPER_PARTIAL_REFRESH_LIMIT = 8


# ============================================================
# --------------------- LCD SETTINGS -------------------------
# ============================================================

# If True, rotate the LCD image by 180 degrees before display.
LCD_ROTATE_180 = True

# If True, clear the LCD during startup.
LCD_CLEAR_ON_START = True

# LCD background color in RGB format.
LCD_BACKGROUND_COLOR = (0, 0, 0)

# LCD text color in RGB format.
LCD_TEXT_COLOR = (255, 255, 255)

# LCD backlight brightness from 0 to 100.
LCD_BACKLIGHT_BRIGHTNESS = 100
