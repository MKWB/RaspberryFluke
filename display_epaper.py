"""
display_epaper.py

This file only handles the Waveshare 2.13" V3 e-paper display.

What this file does:
- Start the e-paper display
- Draw a fixed header and 5 body lines onto an image
- Show that image on the screen using partial or full refresh as appropriate
- Limit how often the screen refreshes
- Force a refresh when VLAN or VOICE changes
- Manage ghosting by forcing a full refresh every N partial refreshes
- Put the display to sleep when appropriate

What this file does NOT do:
- Read config files
- Capture packets
- Parse LLDP or CDP data
- Store broader application state
- Decide what lines to show (that is main.py's job)
"""

import logging
import threading
import time

from PIL import Image, ImageDraw, ImageFont
from waveshare_epd import epd2in13_V3


log = logging.getLogger(__name__)


class EPaperDisplay:
    # Screen size in pixels for the Waveshare 2.13" V3 display.
    DISPLAY_WIDTH  = 250
    DISPLAY_HEIGHT = 122

    # Text position on the screen.
    LEFT_MARGIN = 10
    TOP_MARGIN  = 4

    # Font settings for the 5 body lines.
    BASE_FONT_SIZE = 16
    MIN_FONT_SIZE  = 10
    LINE_SPACING   = 2

    # Fixed header drawn at the top of every screen.
    TITLE_TEXT       = "RaspberryFluke"
    TITLE_FONT_SIZE  = 16
    TITLE_UNDERLINE_GAP = 1
    TITLE_BODY_GAP      = 2

    # Default font file.
    DEFAULT_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

    # Number of partial refreshes before a full refresh is forced to clear ghosting.
    # Starting the counter at this limit ensures the very first render is always
    # a full refresh, giving a clean slate on boot.
    PARTIAL_REFRESH_LIMIT = 8

    def __init__(
        self,
        font_path=None,
        min_refresh_interval=10,
        auto_sleep=True,
        startup_mode=True,
        partial_refresh_limit=None,
    ):
        """
        Set up the display object.

        font_path:
            Optional path to a .ttf font file.

        min_refresh_interval:
            Minimum number of seconds between normal display refreshes.

        auto_sleep:
            If True, the panel goes back to sleep after normal updates.

        startup_mode:
            If True, the display stays awake between updates during early boot
            so the panel does not repeatedly init/sleep before the first
            real result arrives.

        partial_refresh_limit:
            Override for PARTIAL_REFRESH_LIMIT. Useful for testing.
        """
        self.font_path            = font_path or self.DEFAULT_FONT_PATH
        self.min_refresh_interval = min_refresh_interval
        self.auto_sleep           = auto_sleep
        self.startup_mode         = startup_mode

        # Allow the limit to be overridden at construction time.
        self._partial_refresh_limit = (
            int(partial_refresh_limit)
            if partial_refresh_limit is not None
            else self.PARTIAL_REFRESH_LIMIT
        )

        # Create the Waveshare display object.
        self.epd = epd2in13_V3.EPD()

        # RLock allows one method to safely call another method that also uses
        # the same lock, which happens during sleep/wake sequences.
        self.lock = threading.RLock()

        # Track display state.
        self.initialized = False
        self.sleeping    = False

        # Start at the limit so the first render always performs a full refresh.
        self.partial_refresh_count = self._partial_refresh_limit

        # Tracks whether displayPartBaseImage has been called at least once.
        # Partial refresh requires both frame buffers to be seeded first.
        # Until this is True, every refresh is forced to be a full refresh.
        self._partial_base_ready = False

        # Save the last 5 normalized body lines shown on screen.
        self.last_lines = None

        # Save the last refresh time.
        self.last_refresh_time = 0.0

        # Preload the body font sizes once so we do not repeatedly read from disk.
        self.font_cache = self._build_font_cache()

        # Load the fixed title font once.
        self.title_font = self._load_title_font()

    # ------------------------------------------------------------------ #
    #  Public interface                                                    #
    # ------------------------------------------------------------------ #

    def initialize(self, clear_on_start=False):
        """
        Start the e-paper display.

        clear_on_start:
            If True, clear the screen to white when starting up.
            Leave False for faster appliance behavior so we can jump
            directly to the boot screen without an extra blank-screen flash.
        """
        with self.lock:
            if self.initialized and not self.sleeping:
                return

            self.epd.init()
            self.initialized = True
            self.sleeping    = False

            # After any hardware init, the next display call must be a full
            # refresh to ensure a clean starting state.
            self.partial_refresh_count = self._partial_refresh_limit

            if clear_on_start:
                self.clear()

    def clear(self, sleep_after=True):
        """
        Clear the screen to white.

        sleep_after:
            If True, put the panel back to sleep after clearing.
        """
        with self.lock:
            self._ensure_awake()
            self.epd.Clear(0xFF)
            self.last_lines            = ["", "", "", "", ""]
            self.last_refresh_time     = time.monotonic()
            self.partial_refresh_count = self._partial_refresh_limit

            if sleep_after and self.auto_sleep and not self.startup_mode:
                self.sleep()

    def sleep(self):
        """
        Put the e-paper display into sleep mode.

        The image remains visible on screen after sleep.
        """
        with self.lock:
            if not self.initialized or self.sleeping:
                return
            self.epd.sleep()
            self.sleeping = True

    def shutdown(self, clear_before_sleep=False):
        """
        Shut down the display safely.

        clear_before_sleep:
            If True, blank the screen before sleeping.
            Leave False so the last result stays visible on screen.
        """
        with self.lock:
            if not self.initialized:
                return

            if clear_before_sleep:
                try:
                    self._ensure_awake()
                    self.epd.Clear(0xFF)
                    self.last_lines        = ["", "", "", "", ""]
                    self.last_refresh_time = time.monotonic()
                except Exception:
                    pass

            try:
                self.epd.sleep()
            except Exception:
                pass

            self.sleeping    = True
            self.initialized = False

    def set_startup_mode(self, enabled):
        """
        Enable or disable startup mode.

        Startup mode keeps the display awake between early updates to avoid
        constant init/sleep churn before the first real result arrives.

        Typical use:
        - startup_mode=True  while booting and waiting for first discovery
        - startup_mode=False after the first real neighbor result is shown
        """
        with self.lock:
            self.startup_mode = bool(enabled)

    def show_lines(self, lines, force=False, protocol=""):
        """
        Show 5 body lines on the e-paper display.

        The fixed "RaspberryFluke" header and underline are always drawn
        by this method. The caller does not need to include a header in lines.

        lines:
            A list of up to 5 body text strings to show below the header.
            Missing lines are filled with blank strings.

        force:
            If True, refresh the display regardless of whether content changed
            or the refresh timer has elapsed.

        Returns:
            True if the display was refreshed.
            False if the update was skipped.
        """
        with self.lock:
            normalized_lines = self._normalize_lines(lines)
            protocol_label   = str(protocol).upper().strip()[:4] if protocol else ""

            # Skip if body text and protocol are identical to what is on screen.
            if not force and normalized_lines == self.last_lines and protocol_label == getattr(self, "_last_protocol", ""):
                return False

            # VLAN or VOICE changes always trigger an immediate refresh,
            # bypassing the normal minimum refresh interval.
            if self._important_fields_changed(normalized_lines):
                force = True

            if not force and not self._refresh_allowed():
                return False

            self._ensure_awake()

            image  = self._render_image(normalized_lines, protocol_label)
            buffer = self.epd.getbuffer(image)

            # Decide between partial and full refresh.
            if self.partial_refresh_count >= self._partial_refresh_limit or not self._partial_base_ready:
                # Full refresh: clears ghosting and seeds both e-paper frame
                # buffers via displayPartBaseImage. Both buffers must be seeded
                # before displayPartial can run without ghosting.
                self.epd.Clear(0xFF)
                self.epd.displayPartBaseImage(buffer)
                self.partial_refresh_count  = 0
                self._partial_base_ready    = True
                log.debug("Full refresh performed (ghost prevention or first render)")
            else:
                # Partial refresh: faster, avoids full-screen flash.
                try:
                    self.epd.displayPartial(buffer)
                    self.partial_refresh_count += 1
                    log.debug(
                        "Partial refresh performed (%d/%d)",
                        self.partial_refresh_count,
                        self._partial_refresh_limit,
                    )
                except Exception:
                    # If partial refresh fails, fall back to a full refresh.
                    log.warning(
                        "Partial refresh failed. Falling back to full refresh.",
                        exc_info=True,
                    )
                    self.epd.Clear(0xFF)
                    self.epd.displayPartBaseImage(buffer)
                    self.partial_refresh_count = 0
                    self._partial_base_ready   = True

            self.last_lines        = normalized_lines
            self._last_protocol    = protocol_label
            self.last_refresh_time = time.monotonic()

            if self.auto_sleep and not self.startup_mode:
                self.sleep()

            return True

    def force_refresh(self):
        """
        Refresh the current screen contents again.

        This redraws the same body lines and resets the partial refresh
        counter so the next call performs a full refresh.

        Returns:
            True if the display was redrawn.
            False if there was nothing to redraw.
        """
        with self.lock:
            if self.last_lines is None:
                return False

            # Reset the counter so the forced refresh is always a full refresh.
            self.partial_refresh_count = self._partial_refresh_limit
            return self.show_lines(self.last_lines, force=True)

    def get_status(self):
        """
        Return basic display status info.
        Useful for debugging.
        """
        with self.lock:
            return {
                "initialized":             self.initialized,
                "sleeping":                self.sleeping,
                "startup_mode":            self.startup_mode,
                "last_lines":              self.last_lines,
                "last_refresh_time":       self.last_refresh_time,
                "min_refresh_interval":    self.min_refresh_interval,
                "auto_sleep":              self.auto_sleep,
                "partial_refresh_count":   self.partial_refresh_count,
                "partial_refresh_limit":   self._partial_refresh_limit,
            }

    # ------------------------------------------------------------------ #
    #  Private helpers                                                     #
    # ------------------------------------------------------------------ #

    def _build_font_cache(self):
        """
        Preload the body font sizes used by the display.

        Falls back to PIL's default font if the font file cannot be loaded.
        """
        cache = {}
        try:
            for size in range(self.MIN_FONT_SIZE, self.BASE_FONT_SIZE + 1):
                cache[size] = ImageFont.truetype(self.font_path, size)
        except OSError:
            log.warning(
                "Font file not found at '%s'. Using PIL default font.",
                self.font_path,
            )
            default_font = ImageFont.load_default()
            for size in range(self.MIN_FONT_SIZE, self.BASE_FONT_SIZE + 1):
                cache[size] = default_font
        return cache

    def _load_title_font(self):
        """
        Load the fixed font used for the header.

        Falls back to PIL's default font if the font file cannot be loaded.
        """
        try:
            return ImageFont.truetype(self.font_path, self.TITLE_FONT_SIZE)
        except OSError:
            return ImageFont.load_default()

    def _normalize_lines(self, lines, max_lines=5):
        """
        Clean up body lines before rendering or comparing.

        What this does:
        - Keep only the first 5 lines
        - Turn None into blank text
        - Collapse extra whitespace
        - Pad with blank lines if fewer than 5 were provided
        """
        cleaned = []
        for line in list(lines)[:max_lines]:
            if line is None:
                cleaned.append("")
            else:
                cleaned.append(" ".join(str(line).strip().split()))
        while len(cleaned) < max_lines:
            cleaned.append("")
        return cleaned

    def _render_image(self, body_lines, protocol=""):
        """
        Turn the 5 normalized body lines into a display image.

        Always draws the fixed "RaspberryFluke" header with an underline,
        then renders each body line below it.

        If protocol is provided ("SNMP", "LLDP", or "CDP"), it is drawn
        in small text to the right of the title on the header row, giving
        the technician a quick visual cue about which method produced the
        current result.

        A vertical overflow warning is logged if content would exceed the
        panel height, so font size misconfigurations are visible in logs.
        """
        image = Image.new("1", (self.DISPLAY_WIDTH, self.DISPLAY_HEIGHT), 255)
        draw  = ImageDraw.Draw(image)

        # Draw centered fixed-size header.
        header_text  = self.TITLE_TEXT
        header_width = draw.textlength(header_text, font=self.title_font)
        header_x     = int((self.DISPLAY_WIDTH - header_width) / 2)
        header_y     = self.TOP_MARGIN

        draw.text((header_x, header_y), header_text, font=self.title_font, fill=0)

        # Draw protocol indicator (e.g. "SNMP", "LLDP", "CDP") right-aligned
        # in the header row using a small font so it doesn't crowd the title.
        if protocol:
            proto_font  = self.font_cache.get(9, self.font_cache[self.MIN_FONT_SIZE])
            proto_text  = str(protocol).upper()[:4]
            proto_width = int(draw.textlength(proto_text, font=proto_font))
            proto_x     = self.DISPLAY_WIDTH - self.LEFT_MARGIN - proto_width
            draw.text((proto_x, header_y), proto_text, font=proto_font, fill=0)

        header_bbox = draw.textbbox(
            (header_x, header_y), header_text, font=self.title_font
        )
        underline_y = header_bbox[3] + self.TITLE_UNDERLINE_GAP
        draw.line(
            (header_bbox[0], underline_y, header_bbox[2], underline_y),
            fill=0,
            width=1,
        )

        # Body starts below the underline.
        y         = underline_y + self.TITLE_BODY_GAP
        max_width = self.DISPLAY_WIDTH - (self.LEFT_MARGIN * 2)

        for line in body_lines:
            if y >= self.DISPLAY_HEIGHT:
                log.warning(
                    "Display overflow: y=%d >= DISPLAY_HEIGHT=%d. "
                    "Line '%s' was not drawn. Consider reducing font size.",
                    y,
                    self.DISPLAY_HEIGHT,
                    line,
                )
                break

            font = self._fit_font_for_line(draw, line, max_width)
            draw.text((self.LEFT_MARGIN, y), line, font=font, fill=0)
            y += font.size + self.LINE_SPACING

        # Rotate so that the Ethernet port is at the top of the physical device.
        return image.rotate(180)

    def _fit_font_for_line(self, draw, text, max_width):
        """
        Pick the largest body font size that fits within max_width for one line.
        """
        for size in range(self.BASE_FONT_SIZE, self.MIN_FONT_SIZE - 1, -1):
            font = self.font_cache[size]
            if draw.textlength(text, font=font) <= max_width:
                return font
        return self.font_cache[self.MIN_FONT_SIZE]

    def _important_fields_changed(self, new_lines):
        """
        Check whether VLAN (index 3) or VOICE (index 4) changed.

        These fields trigger an immediate refresh, bypassing the normal
        minimum refresh interval, so the technician sees changes right away.
        """
        if self.last_lines is None:
            return True

        return (
            self.last_lines[3] != new_lines[3]
            or self.last_lines[4] != new_lines[4]
        )

    def _refresh_allowed(self):
        """
        Check whether enough time has passed since the last refresh.
        """
        elapsed = time.monotonic() - self.last_refresh_time
        return elapsed >= self.min_refresh_interval

    def _ensure_awake(self):
        """
        Make sure the display is ready to receive a new image.

        If the display was never started or was sleeping, call init() to
        wake it. After any hardware init, the partial refresh counter is
        reset to the limit so the next display call performs a full refresh.
        """
        if not self.initialized:
            self.epd.init()
            self.initialized           = True
            self.sleeping              = False
            self.partial_refresh_count = self._partial_refresh_limit
            return

        if self.sleeping:
            self.epd.init()
            self.sleeping              = False
            self.partial_refresh_count = self._partial_refresh_limit