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


# ============================================================
# ================= USER DISPLAY SELECTION ===================
# ============================================================
# Change DISPLAY_TYPE to match the display you are using.
#
# Valid values:
#   "epaper"  = Waveshare 2.13" V3 e-paper display
#   "lcd"     = Waveshare 1.44" LCD HAT display
# ============================================================

DISPLAY_TYPE = "epaper"

# The e-paper display is the recommended display.


# ============================================================
# -------------------- SHARED SETTINGS -----------------------
# ============================================================

# Optional font path used by both display types.
# Leave this as-is unless you specifically want to use a different font.
DISPLAY_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Main network interface to monitor.
NETWORK_INTERFACE = "eth0"

# Maximum number of seconds to wait for lldpctl to return during normal use.
# Keep this fairly short so the appliance stays responsive.
CAPTURE_TIMEOUT = 2

# Fast poll interval used during startup before the first successful
# neighbor discovery. Lower values help reduce time-to-info.
STARTUP_POLL_INTERVAL = 1

# Normal poll interval used after the first successful neighbor discovery.
# This reduces unnecessary polling once useful data is already on screen.
STEADY_POLL_INTERVAL = 10

# How long the program waits before treating previously discovered neighbor
# data as stale and clearing the active result.
DISCOVERY_TIMEOUT = 180

# If True, show a dedicated waiting-for-link screen when Ethernet carrier is down.
WAITING_FOR_LINK_SCREEN = True

# If True, show a dedicated waiting-for-discovery screen when link is up
# but LLDP/CDP data has not been discovered yet.
WAITING_FOR_DISCOVERY_SCREEN = True

# Application mode:
#   "appliance" = minimal logging
#   "dev"       = more verbose logging
APP_MODE = "appliance"

# Leave blank to let APP_MODE decide automatically.
# Valid examples: "DEBUG", "INFO", "WARNING", "ERROR"
LOG_LEVEL = ""


# ============================================================
# ------------------- E-PAPER SETTINGS -----------------------
# ============================================================

# Minimum number of seconds between normal e-paper refreshes.
# This helps reduce unnecessary screen updates.
EPAPER_MIN_REFRESH_INTERVAL = 10

# If True, the e-paper panel goes back to sleep after each update.
# The image should remain visible on screen.
EPAPER_AUTO_SLEEP = True


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