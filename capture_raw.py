"""
capture_raw.py

Raw Ethernet frame capture for LLDP and CDP neighbor discovery.

Uses AF_PACKET raw sockets to capture frames directly from the wire
without going through lldpd or any other daemon. Frames are processed
the moment they arrive rather than on a polling schedule.

What this file does:
- Open a raw AF_PACKET socket on the specified interface
- Wait for LLDP or CDP Ethernet frames using select()
- Identify frame type by destination MAC and Ethertype
- Return raw frame bytes and protocol type to the caller

What this file does NOT do:
- Parse frame contents (that belongs in parse_lldp_raw and parse_cdp_raw)
- Send frames (that belongs in trigger.py)
- Update application state
- Talk to the display
"""

from __future__ import annotations

import logging
import select
import socket
import struct


log = logging.getLogger(__name__)

# ETH_P_ALL captures every Ethernet frame on the interface regardless of
# protocol type. We filter for LLDP and CDP ourselves in Python.
_ETH_P_ALL = 0x0003

# LLDP uses Ethertype 0x88CC (Ethernet II) and a reserved multicast MAC.
_LLDP_ETHERTYPE = 0x88CC
_LLDP_DST_MAC   = b"\x01\x80\xc2\x00\x00\x0e"

# CDP uses 802.3 frames (not Ethernet II) with LLC/SNAP encapsulation.
# We identify CDP frames by destination MAC alone because 802.3 frames
# do not carry an Ethertype in the normal position.
_CDP_DST_MAC = b"\x01\x00\x0c\xcc\xcc\xcc"

# Discard anything shorter than a standard Ethernet header.
_MIN_FRAME_SIZE = 14


def _is_lldp_frame(frame: bytes) -> bool:
    """
    Return True if this Ethernet frame is an LLDP frame.

    LLDP frames are Ethernet II frames with Ethertype 0x88CC
    sent to the LLDP multicast MAC 01:80:C2:00:00:0E.
    """
    if len(frame) < _MIN_FRAME_SIZE:
        return False

    dst_mac   = frame[0:6]
    ethertype = struct.unpack("!H", frame[12:14])[0]

    return dst_mac == _LLDP_DST_MAC and ethertype == _LLDP_ETHERTYPE


def _is_cdp_frame(frame: bytes) -> bool:
    """
    Return True if this Ethernet frame is a CDP frame.

    CDP frames are sent to Cisco's multicast MAC 01:00:0C:CC:CC:CC.
    We match on destination MAC only because CDP uses 802.3 framing
    where the two bytes at offset 12 carry the frame length, not an
    Ethertype, making Ethertype matching unreliable for CDP.
    """
    if len(frame) < _MIN_FRAME_SIZE:
        return False

    return frame[0:6] == _CDP_DST_MAC


class RawCapture:
    """
    Raw Ethernet frame capture session for one network interface.

    Open one instance when link comes up and close it when link goes
    down. The same socket is reused across many receive_frame() calls
    so there is no per-frame socket creation overhead.

    Usage:
        with RawCapture("eth0") as cap:
            protocol, frame = cap.receive_frame(timeout=2.0)
    """

    def __init__(self, interface: str) -> None:
        self.interface = interface
        self._sock: socket.socket | None = None

    def open(self) -> bool:
        """
        Open the raw AF_PACKET socket on the interface.

        Returns True if successful.
        Returns False if the socket could not be opened (logged as error).

        Requires root privileges or CAP_NET_RAW. The systemd service file
        runs as root and includes AF_PACKET in RestrictAddressFamilies.
        """
        try:
            self._sock = socket.socket(
                socket.AF_PACKET,
                socket.SOCK_RAW,
                socket.htons(_ETH_P_ALL),
            )
            self._sock.bind((self.interface, 0))
            log.debug("Raw capture socket opened on %s", self.interface)
            return True

        except PermissionError:
            log.error(
                "Permission denied opening raw socket on %s. "
                "The service must run as root or have CAP_NET_RAW.",
                self.interface,
            )
            return False

        except OSError as exc:
            log.error(
                "Could not open raw socket on %s: %s",
                self.interface,
                exc,
            )
            return False

    def close(self) -> None:
        """
        Close the raw socket and release system resources.
        """
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
            log.debug("Raw capture socket closed on %s", self.interface)

    def receive_frame(
        self,
        timeout: float = 2.0,
    ) -> tuple[str, bytes] | tuple[None, None]:
        """
        Wait for one LLDP or CDP frame and return it immediately.

        Uses select() with the given timeout so the caller's main loop
        remains responsive to shutdown signals and carrier changes even
        when no frames are arriving on the wire.

        Non-LLDP and non-CDP frames are silently discarded. Only the
        first matching frame per call is returned — the loop in main.py
        calls this repeatedly to collect subsequent frames.

        Parameters:
            timeout:
                Maximum seconds to block waiting for a frame.
                After this time, returns (None, None) so the caller
                can check carrier state and the shutdown event.

        Returns:
            ("lldp", frame_bytes) for an LLDP frame.
            ("cdp",  frame_bytes) for a CDP frame.
            (None, None) if the timeout elapsed or an error occurred.
        """
        if self._sock is None:
            log.error("receive_frame called but raw socket is not open")
            return None, None

        deadline = timeout

        while deadline > 0:
            try:
                ready, _, _ = select.select([self._sock], [], [], min(deadline, 2.0))
            except Exception as exc:
                log.debug("select() error on %s: %s", self.interface, exc)
                return None, None

            if not ready:
                return None, None

            try:
                frame = self._sock.recv(65535)
            except Exception as exc:
                log.debug("recv() error on %s: %s", self.interface, exc)
                return None, None

            if _is_lldp_frame(frame):
                log.debug(
                    "LLDP frame received on %s (%d bytes)",
                    self.interface,
                    len(frame),
                )
                return "lldp", frame

            if _is_cdp_frame(frame):
                log.debug(
                    "CDP frame received on %s (%d bytes)",
                    self.interface,
                    len(frame),
                )
                return "cdp", frame

            # Non-matching frame (e.g. ARP, STP). Discard and keep waiting.
            deadline -= 0.001

        return None, None

    def __enter__(self) -> "RawCapture":
        self.open()
        return self

    def __exit__(self, *_) -> None:
        self.close()
