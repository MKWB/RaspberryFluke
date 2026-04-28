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
- Optimized boot time — approximately 15-20 seconds from power to discovery
- No reboot required between port tests
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

Once powered, the Pi boots Raspberry Pi OS Lite (64-bit). Boot time optimizations applied by the installer reduce startup time to approximately 15-20 seconds. A systemd service launches the RaspberryFluke program automatically as soon as the system is ready.

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

### 1. Flash the SD card

Flash **Raspberry Pi OS Lite (64-bit)** to the SD card using [Raspberry Pi Imager](https://www.raspberrypi.com/software/).

During the imaging process, configure the following in Raspberry Pi Imager's settings:
- Set a hostname (e.g. `raspberryfluke`)
- Set a username and password
- Enable SSH

### 2. Boot and update

Insert the SD card, connect power, and SSH into the device. Then update the system:

```bash
sudo apt update && sudo apt upgrade -y
```

### 3. Install git

```bash
sudo apt install git -y
```

### 4. Clone the repository

```bash
sudo git clone https://github.com/MKWB/RaspberryFluke.git /opt/raspberryfluke
```

### 5. Run the installer

```bash
cd /opt/raspberryfluke
sudo bash install.sh
```

### 6. Reboot

```bash
sudo reboot
```

### 7. Verify

```bash
sudo systemctl status raspberryfluke.service
```

The service should show `active (running)`. RaspberryFluke will now start automatically on every boot.

---

## What install.sh Does

### Before starting
- Confirms it is being run as root — refuses to proceed if not
- Confirms RaspberryFluke files exist in `/opt/raspberryfluke` — refuses to proceed if the repo was not cloned first

### Step 1 — System packages
Installs the following via `apt-get`:
- `git` — for cloning repositories
- `python3` and `python3-pip` — Python runtime
- `python3-pil` — image rendering for the e-paper display
- `python3-lgpio` and `python3-rpi.gpio` — GPIO pin control for the e-paper HAT
- `fonts-dejavu-core` — the font used on the display
- `snmp` — provides `snmpget` and `snmpwalk` for active switch discovery

### Step 2 — Boot time optimizations

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

### Step 3 — SPI interface
- Checks if SPI is already enabled
- If not, adds `dtparam=spi=on` to `config.txt`
- SPI is required for the e-paper display to communicate with the Pi

### Step 4 — udev rule
- Creates `/etc/udev/rules.d/99-raspberryfluke-eth0.rules`
- Fires the instant the kernel detects a cable plugged in
- Immediately sets eth0 to up and promiscuous mode before any other service reacts
- Promiscuous mode is required to receive LLDP and CDP multicast frames
- Reloads udev rules immediately

### Step 5 — Journal persistence
- Creates `/var/log/journal/` to enable persistent log storage
- Configures `Storage=persistent` so logs survive reboots and hard power cuts
- Sets `SyncIntervalSec=10s` so at most 10 seconds of logs are lost on a hard power cut

### Step 6 — Waveshare e-Paper library
- Clones the official Waveshare e-Paper repository to `/opt/waveshare-epaper`
- Copies only the Python driver folder (`waveshare_epd/`) into `/opt/raspberryfluke/`
- Deletes the full Waveshare clone after copying

### Step 7 — File permissions
- Sets ownership of all files in `/opt/raspberryfluke/` to root
- Makes `main.py` executable

### Step 8 — Systemd service
- Copies `raspberryfluke.service` to `/etc/systemd/system/`
- Reloads systemd
- Enables the service to start on every boot
- Starts the service immediately

### At the end
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
| `LOG_LEVEL` | `"WARNING"` | Set to `"DEBUG"` for verbose logging during troubleshooting |

After changing `rfconfig.py`, restart the service:

```bash
sudo systemctl restart raspberryfluke.service
```

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