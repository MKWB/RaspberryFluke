# RaspberryFluke

A pocket-sized network diagnostic tool that displays switch port information using a Raspberry Pi Zero 2 W, a PoE HAT, and an e-Paper display.

Inspired by the functionality of commercial network port identification tools used by field technicians.

---

## Overview

RaspberryFluke is a portable network diagnostic tool designed to quickly identify switch port information including hostname, IP address, port number, VLAN, and voice VLAN.

The device plugs directly into a switch port via Ethernet. If the port has PoE enabled, it powers on automatically. Results are displayed on a low-power e-Paper screen that retains its image even when power is removed.

The goal was to build a practical, open-source alternative to expensive commercial tools using inexpensive and widely available hardware.

---

## Features

- Powered by PoE or USB power bank — no separate power supply needed
- Multi-path parallel discovery: SNMP, LLDP, and CDP run simultaneously
- Displays switch hostname, IP, port, access VLAN, and voice VLAN
- Protocol indicator shows which discovery method produced the result
- Handles partial results — displays available data even when some fields are missing
- Compatible with Cisco (CDP/LLDP), FortiSwitch, and any LLDP-capable switch
- Low-power e-Paper display retains image with zero power draw
- Optimized boot time — approximately 20-25 seconds from power to discovery
- No reboot required between port tests
- Optional port history logging with three configurable modes
- Read-only filesystem support for SD card protection against hard power cuts
- Automatic service startup via systemd
- Open source

---

## Display Output

```text
         RaspberryFluke        CDP
─────────────────────────────────
SW: SWITCH-01
IP: 10.10.1.2
PORT: Gi1/0/24
VLAN: 120
VOICE: 130
```

---

## Hardware

- Raspberry Pi Zero 2 W
- 40-pin male GPIO header
- Waveshare 2.13" e-Paper HAT+ display (SKU 27467)
- Waveshare PoE Ethernet / USB HUB BOX (SKU 20895)

---

## Software

- Raspberry Pi OS Lite (64-bit)
- Python 3
- SNMP tools (`snmpget`, `snmpwalk`)
- Waveshare EPD drivers
- systemd service for automatic startup

---

## How It Works

Connect the device to an Ethernet cable plugged into an active switch port. If PoE is enabled on the port, the device powers on automatically. If PoE is not available, the device can be powered via an external USB power source such as a power bank.

### Boot

Once powered, the Pi boots Raspberry Pi OS Lite (64-bit). Boot time optimizations applied by the installer reduce startup time to approximately 20-25 seconds. A systemd service launches the RaspberryFluke program automatically as soon as the system is ready.

### Discovery

RaspberryFluke uses a multi-path parallel discovery architecture to identify the connected switch port as quickly as possible. The following methods run simultaneously the moment a link is detected:

**SNMP (primary — fastest)**
If a DHCP lease is obtained, RaspberryFluke immediately queries the switch via SNMP using common community strings. It also sends ARP probes to likely switch management IPs to find the switch directly. If SNMP succeeds, results are returned in 2-5 seconds. DHCP Option 82 relay agent data is also checked opportunistically — if the switch has inserted port and VLAN information into the DHCP response, it is used immediately.

**LLDP Passive Capture (secondary)**
A raw AF_PACKET socket listens for IEEE 802.1AB LLDP frames broadcast by the switch. An LLDP fast-start trigger frame is sent immediately on link-up to prompt LLDP-capable switches to respond right away. Typical response time is 3-8 seconds.

**CDP Passive Capture (fallback)**
A raw AF_PACKET socket listens for Cisco Discovery Protocol frames. A persistent stream of CDP trigger frames is sent throughout the discovery window to maximise the chance of catching the switch's polling cycle. On Cisco CDP-only environments, typical response time is 10-30 seconds.

The first method to return a valid result wins and its data is shown on the display immediately. The others are cancelled. After the initial result is displayed, a background listener continues receiving switch advertisements to keep the data fresh and detect any changes.

### Partial Results

Some switches — notably FortiSwitch — omit optional fields such as the port VLAN ID from their LLDP advertisements. If partial data is received (switch name and port identified but VLAN missing), RaspberryFluke will display the available information after 30 seconds rather than showing nothing. The background listener continues trying to obtain the missing fields and will update the display automatically if complete data arrives.

### Display

The following information is shown on the e-Paper display:

- **SW** — Switch hostname
- **IP** — Switch management IP address
- **PORT** — Port the device is connected to
- **VLAN** — Access VLAN
- **VOICE** — Voice VLAN (if configured)

A small protocol indicator in the top-right corner shows which discovery method produced the result: **SNMP**, **LLDP**, or **CDP**.

### Link Loss

If the cable is unplugged, the display immediately shows "Waiting for link..." and the device resets to listen for the next connection. No reboot is required between port tests.

### No Data Found

If no switch data is received within 120 seconds, the display shows "No active neighbor data." The device continues listening and will update the display automatically if the switch begins advertising.

---

## Installation

### Step 1. Flash the SD card

Flash **Raspberry Pi OS Lite (64-bit)** to the SD card using [Raspberry Pi Imager](https://www.raspberrypi.com/software/).

During the imaging process, configure the following in Raspberry Pi Imager's settings:
- Set a hostname (e.g. `raspberryfluke`)
- Set a username and password
- Enable SSH

### Step 2. Boot and update

Insert the SD card, connect power, and SSH into the device. Then update the system:

```bash
sudo apt update && sudo apt upgrade -y
```

### Step 3. Install git

Git is required to clone the repository. 

```bash
sudo apt install git -y
```

### Step 4. Clone the repository

```bash
sudo git clone https://github.com/MKWB/RaspberryFluke.git /opt/raspberryfluke
```

### Step 5. Run the installer

```bash
cd /opt/raspberryfluke
sudo bash install.sh
```

### Step 6. Reboot

```bash
sudo reboot
```

### Step 7. Verify

```bash
sudo systemctl status raspberryfluke.service
```

The service should show `active (running)`. RaspberryFluke will now start automatically on every boot.


## What install.sh Does

#### Before starting
- Confirms it is being run as root — refuses to proceed if not
- Confirms RaspberryFluke files exist in `/opt/raspberryfluke` — refuses to proceed if the repo was not cloned first

#### Step 1 — System packages
Installs the following via `apt-get`:
- `git` — for cloning repositories
- `python3` and `python3-pip` — Python runtime
- `python3-pil` — image rendering for the e-paper display
- `python3-lgpio` and `python3-rpi.gpio` — GPIO pin control for the e-paper HAT
- `fonts-dejavu-core` — the font used on the display
- `snmp` — provides `snmpget` and `snmpwalk` for active switch discovery

#### Step 2 — Boot time optimizations

Hardware changes written to `/boot/firmware/config.txt`:
- Disables Bluetooth (not needed, saves 3-5 seconds)
- Disables WiFi (not needed, saves 2-3 seconds)
- Enables HDMI blanking (headless device, saves 1-2 seconds)
- Disables the rainbow boot splash screen
- Removes the artificial 1-second boot delay

Kernel command line changes written to `/boot/firmware/cmdline.txt`:
- Redirects console output to tty3 (an unused virtual terminal)
- Adds `quiet loglevel=0` to suppress kernel boot messages
- Removes the serial console (`console=serial0`) which causes delays after Bluetooth is disabled

Services masked (permanently disabled, never start):
- `triggerhappy` — input event daemon, not needed
- `apt-daily` and `apt-daily-upgrade` timers — automatic update checks
- `man-db` timer — man page indexing
- `NetworkManager` and `NetworkManager-wait-online` — replaced by systemd-networkd
- All `cloud-init` services — cloud provisioning tool with no purpose on this device

Network manager replacement:
- Disables NetworkManager (was consuming 13-14 seconds of boot time)
- Enables `systemd-networkd` (starts in under 200ms)
- Creates `/etc/systemd/network/10-eth0.network` — configures DHCP on eth0

Cloud-init lockout:
- Creates `/etc/cloud/cloud-init.disabled` — prevents cloud-init from running even if service masks are removed

#### Step 3 — SPI interface
- Checks if SPI is already enabled
- If not, adds `dtparam=spi=on` to `config.txt`
- SPI is required for the e-paper display to communicate with the Pi

#### Step 4 — udev rule
- Creates `/etc/udev/rules.d/99-raspberryfluke-eth0.rules`
- Fires the instant the kernel detects a cable plugged in
- Immediately sets eth0 to up and promiscuous mode before any other service reacts
- Promiscuous mode is required to receive LLDP and CDP multicast frames
- Reloads udev rules immediately

#### Step 5 — Journal persistence
- Creates `/var/log/journal/` to enable persistent log storage
- Configures `Storage=persistent` so logs survive reboots and hard power cuts
- Sets `SyncIntervalSec=10s` so at most 10 seconds of logs are lost on a hard power cut

#### Step 6 — Waveshare e-Paper library
- Clones the official Waveshare e-Paper repository to `/opt/waveshare-epaper`
- Copies only the Python driver folder (`waveshare_epd/`) into `/opt/raspberryfluke/`
- Deletes the full Waveshare clone after copying

#### Step 7 — File permissions
- Sets ownership of all files in `/opt/raspberryfluke/` to root
- Makes `main.py` executable

#### Step 8 — Data directory
- Creates `/data/raspberryfluke/` for port history storage
- This directory is used by history logging (modes 1 and 2)
- On devices with read-only filesystem enabled, this directory is backed by a persistent image file on the boot partition

#### Step 9 — Systemd service
- Copies `raspberryfluke.service` to `/etc/systemd/system/`
- Reloads systemd
- Enables the service to start on every boot
- Starts the service immediately

#### At the end
- Prints a completion summary
- Warns that a reboot is required for hardware changes to take effect

---

## Configuration

Optional settings can be adjusted in `/opt/raspberryfluke/rfconfig.py`:

| Setting | Default | Description |
|---|---|---|
| `SNMP_USER_COMMUNITY` | `""` | Try this community string first before the built-in list |
| `DISCOVERY_TIMEOUT` | `120.0` | Seconds before showing "No active neighbor data" |
| `RESULT_REVEAL_DELAY` | `1.5` | Minimum seconds "Scanning..." is shown before data replaces it |
| `PARTIAL_DISPLAY_DELAY` | `30.0` | Seconds to wait before displaying partial results |
| `PORT_HISTORY_MODE` | `0` | History logging mode: 0=off, 1=port history, 2=debug log |
| `PORT_HISTORY_LIMIT` | `50` | Maximum number of entries kept in mode 1 |
| `PORT_HISTORY_PATH` | `"/data/raspberryfluke"` | Directory where history files are written |
| `LOG_LEVEL` | `"WARNING"` | Set to `"DEBUG"` for verbose logging during troubleshooting |

After changing `rfconfig.py`, restart the service:

```bash
sudo systemctl restart raspberryfluke.service
```

---

## Port History Logging

RaspberryFluke can optionally record every port discovery result to disk. This is useful for technicians who need a record of which ports were tested during a job.

### Enabling history logging

Edit `/opt/raspberryfluke/rfconfig.py` and set `PORT_HISTORY_MODE`:

```python
PORT_HISTORY_MODE = 1   # Record last 50 port results
```

Restart the service:

```bash
sudo systemctl restart raspberryfluke.service
```

### Modes

**Mode 0 — Off (default)**
No history is recorded. No disk writes from application data. Recommended for normal use and fully compatible with the read-only filesystem.

**Mode 1 — Port History**
Records the last `PORT_HISTORY_LIMIT` port discovery results (default 50) to `/data/raspberryfluke/history.jsonl`. Each entry contains a timestamp, the discovery protocol used, and all switch data. Oldest entries are automatically dropped when the limit is reached.

**Mode 2 — Debug Log**
Writes verbose rotating log entries to `/data/raspberryfluke/debug.log`. The file rotates at 5MB and three backups are kept. Useful for field troubleshooting without a live SSH session.

### Reading the history log

SSH into the device and run:

```bash
cat /data/raspberryfluke/history.jsonl
```

For a formatted, readable view of each entry:

```bash
cat /data/raspberryfluke/history.jsonl | python3 -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if line:
        print(json.dumps(json.loads(line), indent=2))
        print()
"
```

Example output:

```json
{
  "timestamp": "2026-04-30 15:18:09",
  "protocol": "CDP",
  "switch_name": "S4-TENFLO-MDF",
  "switch_ip": "10.126.0.4",
  "port": "Gi1/0/31",
  "vlan": "2001",
  "voice_vlan": "3032"
}
```

> **Note:** The Pi Zero 2W has no hardware real-time clock. Timestamps are set by NTP after the device gets a network connection. On networks without internet access, timestamps may not be accurate.

---

## Read-Only Filesystem (Recommended)

RaspberryFluke is designed to be unplugged from PoE at any moment without warning. On a standard read-write filesystem, a hard power cut during a disk write can corrupt the SD card over time. Enabling the read-only filesystem eliminates this risk entirely.

When read-only is enabled:
- The root filesystem (OS and code) is mounted read-only — the SD card cannot be corrupted by a hard power cut
- A 256MB writable image file (`/boot/firmware/rfdata.img`) is mounted at `/data` for port history storage
- All other runtime writes (logs, temp files, DHCP leases) go to RAM and are lost on power cut — this is safe and intentional
- The device boots and operates identically from the user's perspective

### Enabling read-only

Run this **once** after `install.sh` has completed and the device is confirmed working. Use a stable power source — **not PoE** — while this script runs.

```bash
sudo bash /opt/raspberryfluke/make_readonly.sh
```

Type `YES` when prompted. The script will configure the filesystem, create the writable data image, and reboot automatically.

### Verifying read-only is active

After rebooting, confirm the root filesystem is mounted read-only:

```bash
mount | grep "on / "
```

The output should include `ro` in the mount options:

/dev/mmcblk0p2 on / type ext4 (ro,noatime)

Also confirm `/data` is writable:

```bash
mount | grep "/data"
```

Should show:

/boot/firmware/rfdata.img on /data type ext4 (rw,noatime)

### Updating the device after read-only is enabled

To pull code updates, temporarily remount the root filesystem as writable:

```bash
sudo remount-rw
cd /opt/raspberryfluke
sudo git pull
sudo remount-ro
sudo reboot
```

Always reboot after remounting read-only to ensure a clean state.

### How the writable /data area works

Rather than creating a new partition (which would require shrinking the root partition — a risky operation), RaspberryFluke uses a file-based approach. A 256MB ext4 image file is created at `/boot/firmware/rfdata.img`. The boot partition is always writable even when the root is read-only, so this file persists safely. Linux mounts it as a loop device at `/data` on every boot. The end result is identical to a dedicated partition from the application's perspective.

---

## Troubleshooting

**View live logs:**
```bash
sudo journalctl -u raspberryfluke.service -f
```

**View logs from the last boot:**
```bash
sudo journalctl -u raspberryfluke.service -b 0 --no-pager
```

**Restart the service:**
```bash
sudo systemctl restart raspberryfluke.service
```

**Check service status:**
```bash
sudo systemctl status raspberryfluke.service
```

**Check boot time breakdown:**
```bash
systemd-analyze blame | head -20
```

**Confirm read-only filesystem is active:**
```bash
mount | grep "on / "
```

**Read port history:**
```bash
cat /data/raspberryfluke/history.jsonl
```

**Temporarily make root writable for updates:**
```bash
sudo remount-rw
```

**Restore read-only after updates:**
```bash
sudo remount-ro
sudo reboot
```