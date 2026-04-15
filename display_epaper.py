"""
display_epaper.py

This file only handles the Waveshare 2.13" V3 e-paper display.

What this file does:
- Start the e-paper display
- Draw text onto an image
- Show that image on the screen
- Limit how often the screen refreshes
- Force a refresh when VLAN or VOICE changes
- Put the display to sleep when appropriate

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

    # Font settings for the body lines.
    BASE_FONT_SIZE = 16
    MIN_FONT_SIZE = 10
    LINE_SPACING = 2

    # Fixed header settings.
    TITLE_TEXT = "RaspberryFluke"
    TITLE_FONT_SIZE = 16
    TITLE_UNDERLINE_GAP = 1
    TITLE_BODY_GAP = 2

    # Default font file.
    DEFAULT_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

    def __init__(
        self,
        font_path=None,
        min_refresh_interval=10,
        auto_sleep=True,
        startup_mode=True,
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
            If True, the display stays in a startup-friendly mode where it can
            remain awake between updates until the first real result is shown.
        """
        self.font_path = font_path or self.DEFAULT_FONT_PATH
        self.min_refresh_interval = min_refresh_interval
        self.auto_sleep = auto_sleep

        # Startup mode is meant to reduce early boot churn.
        # While enabled, we do not automatically sleep after each update.
        self.startup_mode = startup_mode

        # Create the Waveshare display object.
        self.epd = epd2in13_V3.EPD()

        # RLock allows one method to safely call another method that also uses the same lock.
        self.lock = threading.RLock()

        # Track display state.
        self.initialized = False
        self.sleeping = False

        # Save the last 5 body lines shown on screen.
        self.last_lines = None

        # Save the last refresh time.
        self.last_refresh_time = 0.0

        # Preload the body font sizes once so we do not repeatedly read from disk.
        self.font_cache = self._build_font_cache()

        # Load the fixed title font once.
        self.title_font = self._load_title_font()

    def _build_font_cache(self):
        """
        Preload the body font sizes used by the display.

        If the font file cannot be loaded, fall back to PIL's default font.
        """
        cache = {}

        try:
            for size in range(self.MIN_FONT_SIZE, self.BASE_FONT_SIZE + 1):
                cache[size] = ImageFont.truetype(self.font_path, size)
        except OSError:
            default_font = ImageFont.load_default()
            for size in range(self.MIN_FONT_SIZE, self.BASE_FONT_SIZE + 1):
                cache[size] = default_font

        return cache

    def _load_title_font(self):
        """
        Load the fixed font used for the header.

        If the configured font file cannot be loaded, fall back to PIL's default font.
        """
        try:
            return ImageFont.truetype(self.font_path, self.TITLE_FONT_SIZE)
        except OSError:
            return ImageFont.load_default()

    def initialize(self, clear_on_start=False):
        """
        Start the e-paper display.

        clear_on_start:
            If True, clear the screen to white when starting up.

        For RaspberryFluke appliance use, this should usually stay False so we
        can jump directly to a boot/listening screen instead of doing an extra
        blank-screen refresh first.
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

        Note:
            For this project, clearing on startup is usually not ideal.
            The appliance should normally jump straight to a startup screen.
        """
        with self.lock:
            self._ensure_awake()

            self.epd.Clear(0xFF)

            # Remember that the screen is now blank.
            self.last_lines = ["", "", "", "", ""]
            self.last_refresh_time = time.monotonic()

            if sleep_after and self.auto_sleep and not self.startup_mode:
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
                try:
                    self._ensure_awake()
                    self.epd.Clear(0xFF)
                    self.last_lines = ["", "", "", "", ""]
                    self.last_refresh_time = time.monotonic()
                except Exception:
                    pass

            try:
                self.epd.sleep()
            except Exception:
                pass

            self.sleeping = True
            self.initialized = False

    def set_startup_mode(self, enabled):
        """
        Enable or disable startup mode.

        Startup mode is intended for the early boot phase where we want the
        display to remain responsive and avoid extra wake/sleep churn.

        Typical use:
        - startup_mode = True while booting and waiting for first discovery
        - startup_mode = False after the first real neighbor result is shown
        """
        with self.lock:
            self.startup_mode = bool(enabled)

    def show_lines(self, lines, force=False):
        """
        Show text on the e-paper display.

        lines:
            A list of body lines to show on the screen.
            For normal neighbor screens, this should be:
            SW / IP / PORT / VLAN / VOICE

            For startup screens, if the first line is "RaspberryFluke",
            it will be treated as the header and will not be drawn twice.

        force:
            If True, refresh no matter what.

        Returns:
            True if the screen was refreshed.
            False if the update was skipped.
        """
        with self.lock:
            normalized_lines = self._normalize_lines(lines)

            # If the body text is exactly the same as last time, skip it.
            if not force and normalized_lines == self.last_lines:
                return False

            # If VLAN or VOICE changed, allow an immediate forced refresh.
            if self._important_fields_changed(normalized_lines):
                force = True

            # If not forced, respect the normal refresh timer.
            if not force and not self._refresh_allowed():
                return False

            self._ensure_awake()

            image = self._render_image(lines)
            buffer = self.epd.getbuffer(image)
            self.epd.display(buffer)

            # Save only the normalized body lines.
            self.last_lines = normalized_lines
            self.last_refresh_time = time.monotonic()

            # During startup mode, keep the display awake so repeated early
            # screen changes do not constantly reinitialize the panel.
            if self.auto_sleep and not self.startup_mode:
                self.sleep()

            return True

    def _render_image(self, lines):
        """
        Turn the text lines into an image.

        Behavior:
        - Draw a fixed header at the top
        - Center the header
        - Underline the header
        - Keep the header at a fixed size
        - Fit each body line independently
        """
        image = Image.new("1", (self.DISPLAY_WIDTH, self.DISPLAY_HEIGHT), 255)
        draw = ImageDraw.Draw(image)

        header_text, body_lines = self._split_header_and_body(lines)

        # Draw centered fixed-size header.
        header_width = draw.textlength(header_text, font=self.title_font)
        header_x = int((self.DISPLAY_WIDTH - header_width) / 2)
        header_y = self.TOP_MARGIN

        draw.text((header_x, header_y), header_text, font=self.title_font, fill=0)

        header_bbox = draw.textbbox((header_x, header_y), header_text, font=self.title_font)
        underline_y = header_bbox[3] + self.TITLE_UNDERLINE_GAP
        draw.line((header_bbox[0], underline_y, header_bbox[2], underline_y), fill=0, width=1)

        # Start body below the underlined header.
        y = underline_y + self.TITLE_BODY_GAP
        max_width = self.DISPLAY_WIDTH - (self.LEFT_MARGIN * 2)

        for line in body_lines:
            font = self._fit_font_for_line(draw, line, max_width)
            draw.text((self.LEFT_MARGIN, y), line, font=font, fill=0)
            y += font.size + self.LINE_SPACING

        return image.rotate(180)

    def _split_header_and_body(self, lines):
        """
        Split incoming lines into a header and 5 body lines.

        Rules:
        - If the first provided line is exactly "RaspberryFluke",
          treat it as the header text and use the remaining lines as body text.
        - Otherwise, use the default fixed header and treat all provided lines
          as body lines.

        This lets old startup screens keep working without drawing the title twice.
        """
        incoming = list(lines)

        if incoming and str(incoming[0]).strip() == self.TITLE_TEXT:
            header_text = self.TITLE_TEXT
            body_source = incoming[1:]
        else:
            header_text = self.TITLE_TEXT
            body_source = incoming

        body_lines = self._normalize_body_lines(body_source)
        return header_text, body_lines

    def _fit_font_for_line(self, draw, text, max_width):
        """
        Pick the largest body font size that fits within max_width for this one line.
        """
        for size in range(self.BASE_FONT_SIZE, self.MIN_FONT_SIZE - 1, -1):
            font = self.font_cache[size]

            if draw.textlength(text, font=font) <= max_width:
                return font

        return self.font_cache[self.MIN_FONT_SIZE]

    def _normalize_body_lines(self, lines, max_lines=5):
        """
        Clean up the body lines before drawing them.

        What this does:
        - Keep only the first 5 body lines
        - Turn None into blank text
        - Remove extra spaces
        - Add blank lines if fewer than 5 were provided
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

    def _normalize_lines(self, lines):
        """
        Return the normalized 5 body lines used for internal comparison.

        This ignores the fixed header because the header does not participate in
        VLAN/VOICE comparisons or body-line redraw checks.
        """
        _header_text, body_lines = self._split_header_and_body(lines)
        return body_lines

    def _important_fields_changed(self, new_lines):
        """
        Check whether VLAN or VOICE changed compared to the previous screen.

        With the fixed header design, new_lines contains body lines only:

        index 0 = SW
        index 1 = IP
        index 2 = PORT
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

        This redraws the same body lines on purpose.
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
                "startup_mode": self.startup_mode,
                "last_lines": self.last_lines,
                "last_refresh_time": self.last_refresh_time,
                "min_refresh_interval": self.min_refresh_interval,
                "auto_sleep": self.auto_sleep,
            }