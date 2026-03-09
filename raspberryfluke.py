import subprocess
import time
import threading
import signal
import logging
import re
from PIL import Image, ImageDraw, ImageFont
from waveshare_epd import epd2in13_V3

# ============================================================
# -------------------- CONFIGURATION -------------------------
# ============================================================

# Physical resolution of the Waveshare 2.13" V3 e-paper panel.
DISPLAY_WIDTH = 250
DISPLAY_HEIGHT = 122

# Layout margins for text placement.
LEFT_MARGIN = 10
TOP_MARGIN = 4

# Font sizing rules.
# Five lines must fit vertically, so the base size is intentionally smaller.
BASE_FONT_SIZE = 16
MIN_FONT_SIZE = 10
LINE_SPACING = 2  # Extra pixels between lines.

# Path to system font used for rendering text.
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# The only wired interface on the RaspberryFluke.
IFACE = "eth0"

# -------------------- Polling / Timing ----------------------

# Background polling frequency for LLDP/CDP updates.
POLL_INTERVAL_SECONDS = 1

# Timeout for subprocess calls to prevent hanging indefinitely.
SUBPROCESS_TIMEOUT_SECONDS = 3

# Maximum time to remain on "Loading" before displaying a "NO NEIGHBOR" screen.
# This accounts for slow CDP environments (e.g., 45–90 seconds observed).
NO_NEIGHBOR_TIMEOUT_SECONDS = 180

# Minimum time between display refreshes once monitoring VLAN changes.
# This protects the e-paper panel if VLAN rapidly flaps.
MIN_DISPLAY_UPDATE_INTERVAL_SECONDS = 10

# -------------------- E-Paper Hygiene -----------------------

# After this many partial refreshes, force a full refresh to prevent ghosting.
PARTIAL_REFRESH_LIMIT = 8

# ============================================================
# -------------------- LOGGING -------------------------------
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("raspberryfluke")

# ============================================================
# -------------------- FONT PRELOAD / CACHE ------------------
# ============================================================

# Preload all font sizes once at startup to avoid disk I/O on each render.
FONT_CACHE = {
    s: ImageFont.truetype(FONT_PATH, s)
    for s in range(MIN_FONT_SIZE, BASE_FONT_SIZE + 1)
}

# ============================================================
# -------------------- SHARED STATE --------------------------
# ============================================================

# Shared tuple containing the most recently collected data.
# Format: (switch_name, switch_ip, port_name, vlan_id, voice_vlan_id)
current_data = ("Loading", "...", "...", "...", "...")

# Lock protects access to current_data between threads.
data_lock = threading.Lock()

# Event used to notify the display loop when new data is available.
data_event = threading.Event()

# Event used to signal clean shutdown.
shutdown_event = threading.Event()

# ============================================================
# -------------------- UTILITY FUNCTIONS ---------------------
# ============================================================

def run(cmd):
    """
    Execute a subprocess command safely with a timeout.

    Any failure returns an empty string, and errors are logged for diagnostics.
    """
    try:
        return subprocess.check_output(
            cmd,
            stderr=subprocess.DEVNULL,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        ).decode("utf-8", errors="ignore").strip()
    except subprocess.TimeoutExpired:
        log.warning("Command timeout: %s", cmd)
        return ""
    except FileNotFoundError:
        log.error("Command not found: %s", cmd)
        return ""
    except subprocess.CalledProcessError:
        log.warning("Command failed: %s", cmd)
        return ""
    except Exception:
        log.exception("Unexpected subprocess error: %s", cmd)
        return ""

def clean_hostname(name):
    """
    Remove domain suffix from a hostname.
    Example: switch.domain.local -> switch
    """
    return name.split(".")[0] if "." in name else name

def shorten_interface(intf):
    """
    Shorten long Cisco-style interface names so they fit cleanly on the display.
    """
    replacements = {
        "GigabitEthernet": "Gi",
        "TenGigabitEthernet": "Te",
        "FastEthernet": "Fa",
    }
    for long, short in replacements.items():
        if intf.startswith(long):
            return intf.replace(long, short)
    return intf

def fit_font(draw, text, max_width):
    """
    Select the largest preloaded font size that fits within max_width.
    """
    for size in range(BASE_FONT_SIZE, MIN_FONT_SIZE - 1, -1):
        font = FONT_CACHE[size]
        if draw.textlength(text, font=font) <= max_width:
            return font
    return FONT_CACHE[MIN_FONT_SIZE]

def _first_value_for_keys(kv, keys):
    """
    Return the first non-empty value found in kv for a list of possible keys.
    """
    for k in keys:
        v = kv.get(k, "")
        if v:
            return v
    return ""

def _find_first_match_value(kv, pattern):
    """
    Return the first value whose key matches the given regex pattern.
    """
    rx = re.compile(pattern)
    for k, v in kv.items():
        if rx.search(k) and v:
            return v
    return ""

def _normalize_vlan(v):
    """
    Normalize VLAN values to a simple numeric string when possible.
    """
    v = (v or "").strip()
    if not v:
        return "N/A"

    # Some outputs may include extra words; pull the first integer.
    m = re.search(r"\b(\d{1,4})\b", v)
    if m:
        return m.group(1)

    return v

# ============================================================
# -------------------- DISCOVERY PARSING ---------------------
# ============================================================

def parse_lldp_keyvalue():
    """
    Query lldpctl in keyvalue format and return a dictionary of key/value pairs.

    Notes:
    - This includes LLDP and (when enabled) CDP information as normalized by lldpd.
    - Key naming varies slightly by lldpd version and which TLVs the switch advertises.
    """
    kv = {}
    out = run(["lldpctl", "-f", "keyvalue"])
    if not out:
        return kv

    for line in out.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        kv[k.strip()] = v.strip()

    return kv

def extract_switch_hostname(kv):
    """
    Extract the switch/system name from chassis name.
    """
    sw = kv.get(f"lldp.{IFACE}.chassis.name", "")
    sw = clean_hostname(sw) if sw else "N/A"
    return sw

def extract_switch_ip(kv):
    """
    Extract the switch management IP address.

    LLDP advertises management addresses via the "Management Address TLV".
    In lldpctl keyvalue output, this is typically exposed via keys containing:
      - chassis.mgmt-ip
      - chassis.mgmt-ip.<index>  (if multiple addresses are present)

    If the switch doesn't advertise a management address, this will be N/A.
    """
    # Common/likely candidates (some lldpd versions use indexed keys).
    candidates = [
        f"lldp.{IFACE}.chassis.mgmt-ip",
        f"lldp.{IFACE}.chassis.mgmt-ip.0",
        f"lldp.{IFACE}.chassis.mgmt-ip.1",
    ]
    ip = _first_value_for_keys(kv, candidates)

    # More flexible: any key like lldp.eth0.chassis.mgmt-ip.<anything>
    if not ip:
        ip = _find_first_match_value(kv, rf"^lldp\.{re.escape(IFACE)}\.chassis\.mgmt-ip(\.|$)")

    # Basic validation: prefer something that looks like IPv4/IPv6.
    ip = (ip or "").strip()
    if not ip:
        return "N/A"
    return ip

def extract_port(kv):
    """
    Extract the port/interface name.
    """
    port = kv.get(f"lldp.{IFACE}.port.ifname", "") or kv.get(f"lldp.{IFACE}.port.descr", "")
    port = shorten_interface(port) if port else "N/A"
    return port

def extract_native_vlan(kv):
    """
    Extract the native VLAN (PVID) / untagged VLAN ID.

    This is commonly provided as:
      lldp.<iface>.vlan.vlan-id
    """
    vlan = kv.get(f"lldp.{IFACE}.vlan.vlan-id", "") or ""
    return _normalize_vlan(vlan)

def extract_voice_vlan(kv):
    """
    Extract the Voice VLAN.

    Priority order:
      1) LLDP-MED network policy voice VLAN (best/cleanest)
      2) Any CDP/aux/voice-related keyvalue fields exposed by lldpd (varies by build)
      3) Fallback: parse human-readable `lldpctl` output for "Voice VLAN"/"Auxiliary VLAN"

    If nothing is found, return N/A.
    """
    # ---- 1) LLDP-MED policy (most standards-based) ----
    v = _find_first_match_value(
        kv,
        rf"^lldp\.{re.escape(IFACE)}\..*med\.policy.*voice.*vlan"
    )
    if not v:
        v = _find_first_match_value(
            kv,
            rf"^lldp\.{re.escape(IFACE)}\..*med\.policy.*application.*voice.*vlan"
        )
    if v:
        return _normalize_vlan(v)

    # ---- 2) CDP / Cisco Aux VLAN mappings (key names vary) ----
    # Some lldpd builds expose CDP fields in keyvalue output; we search broadly for:
    #   - 'cdp' + ('voice' or 'aux') + 'vlan'
    #   - or any key that includes aux/voice vlan even without explicit 'cdp'
    v = _find_first_match_value(
        kv,
        rf"^lldp\.{re.escape(IFACE)}\..*(cdp).*(voice|aux).*(vlan)"
    )
    if not v:
        v = _find_first_match_value(
            kv,
            rf"^lldp\.{re.escape(IFACE)}\..*(voice|aux).*(vlan)"
        )
    if v:
        return _normalize_vlan(v)

    # ---- 3) Fallback: parse human-readable lldpctl output ----
    # This catches cases where the info exists but isn't exported as keyvalue.
    out = run(["lldpctl"])
    if out:
        # Look for common phrasing seen in Cisco/LLDP-MED outputs.
        # Examples might include:
        #   "Voice VLAN: 2100"
        #   "Auxiliary VLAN: 2100"
        #   "VLAN: 2100 (voice)"
        patterns = [
            r"Voice\s+VLAN\s*:\s*(\d{1,4})",
            r"Auxiliary\s+VLAN\s*:\s*(\d{1,4})",
            r"\bvoice\b.*\bVLAN\b.*?(\d{1,4})",
        ]
        for p in patterns:
            m = re.search(p, out, flags=re.IGNORECASE)
            if m:
                return _normalize_vlan(m.group(1))

    return "N/A"

def get_switch_info():
    """
    Collect switch hostname, switch management IP, port, VLAN, and voice VLAN.
    Returns: (SW, SW_IP, PORT, VLAN, VOICE_VLAN)
    """
    kv = parse_lldp_keyvalue()

    sw = extract_switch_hostname(kv)
    sw_ip = extract_switch_ip(kv)
    port = extract_port(kv)
    vlan = extract_native_vlan(kv)
    voice = extract_voice_vlan(kv)

    return (sw, sw_ip, port, vlan, voice)

def is_data_ready(data):
    """
    Determine whether the essential neighbor information is populated.

    We treat VOICE VLAN as optional, because not all environments advertise LLDP-MED policy.
    """
    sw, _sw_ip, port, vlan, _voice = data
    if sw in ("Loading", "N/A", ""):
        return False
    if port in ("...", "N/A", ""):
        return False
    if vlan in ("...", "N/A", ""):
        return False
    return True

# ============================================================
# -------------------- BACKGROUND POLLER ---------------------
# ============================================================

def data_collector():
    """
    Continuously poll for updated LLDP/CDP information.
    When data changes, update shared state and signal the display loop.
    """
    global current_data
    last = None

    while not shutdown_event.is_set():
        new_data = get_switch_info()

        with data_lock:
            if new_data != current_data:
                current_data = new_data
                data_event.set()

        if last != new_data:
            log.info("Data update: SW=%s SWIP=%s PORT=%s VLAN=%s VOICE=%s", *new_data)
            last = new_data

        time.sleep(POLL_INTERVAL_SECONDS)

# ============================================================
# -------------------- DISPLAY RENDERING ---------------------
# ============================================================

def render_image(data):
    """
    Render the 5-line display layout in the user-requested order:
      1) SW
      2) Switch IP
      3) Port
      4) VLAN
      5) Voice VLAN
    """
    image = Image.new("1", (DISPLAY_WIDTH, DISPLAY_HEIGHT), 255)
    draw = ImageDraw.Draw(image)

    lines = [
        f"SW: {data[0]}",
        f"IP: {data[1]}",
        f"PORT: {data[2]}",
        f"VLAN: {data[3]}",
        f"VOICE: {data[4]}",
    ]

    y = TOP_MARGIN
    max_width = DISPLAY_WIDTH - (LEFT_MARGIN * 2)

    for line in lines:
        font = fit_font(draw, line, max_width)
        draw.text((LEFT_MARGIN, y), line, font=font, fill=0)
        y += font.size + LINE_SPACING

    # Rotates image on screen so that eth0 is on top.
    return image.rotate(180)

def render_no_neighbor():
    """
    Render a display indicating that no neighbor has been learned after timeout.
    """
    return render_image(("NO NEIGHBOR", "N/A", "N/A", "N/A", "N/A"))

# ============================================================
# -------------------- SIGNAL HANDLING -----------------------
# ============================================================

def handle_shutdown(signum, frame):
    """
    Signal handler for clean shutdown (SIGTERM / SIGINT).
    """
    log.info("Shutdown requested (signal %s)", signum)
    shutdown_event.set()
    data_event.set()

# ============================================================
# -------------------- MAIN SERVICE LOOP ---------------------
# ============================================================

def main():
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    epd = epd2in13_V3.EPD()

    partial_refresh_count = 0
    last_display_update_mono = 0.0

    first_ready_displayed = False
    no_neighbor_displayed = False
    boot_start_mono = time.monotonic()
    last_displayed_vlan = None
    last_displayed_voice_vlan = None

    try:
        # Initial boot screen (full refresh).
        epd.init()
        epd.Clear(0xFF)
        boot_img = render_image(("Loading", "...", "...", "...", "..."))
        epd.display(epd.getbuffer(boot_img))
        log.info("Displayed boot screen (Loading)")

        threading.Thread(target=data_collector, daemon=True).start()

        while not shutdown_event.is_set():
            data_event.wait(timeout=1.0)
            data_event.clear()

            with data_lock:
                snap = current_data

            now_mono = time.monotonic()

            # If we have not yet learned a neighbor, keep showing Loading until timeout.
            if not first_ready_displayed:
                if is_data_ready(snap):
                    epd.init()
                    epd.Clear(0xFF)
                    img = render_image(snap)
                    epd.display(epd.getbuffer(img))
                    epd.sleep()

                    first_ready_displayed = True
                    no_neighbor_displayed = False
                    last_displayed_vlan = snap[3]
                    last_displayed_voice_vlan = snap[4]
                    partial_refresh_count = 0
                    last_display_update_mono = now_mono

                    log.info("First neighbor displayed. Now monitoring VLAN changes.")
                    continue

                if (not no_neighbor_displayed) and ((now_mono - boot_start_mono) >= NO_NEIGHBOR_TIMEOUT_SECONDS):
                    epd.init()
                    epd.Clear(0xFF)
                    img = render_no_neighbor()
                    epd.display(epd.getbuffer(img))
                    epd.sleep()

                    no_neighbor_displayed = True
                    partial_refresh_count = 0
                    last_display_update_mono = now_mono

                    log.warning("No neighbor after %ss; displayed NO NEIGHBOR screen.", NO_NEIGHBOR_TIMEOUT_SECONDS)

                continue

            # After initial display, update only when VLAN or VOICE VLAN changes.
            if not is_data_ready(snap):
                continue

            current_vlan = snap[3]
            current_voice = snap[4]

            vlan_changed = (last_displayed_vlan is not None and current_vlan != last_displayed_vlan)
            voice_changed = (last_displayed_voice_vlan is not None and current_voice != last_displayed_voice_vlan)

            if vlan_changed or voice_changed:
                if (now_mono - last_display_update_mono) < MIN_DISPLAY_UPDATE_INTERVAL_SECONDS:
                    continue

                epd.init()
                img = render_image(snap)

                if partial_refresh_count >= PARTIAL_REFRESH_LIMIT:
                    epd.Clear(0xFF)
                    epd.display(epd.getbuffer(img))
                    partial_refresh_count = 0
                    log.info("VLAN/VOICE change: full refresh.")
                else:
                    try:
                        epd.displayPartial(epd.getbuffer(img))
                        partial_refresh_count += 1
                        log.info("VLAN/VOICE change: partial refresh.")
                    except Exception:
                        epd.Clear(0xFF)
                        epd.display(epd.getbuffer(img))
                        partial_refresh_count = 0
                        log.warning("Partial refresh failed; full refresh used.", exc_info=True)

                epd.sleep()

                last_displayed_vlan = current_vlan
                last_displayed_voice_vlan = current_voice
                last_display_update_mono = now_mono

        # Clean shutdown.
        try:
            epd.sleep()
        except Exception:
            pass
        log.info("Exited cleanly.")

    except Exception:
        try:
            epd.sleep()
        except Exception:
            pass
        raise

if __name__ == "__main__":
    main()
