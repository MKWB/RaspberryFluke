# RaspberryFluke

Pocket network diagnostic tool that displays LLDP/CDP switch data using a Raspberry Pi Zero 2 W, a PoE HAT, and an E-Paper display.

---

## Overview

This project is a pocket-sized network diagnostic tool designed to quickly identify switch port information such as hostname, IP address, port number, VLAN, and voice VLAN using LLDP/CDP.  

The device runs on a Raspberry Pi Zero 2 W and displays results on an e-Paper display, making it useful for technicians deploying or troubleshooting network equipment in the field.

---

## Why This Exists


Commercial network diagnostic tools that provide quick switch port identification can be expensive. This project explores how a small Linux-based device can extract useful switch information using LLDP/CDP and display it on a low-power screen.

The goal was to build a simple, practical tool using inexpensive and widely available hardware.

---

## Features

- Runs on Raspberry Pi Zero 2 W 
- Detects switch hostname
- Detects switch IP address
- Identifies switch port
- Displays access VLAN
- Displays voice VLAN
- Low power E-Paper display
- Fast boot and automatic detection
- Powered by PoE via a PoE HAT or a USB power bank

---

## Display Output

SW: SWITCH-01  
IP: 10.10.1.2  
PORT: Gi1/0/24  
VLAN: 120  
VOICE: 130

---

## Hardware

- Raspberry Pi Zero 2 W
- 40-pin Male GPIO Header
- Waveshare 2.13" E-Paper HAT+ display (SKU 27467)
- Waveshare PoE Ethernet / USB HUB BOX (SKU 20895)

---

## Software

- Raspberry Pi OS
- Python
- LLDP/CDP parsing
- Waveshare EPD drivers
- systemd service for automatic startup

---

## How It Works

Connect the device to an Ethernet cable connected to an active switch.

If PoE is enabled on the port, the device powers on automatically. If PoE is not available, the device can be powered using an external power source such as a USB power bank.

Once powered on, the Raspberry Pi boots into Raspberry Pi OS. A systemd service automatically launches the Python script which listens for LLDP/CDP packets transmitted by the switch.

The script extracts relevant switch information such as hostname, IP address, port number, VLAN, and voice VLAN. The data is then formatted and displayed on the e-Paper screen. 

---

## Installation

1. Flash Raspberry Pi OS to the SD card using Raspberry Pi Imager.

2. Boot the Raspberry Pi and update the system:

sudo apt update
sudo apt upgrade -y

3. Install required packages:

sudo apt -y install git lldpd python3 python3-pip python3-pil python3-lgpio python3-rpi.gpio

4. Enable SPI (required for the E-Paper display):

sudo raspi-config

Navigate to:
Interface Options -> SPI -> Enable

5. Reboot the device

sudo reboot

6. Configure lldpd for CDP and receive-only mode:

sudo nano /etc/default/lldpd

Set:

DAEMON_ARGS="-r -c"
LLDPD_OPTIONS=""

7. Restart lldpd:

sudo systemctl restart lldpd

8. Clone the RaspberryFluke repository into /opt:

sudo rm -rf /opt/raspberryfluke
sudo git clone https://github.com/YOURUSERNAME/raspberryfluke.git /opt/raspberryfluke
sudo chown -R root:root /opt/raspberryfluke

9. Clone the Waveshare repository:

cd ~
git clone https://github.com/waveshare/e-Paper.git

10. Copy the waveshare_epd library into /opt/raspberryfluke:

sudo cp -r ~/e-Paper/RaspberryPi_JetsonNano/python/lib/waveshare_epd /opt/raspberryfluke/

11. Make the script executable:

sudo chmod 755 /opt/raspberryfluke/raspberryfluke.py

12. Create the systemd service file:

sudo nano /etc/systemd/system/raspberryfluke.service

Paste the following:

[Unit]
Description=RaspberryFluke LLDP/CDP E-Paper Display
After=network.target lldpd.service
Wants=lldpd.service

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=/opt/raspberryfluke
Environment=PYTHONPATH=/opt/raspberryfluke
ExecStart=/usr/bin/python3 /opt/raspberryfluke/raspberryfluke.py
Restart=on-failure
RestartSec=3

#Systemd hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectControlGroups=true
ProtectKernelModules=true
ProtectKernelTunables=true
LockPersonality=true
RestrictSUIDSGID=true
RemoveIPC=true
RestrictAddressFamilies=AF_UNIX AF_NETLINK

[Install]
WantedBy=multi-user.target

13. Enable and start the service:

sudo systemctl daemon-reload
sudo systemctl enable raspberryfluke.service
sudo systemctl start raspberryfluke.service

14. Verify the service is running:

sudo systemctl status raspberryfluke.service

The RaspberryFluke script will now run automatically each time the device boots.
