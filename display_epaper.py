"""
display_epaper.py

This file only handles the Waveshare 2.13" V3 e-paper display.

What this file does:
- Start the e-paper display
- Draw text onto an image
- Show that image on the screen
- Limit how often the screen refreshes
- Force a refresh when VLAN or VOICE changes
- Put the display to sleep when not actively updating

What this file does NOT do:
- Read config files
- Capture packets
- Parse LLDP or CDP data
- Store broader application state
"""

import threading
import time

from PIL import Image, ImageDraw, ImageFont
from waveshare_epd import epd2in13_V3


class EPaperDisplay:
    # Screen size in pixels for the Waveshare 2.13" V3 display.
    DISPLAY_WIDTH = 250
    DISPLAY_HEIGHT = 122

    # Text position on the screen.
    LEFT_MARGIN = 10
    TOP_MARGIN = 4

    # Font settings.
    BASE_FONT_SIZE = 16
    MIN_FONT_SIZE = 10
    LINE_SPACING = 2

    # Default font file.
    DEFAULT_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

    def __init__(
        self,
        font_path=None,
        min_refresh_interval=10,
        auto_sleep=True,
    ):
        """
        Set up the display object.

        font_path:
            Optional path to a .ttf font file.

        min_refresh_interval:
            Minimum number of seconds between normal display refreshes.

        auto_sleep:
            If True, the panel goes back to sleep after each update.
        """
        self.font_path = font_path or self.DEFAULT_FONT_PATH
        self.min_refresh_interval = min_refresh_interval
        self.auto_sleep = auto_sleep

        # Create the Waveshare display object.
        self.epd = epd2in13_V3.EPD()

        # RLock allows one method to safely call another method that also uses the same lock.
        self.lock = threading.RLock()

        # Track display state.
        self.initialized = False
        self.sleeping = False

        # Save the last 5 lines shown on screen.
        self.last_lines = None

        # Save the last refresh time.
        self.last_refresh_time = 0

    def initialize(self, clear_on_start=True):
        """
        Start the e-paper display.

        clear_on_start:
            If True, clear the screen to white when starting up.
        """
        with self.lock:
            if self.initialized and not self.sleeping:
                return

            self.epd.init()
            self.initialized = True
            self.sleeping = False

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

            # Remember that the screen is now blank.
            self.last_lines = ["", "", "", "", ""]
            self.last_refresh_time = time.monotonic()

            if sleep_after and self.auto_sleep:
                self.sleep()

    def sleep(self):
        """
        Put the e-paper display into sleep mode.

        The image usually stays visible even after sleep.
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
            Usually want this to stay False
            so the last result stays visible on the screen.
        """
        with self.lock:
            if not self.initialized:
                return

            if clear_before_sleep:
                self._ensure_awake()
                self.epd.Clear(0xFF)
                self.last_lines = ["", "", "", "", ""]
                self.last_refresh_time = time.monotonic()

            self.epd.sleep()
            self.sleeping = True
            self.initialized = False

    def show_lines(self, lines, force=False):
        """
        Show text on the e-paper display.

        lines:
            A list of text lines to show on the screen.

        force:
            If True, refresh no matter what.

        Returns:
            True if the screen was refreshed.
            False if the update was skipped.
        """
        with self.lock:
            normalized_lines = self._normalize_lines(lines)

            # If the text is exactly the same as last time, skip it.
            if not force and normalized_lines == self.last_lines:
                return False

            # If VLAN or VOICE changed, allow an immediate forced refresh.
            if self._important_fields_changed(normalized_lines):
                force = True

            # If not forced, respect the normal refresh timer.
            if not force and not self._refresh_allowed():
                return False

            self._ensure_awake()

            image = self._render_image(normalized_lines)
            buffer = self.epd.getbuffer(image)
            self.epd.display(buffer)

            # Save what is now on the screen.
            self.last_lines = normalized_lines
            self.last_refresh_time = time.monotonic()

            # Put the display back to sleep after updating.
            if self.auto_sleep:
                self.sleep()

            return True

    def _render_image(self, lines):
        """
        Turn the text lines into an image that fits the screen.
        """
        image = Image.new("1", (self.DISPLAY_WIDTH, self.DISPLAY_HEIGHT), 255)
        draw = ImageDraw.Draw(image)

        font = self._choose_font(draw, lines)
        y = self.TOP_MARGIN

        for line in lines:
            draw.text((self.LEFT_MARGIN, y), line, font=font, fill=0)

            bbox = draw.textbbox((0, 0), line, font=font)
            line_height = bbox[3] - bbox[1]
            y += line_height + self.LINE_SPACING

        return image

    def _normalize_lines(self, lines, max_lines=5):
        """
        Clean up the lines before drawing them.

        What this does:
        - Keep only the first 5 lines
        - Turn None into blank text
        - Remove extra spaces
        - Add blank lines if fewer than 5 were provided
        """
        cleaned = []

        for line in list(lines)[:max_lines]:
            if line is None:
                cleaned.append("")
            else:
                cleaned.append(str(line).strip())

        while len(cleaned) < max_lines:
            cleaned.append("")

        return cleaned

    def _important_fields_changed(self, new_lines):
        """
        Check whether VLAN or VOICE changed compared to the previous screen.

        line 4 = VLAN
        line 5 = VOICE

        Using zero-based indexes:
        index 3 = VLAN
        index 4 = VOICE
        """
        if self.last_lines is None:
            return True

        old_vlan = self.last_lines[3]
        old_voice = self.last_lines[4]

        new_vlan = new_lines[3]
        new_voice = new_lines[4]

        return old_vlan != new_vlan or old_voice != new_voice

    def _choose_font(self, draw, lines):
        """
        Pick the biggest font size that still fits on the screen.
        """
        for size in range(self.BASE_FONT_SIZE, self.MIN_FONT_SIZE - 1, -1):
            font = self._get_font(size)

            if self._lines_fit(draw, lines, font):
                return font

        return self._get_font(self.MIN_FONT_SIZE)

    def _lines_fit(self, draw, lines, font):
        """
        Check if all lines fit on the screen.
        """
        usable_width = self.DISPLAY_WIDTH - (self.LEFT_MARGIN * 2)
        usable_height = self.DISPLAY_HEIGHT - self.TOP_MARGIN

        total_height = 0

        for index, line in enumerate(lines):
            bbox = draw.textbbox((0, 0), line, font=font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]

            if text_width > usable_width:
                return False

            total_height += text_height

            if index < len(lines) - 1:
                total_height += self.LINE_SPACING

        return total_height <= usable_height

    def _get_font(self, size):
        """
        Load the font file.

        If the font file is missing, use the default PIL font
        so the program does not crash.
        """
        try:
            return ImageFont.truetype(self.font_path, size)
        except OSError:
            return ImageFont.load_default()

    def _refresh_allowed(self):
        """
        Check whether enough time has passed since the last refresh.
        """
        elapsed = time.monotonic() - self.last_refresh_time
        return elapsed >= self.min_refresh_interval

    def _ensure_awake(self):
        """
        Make sure the display is ready to use.

        If it was never started, start it.
        If it was sleeping, wake it up.
        """
        if not self.initialized:
            self.epd.init()
            self.initialized = True
            self.sleeping = False
            return

        if self.sleeping:
            self.epd.init()
            self.sleeping = False

    def force_refresh(self):
        """
        Refresh the current screen contents again.

        This redraws the same text on purpose.
        """
        with self.lock:
            if self.last_lines is None:
                return False

            return self.show_lines(self.last_lines, force=True)

    def get_status(self):
        """
        Return basic display status info.
        Useful for debugging.
        """
        with self.lock:
            return {
                "initialized": self.initialized,
                "sleeping": self.sleeping,
                "last_lines": self.last_lines,
                "last_refresh_time": self.last_refresh_time,
                "min_refresh_interval": self.min_refresh_interval,
                "auto_sleep": self.auto_sleep,
            }