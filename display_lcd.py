"""
display_lcd.py

This file only handles the Waveshare 1.44" LCD HAT display.

What this file does:
- Start the LCD display
- Draw text onto an image
- Show that image on the screen
- Keep the backlight on during normal operation
- Optionally clear the screen
- Optionally turn the backlight off during graceful shutdown

What this file does NOT do:
- Read config files
- Capture packets
- Parse LLDP or CDP data
- Decide whether a field change is important
- Store broader application state
"""

import threading

from PIL import Image, ImageDraw, ImageFont
from waveshare_lcd import LCD_1in44


class LCDDisplay:
    # Screen size in pixels for the Waveshare 1.44" LCD HAT.
    DISPLAY_WIDTH = 128
    DISPLAY_HEIGHT = 128

    # Text position on the screen.
    LEFT_MARGIN = 4
    TOP_MARGIN = 4

    # Font sizing rules.
    BASE_FONT_SIZE = 14
    MIN_FONT_SIZE = 8
    LINE_SPACING = 2

    # Default font file.
    DEFAULT_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

    # Basic colors for a small LCD UI.
    DEFAULT_BG_COLOR = (0, 0, 0)
    DEFAULT_TEXT_COLOR = (255, 255, 255)

    # Backlight brightness percent.
    DEFAULT_BACKLIGHT_BRIGHTNESS = 100

    def __init__(
        self,
        font_path=None,
        rotate_180=True,
        clear_on_start=True,
        background_color=None,
        text_color=None,
        backlight_brightness=100,
    ):
        """
        Set up the LCD display object.

        font_path:
            Optional path to a .ttf font file.

        rotate_180:
            If True, rotate the rendered image by 180 degrees before display.

        clear_on_start:
            If True, clear the LCD during initialization.

        background_color:
            RGB tuple for the background color.

        text_color:
            RGB tuple for the text color.

        backlight_brightness:
            Brightness percent from 0 to 100.
        """
        self.font_path = font_path or self.DEFAULT_FONT_PATH
        self.rotate_180 = rotate_180
        self.clear_on_start = clear_on_start

        self.background_color = background_color or self.DEFAULT_BG_COLOR
        self.text_color = text_color or self.DEFAULT_TEXT_COLOR
        self.backlight_brightness = max(0, min(100, int(backlight_brightness)))

        self.lock = threading.RLock()

        # Track display state.
        self.initialized = False
        self.backlight_on = False

        # Save the last 5 lines shown on screen.
        self.last_lines = None

        # LCD driver object gets created during initialize().
        self.lcd = None

    def initialize(self):
        """
        Start the LCD display and get it ready for drawing.

        This method:
        - creates the Waveshare LCD object
        - initializes the LCD hardware
        - turns the backlight on
        - clears the screen if requested
        """
        with self.lock:
            if self.initialized:
                return

            self.lcd = LCD_1in44.LCD()

            if hasattr(LCD_1in44, "SCAN_DIR_DFT"):
                self.lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
            else:
                self.lcd.LCD_Init()

            self.initialized = True
            self._set_backlight(True)

            if self.clear_on_start:
                self.clear()

    def clear(self, color=None):
        """
        Clear the LCD to a solid color.

        color:
            Optional RGB tuple.
            If not provided, the configured background color is used.
        """
        with self.lock:
            self._ensure_initialized()

            fill_color = color or self.background_color
            image = Image.new(
                "RGB",
                (self.DISPLAY_WIDTH, self.DISPLAY_HEIGHT),
                fill_color,
            )

            self._show_image(image)

            # Remember that the screen is now blank.
            self.last_lines = ["", "", "", "", ""]

    def show_lines(self, lines, force=False):
        """
        Show text on the LCD display.

        lines:
            A list of text lines to show on the screen.

        force:
            If True, redraw even if the text did not change.

        Returns:
            True if the screen was updated.
            False if the update was skipped.
        """
        with self.lock:
            self._ensure_initialized()

            normalized_lines = self._normalize_lines(lines)
            prepared_lines = self._prepare_lines_for_lcd(normalized_lines)

            # LCDs do not need e-paper-style refresh throttling.
            # If the text is exactly the same and force is False, skip redraw.
            if not force and prepared_lines == self.last_lines:
                return False

            image = self._render_image(prepared_lines)
            self._show_image(image)

            self.last_lines = prepared_lines

            # Keep the backlight on during normal operation.
            self._set_backlight(True)

            return True

    def sleep(self):
        """
        Optional low-activity display state.

        For this LCD version, normal operation keeps the backlight on
        continuously so the user can always see the screen.

        This method intentionally does nothing right now so the rest
        of the project can still call a display 'sleep' method if needed.

        Returns:
            False to indicate no state change was made.
        """
        with self.lock:
            self._ensure_initialized()
            return False

    def wake(self):
        """
        Make sure the LCD is visible again.

        Returns:
            True if the backlight was turned on.
            False if it was already on.
        """
        with self.lock:
            self._ensure_initialized()

            if self.backlight_on:
                return False

            self._set_backlight(True)
            return True

    def shutdown(self, clear_first=False, backlight_off=True):
        """
        Shut down the LCD as gracefully as possible.

        clear_first:
            If True, clear the screen before shutdown.

        backlight_off:
            If True, turn the backlight off.

        Important:
            This only works if your program exits cleanly and this method
            actually gets called. If power is physically pulled, Python
            does not get a chance to run this cleanup code.
        """
        with self.lock:
            if not self.initialized:
                return

            if clear_first:
                self.clear()

            if backlight_off:
                self._set_backlight(False)

            try:
                self.lcd.module_exit()
            except Exception:
                pass

            self.initialized = False
            self.backlight_on = False

    def force_refresh(self):
        """
        Redraw the current screen contents again.

        This redraws the same text on purpose.

        Returns:
            True if the display was redrawn.
            False if there was nothing to redraw.
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
                "backlight_on": self.backlight_on,
                "last_lines": self.last_lines,
                "rotate_180": self.rotate_180,
                "backlight_brightness": self.backlight_brightness,
            }

    def _render_image(self, lines):
        """
        Turn the text lines into an RGB image that fits the screen.
        """
        image = Image.new(
            "RGB",
            (self.DISPLAY_WIDTH, self.DISPLAY_HEIGHT),
            self.background_color,
        )
        draw = ImageDraw.Draw(image)

        font = self._choose_font(draw, lines)
        y = self.TOP_MARGIN

        for line in lines:
            draw.text(
                (self.LEFT_MARGIN, y),
                line,
                font=font,
                fill=self.text_color,
            )

            bbox = draw.textbbox((0, 0), line, font=font)
            line_height = bbox[3] - bbox[1]
            y += line_height + self.LINE_SPACING

        if self.rotate_180:
            image = image.rotate(180)

        return image

    def _show_image(self, image):
        """
        Send a PIL image to the LCD.

        Waveshare's LCD_ShowImage method expects an image matching
        the display dimensions.
        """
        self._ensure_initialized()
        self.lcd.LCD_ShowImage(image, 0, 0)

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
                cleaned.append(" ".join(str(line).strip().split()))

        while len(cleaned) < max_lines:
            cleaned.append("")

        return cleaned

    def _prepare_lines_for_lcd(self, lines):
        """
        Make the lines more likely to fit on the small 128x128 LCD.

        This helper keeps the display logic local to the LCD module.
        It does not parse protocols or decide what values mean.
        """
        draw = ImageDraw.Draw(
            Image.new("RGB", (self.DISPLAY_WIDTH, self.DISPLAY_HEIGHT), self.background_color)
        )
        font = self._get_font(self.MIN_FONT_SIZE)
        usable_width = self.DISPLAY_WIDTH - (self.LEFT_MARGIN * 2)

        prepared = []

        for line in lines:
            shortened = self._abbreviate_terms(line)
            shortened = self._truncate_to_width(draw, shortened, font, usable_width)
            prepared.append(shortened)

        return prepared

    def _abbreviate_terms(self, text):
        """
        Shorten a few known long interface terms so they fit better.

        This is display formatting only.
        """
        replacements = {
            "TenGigabitEthernet": "Te",
            "GigabitEthernet": "Gi",
            "FastEthernet": "Fa",
            "TwentyFiveGigE": "Twe",
            "TwentyFiveGigabitEthernet": "Twe",
            "FortyGigabitEthernet": "Fo",
            "HundredGigE": "Hu",
            "HundredGigabitEthernet": "Hu",
            "Port-channel": "Po",
            "Port-Channel": "Po",
            "Ethernet": "Eth",
        }

        result = text
        for old, new in replacements.items():
            result = result.replace(old, new)

        return result

    def _truncate_to_width(self, draw, text, font, usable_width):
        """
        Truncate a line with ... if it is too wide at the minimum font size.
        """
        if not text:
            return text

        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]

        if text_width <= usable_width:
            return text

        ellipsis = "..."
        trimmed = text

        while trimmed:
            trimmed = trimmed[:-1].rstrip()
            candidate = trimmed + ellipsis
            bbox = draw.textbbox((0, 0), candidate, font=font)
            candidate_width = bbox[2] - bbox[0]

            if candidate_width <= usable_width:
                return candidate

        return ellipsis

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

    def _set_backlight(self, on):
        """
        Turn the LCD backlight on or off using the vendor driver's PWM method.
        """
        self._ensure_initialized()

        if on:
            self.lcd.bl_DutyCycle(self.backlight_brightness)
            self.backlight_on = True
        else:
            self.lcd.bl_DutyCycle(0)
            self.backlight_on = False

    def _ensure_initialized(self):
        """
        Make sure the display is ready to use.
        """
        if not self.initialized:
            raise RuntimeError(
                "LCD display is not initialized. Call initialize() first."
            )