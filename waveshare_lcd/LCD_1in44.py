"""
LCD_1in44.py

Driver for the Waveshare 1.44inch LCD HAT (SKU 13891).

Display specifications:
  - Controller : ST7735S
  - Resolution : 128 x 128 pixels
  - Interface  : 4-wire SPI
  - Colors     : RGB565 (65K colors)

GPIO pin assignments (BCM numbering):
  - RST  : 27  (reset)
  - DC   : 25  (data/command select)
  - BL   : 24  (backlight PWM)
  - CS   : 8   (SPI chip select, CE0)
  - CLK  : 11  (SPI clock, SCLK)
  - MOSI : 10  (SPI data, MOSI)

This driver provides the API expected by display_lcd.py:
  - LCD()                      — create the driver object
  - SCAN_DIR_DFT               — default scan direction constant
  - lcd.LCD_Init(scan_dir)     — initialize the hardware
  - lcd.LCD_ShowImage(img,x,y) — push a PIL image to the screen
  - lcd.bl_DutyCycle(pct)      — set backlight brightness (0-100)
  - lcd.module_exit()          — clean up GPIO and SPI

Based on the official Waveshare demo code for the 1.44inch LCD HAT.
Reference: https://www.waveshare.com/wiki/1.44inch_LCD_HAT
"""

import time
import spidev
import RPi.GPIO as GPIO
from PIL import Image


# ============================================================
# GPIO pin assignments (BCM numbering)
# ============================================================

RST_PIN = 27
DC_PIN  = 25
BL_PIN  = 24
CS_PIN  = 8    # CE0 — handled by spidev, not set manually


# ============================================================
# Scan direction constants
# ============================================================

# The ST7735S supports 8 scan directions via MADCTL register bits.
# We define the two most commonly needed ones.

L2R_U2D = 0   # Left-to-right, top-to-bottom (default landscape)
SCAN_DIR_DFT = L2R_U2D


# ============================================================
# ST7735S command codes
# ============================================================

NOP        = 0x00
SWRESET    = 0x01
SLPIN      = 0x10
SLPOUT     = 0x11
NORON      = 0x13
INVOFF     = 0x20
INVON      = 0x21
GAMSET     = 0x26
DISPOFF    = 0x28
DISPON     = 0x29
CASET      = 0x2A
RASET      = 0x2B
RAMWR      = 0x2C
RAMRD      = 0x2E
MADCTL     = 0x36
COLMOD     = 0x3A
FRMCTR1    = 0xB1
FRMCTR2    = 0xB2
FRMCTR3    = 0xB3
INVCTR     = 0xB4
DISSET5    = 0xB6
PWCTR1     = 0xC0
PWCTR2     = 0xC1
PWCTR3     = 0xC2
PWCTR4     = 0xC3
PWCTR5     = 0xC4
VMCTR1     = 0xC5
GMCTRP1    = 0xE0
GMCTRN1    = 0xE1


# ============================================================
# LCD driver class
# ============================================================

class LCD:
    """
    Driver for the Waveshare 1.44inch LCD HAT.

    Usage:
        lcd = LCD()
        lcd.LCD_Init(SCAN_DIR_DFT)
        lcd.LCD_ShowImage(pil_image, 0, 0)
        lcd.bl_DutyCycle(100)    # full brightness
        lcd.module_exit()
    """

    WIDTH  = 128
    HEIGHT = 128

    def __init__(self):
        self._spi       = None
        self._pwm       = None
        self._initialized = False

    # --------------------------------------------------------
    # Low-level SPI and GPIO helpers
    # --------------------------------------------------------

    def _gpio_init(self):
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(RST_PIN, GPIO.OUT, initial=GPIO.HIGH)
        GPIO.setup(DC_PIN,  GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(BL_PIN,  GPIO.OUT, initial=GPIO.HIGH)

    def _spi_init(self):
        self._spi = spidev.SpiDev()
        self._spi.open(0, 0)                 # bus 0, device 0 (CE0)
        self._spi.max_speed_hz = 9000000     # 9 MHz — reliable on Pi Zero
        self._spi.mode = 0b00               # CPOL=0, CPHA=0

    def _send_command(self, cmd):
        GPIO.output(DC_PIN, GPIO.LOW)
        self._spi.writebytes([cmd])

    def _send_data(self, data):
        GPIO.output(DC_PIN, GPIO.HIGH)
        if isinstance(data, int):
            self._spi.writebytes([data])
        else:
            # Send in chunks to avoid SPI buffer limits.
            chunk = 4096
            for i in range(0, len(data), chunk):
                self._spi.writebytes(list(data[i:i + chunk]))

    def _reset(self):
        GPIO.output(RST_PIN, GPIO.HIGH)
        time.sleep(0.01)
        GPIO.output(RST_PIN, GPIO.LOW)
        time.sleep(0.01)
        GPIO.output(RST_PIN, GPIO.HIGH)
        time.sleep(0.01)

    # --------------------------------------------------------
    # ST7735S initialization sequence
    # --------------------------------------------------------

    def _init_st7735s(self, scan_dir):
        """
        Send the ST7735S initialization command sequence.

        This sequence is derived from the official Waveshare demo code
        for the 1.44inch LCD HAT and configures the controller for
        128x128 RGB565 operation.
        """
        # Software reset and sleep-out
        self._send_command(SWRESET)
        time.sleep(0.15)
        self._send_command(SLPOUT)
        time.sleep(0.5)

        # Frame rate control
        self._send_command(FRMCTR1)
        self._send_data([0x01, 0x2C, 0x2D])
        self._send_command(FRMCTR2)
        self._send_data([0x01, 0x2C, 0x2D])
        self._send_command(FRMCTR3)
        self._send_data([0x01, 0x2C, 0x2D, 0x01, 0x2C, 0x2D])

        # Inversion control — line inversion
        self._send_command(INVCTR)
        self._send_data(0x07)

        # Power control
        self._send_command(PWCTR1)
        self._send_data([0xA2, 0x02, 0x84])
        self._send_command(PWCTR2)
        self._send_data(0xC5)
        self._send_command(PWCTR3)
        self._send_data([0x0A, 0x00])
        self._send_command(PWCTR4)
        self._send_data([0x8A, 0x2A])
        self._send_command(PWCTR5)
        self._send_data([0x8A, 0xEE])

        # VCOM control
        self._send_command(VMCTR1)
        self._send_data(0x0E)

        # Memory data access control (scan direction / rotation)
        self._send_command(MADCTL)
        # 0x C8 = MY|MX|MV|ML|RGB|MH = 1100 1000
        # Adjust for the HAT orientation — default gives correct portrait.
        if scan_dir == L2R_U2D:
            self._send_data(0xC8)
        else:
            self._send_data(0xC8)

        # Color mode — 16-bit RGB565
        self._send_command(COLMOD)
        self._send_data(0x05)

        # Positive / negative gamma correction
        self._send_command(GMCTRP1)
        self._send_data([
            0x02, 0x1C, 0x07, 0x12,
            0x37, 0x32, 0x29, 0x2D,
            0x29, 0x25, 0x2B, 0x39,
            0x00, 0x01, 0x03, 0x10,
        ])
        self._send_command(GMCTRN1)
        self._send_data([
            0x03, 0x1D, 0x07, 0x06,
            0x2E, 0x2C, 0x29, 0x2D,
            0x2E, 0x2E, 0x37, 0x3F,
            0x00, 0x00, 0x02, 0x10,
        ])

        # Normal display on
        self._send_command(NORON)
        time.sleep(0.01)

        # Set column and row address to full 128x128
        self._send_command(CASET)
        self._send_data([0x00, 0x00, 0x00, 0x7F])
        self._send_command(RASET)
        self._send_data([0x00, 0x00, 0x00, 0x7F])

        # Display on
        self._send_command(DISPON)
        time.sleep(0.1)

    # --------------------------------------------------------
    # Public API
    # --------------------------------------------------------

    def LCD_Init(self, scan_dir=SCAN_DIR_DFT):
        """
        Initialize the LCD hardware.

        Must be called once before LCD_ShowImage or bl_DutyCycle.

        Parameters:
            scan_dir : scan direction constant (use SCAN_DIR_DFT)
        """
        self._gpio_init()
        self._spi_init()

        # Set up backlight PWM on BL_PIN at 1kHz.
        self._pwm = GPIO.PWM(BL_PIN, 1000)
        self._pwm.start(100)   # start at full brightness

        self._reset()
        self._init_st7735s(scan_dir)
        self._initialized = True

    def LCD_ShowImage(self, image, x, y):
        """
        Display a PIL image on the LCD.

        The image is converted to RGB565 and sent via SPI.

        Parameters:
            image : PIL Image object (RGB mode, 128x128)
            x     : x offset (typically 0)
            y     : y offset (typically 0)
        """
        if not self._initialized:
            return

        # Resize to display dimensions if needed.
        if image.size != (self.WIDTH, self.HEIGHT):
            image = image.resize((self.WIDTH, self.HEIGHT))

        # Convert RGB888 PIL image to RGB565 byte stream.
        img_rgb = image.convert("RGB")
        pixels  = list(img_rgb.getdata())

        buf = bytearray(self.WIDTH * self.HEIGHT * 2)
        idx = 0
        for r, g, b in pixels:
            # RGB565: RRRRRGGGGGGBBBBB packed into two bytes big-endian
            color = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            buf[idx]     = (color >> 8) & 0xFF
            buf[idx + 1] = color & 0xFF
            idx += 2

        # Set write window to full screen.
        self._send_command(CASET)
        self._send_data([0x00, x, 0x00, x + self.WIDTH - 1])
        self._send_command(RASET)
        self._send_data([0x00, y, 0x00, y + self.HEIGHT - 1])

        # Write pixel data.
        self._send_command(RAMWR)
        self._send_data(buf)

    def bl_DutyCycle(self, duty):
        """
        Set the backlight brightness.

        Parameters:
            duty : brightness percentage, 0 (off) to 100 (full)
        """
        if self._pwm is not None:
            duty = max(0, min(100, int(duty)))
            self._pwm.ChangeDutyCycle(duty)

    def module_exit(self):
        """
        Clean up SPI and GPIO resources.

        Called during graceful shutdown. Safe to call multiple times.
        """
        if self._pwm is not None:
            try:
                self._pwm.stop()
            except Exception:
                pass
            self._pwm = None

        if self._spi is not None:
            try:
                self._spi.close()
            except Exception:
                pass
            self._spi = None

        try:
            GPIO.output(BL_PIN,  GPIO.LOW)
            GPIO.output(RST_PIN, GPIO.LOW)
            GPIO.output(DC_PIN,  GPIO.LOW)
            GPIO.cleanup()
        except Exception:
            pass

        self._initialized = False
