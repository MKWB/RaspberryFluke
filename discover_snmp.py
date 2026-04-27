"""
discover_snmp.py

SNMP-first active switch port discovery using system snmpget/snmpwalk tools.

Uses the system-installed snmpget and snmpwalk binaries via subprocess rather
than a Python SNMP library. This avoids Python version compatibility issues
entirely — the C tools work reliably on any Python version.

Discovery sequence:
  1. Wait for a default gateway IP to appear in the routing table (DHCP).
     If DHCP times out, ask discover_arp for a candidate gateway.
  2. Try each community string in order against the gateway.
  3. For each working community string:
     a. GET sysName to confirm SNMP access and get the switch hostname.
     b. Find which port we are connected to via:
        - CDP-MIB  : walk cdpCacheDeviceId to find "RaspberryFluke"
        - LLDP-MIB : walk lldpRemSysName to find "RaspberryFluke"
        - BRIDGE-MIB: MAC forwarding table lookup with our MAC address
     c. Retrieve port name, VLAN, and voice VLAN via IF-MIB and Cisco MIBs.
  4. Return the first complete result.

Expected performance:
  - DHCP available, SNMP public:    2-5 seconds total
  - DHCP unavailable, ARP fallback: 5-10 seconds total
  - SNMP unavailable:               returns None (passive takes over)

What this file does:
  - Poll the kernel routing table for a default gateway
  - Fall back to ARP observation when DHCP is unavailable
  - Try multiple SNMP community strings automatically
  - Query Cisco CDP-MIB, standard LLDP-MIB, and BRIDGE-MIB for port info
  - Return a normalized neighbor dict

What this file does NOT do:
  - Open raw Ethernet sockets for frame capture (discover_passive.py does that)
  - Send trigger frames (race.py does that)
  - Talk to the display
  - Manage application state
"""

from __future__ import annotations

import logging
import shutil
import socket
import struct
import subprocess
import threading
import time
from typing import Optional

import discover_arp
import parse_utils
import rfconfig


log = logging.getLogger(__name__)


# ============================================================
# Check for snmpget / snmpwalk at import time
# ============================================================

_SNMPGET  = shutil.which("snmpget")
_SNMPWALK = shutil.which("snmpwalk")
_SNMP_AVAILABLE = bool(_SNMPGET and _SNMPWALK)

if not _SNMP_AVAILABLE:
    log.warning(
        "snmpget/snmpwalk not found. SNMP discovery is disabled. "
        "Install with: sudo apt-get install -y snmp"
    )


# ============================================================
# OID constants
# ============================================================

_OID_SYS_NAME            = "1.3.6.1.2.1.1.5.0"
_OID_IF_DESCR            = "1.3.6.1.2.1.2.2.1.2"
_OID_BRIDGE_FDB_PORT     = "1.3.6.1.2.1.17.4.3.1.2"
_OID_BRIDGE_PORT_IFIDX   = "1.3.6.1.2.1.17.1.4.1.2"
_OID_CDP_CACHE_DEVICE_ID = "1.3.6.1.4.1.9.9.23.1.2.1.1.6"
_OID_CDP_NATIVE_VLAN     = "1.3.6.1.4.1.9.9.23.1.2.1.1.11"
_OID_CDP_VOICE_VLAN      = "1.3.6.1.4.1.9.9.23.1.2.1.1.21"
_OID_CISCO_VM_VLAN       = "1.3.6.1.4.1.9.9.68.1.2.2.1.2"
_OID_CISCO_VOICE_VLAN    = "1.3.6.1.4.1.9.9.432.1.1.1.1.2"
_OID_DOT1Q_PVID          = "1.3.6.1.2.1.17.7.1.4.5.1.1"
_OID_VTP_VLAN_STATE      = "1.3.6.1.4.1.9.9.46.1.3.1.1.2"
_OID_LLDP_REM_SYS_NAME  = "1.0.8802.1.1.2.1.4.1.1.9"
_OID_LLDP_LOC_PORT_DESC = "1.0.8802.1.1.2.1.3.7.1.4"


# ============================================================
# Configuration helpers
# ============================================================

def _get_communities() -> list[str]:
    """Build the ordered list of SNMP community strings to try."""
    built_in = list(getattr(rfconfig, "SNMP_COMMUNITY_STRINGS", [
        "public", "cisco", "community", "private",
        "manager", "snmp", "monitor", "readonly",
    ]))
    user = str(getattr(rfconfig, "SNMP_USER_COMMUNITY", "")).strip()
    if user and user not in built_in:
        return [user] + built_in
    return built_in


def _snmp_timeout() -> int:
    try:
        return max(1, int(getattr(rfconfig, "SNMP_TIMEOUT", 1)))
    except (TypeError, ValueError):
        return 1


def _dhcp_wait() -> float:
    try:
        return max(1.0, float(getattr(rfconfig, "SNMP_DHCP_WAIT", 8.0)))
    except (TypeError, ValueError):
        return 8.0


def _arp_wait() -> float:
    try:
        return max(1.0, float(getattr(rfconfig, "SNMP_ARP_WAIT", 3.0)))
    except (TypeError, ValueError):
        return 3.0


# ============================================================
# Candidate IP discovery — ARP probe and DHCP Option 82
# ============================================================

def _get_interface_ip(interface: str) -> Optional[str]:
    """
    Get the IPv4 address of the interface using 'ip addr show'.
    Returns the IP string or None if not yet configured.
    """
    try:
        result = subprocess.run(
            ["ip", "-4", "addr", "show", interface],
            capture_output=True, text=True, timeout=2,
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("inet "):
                return line.split()[1].split("/")[0]
    except Exception:
        pass
    return None


def _build_candidate_ips(gateway: str) -> list[str]:
    """
    Build a list of likely switch management IPs from the gateway.

    On most networks the switch is at .1, .2, or .254 of the subnet.
    We include the gateway itself plus these common alternatives.
    """
    candidates = [gateway]
    try:
        octets = gateway.split(".")
        if len(octets) == 4:
            prefix = ".".join(octets[:3])
            for last in ("1", "2", "254"):
                ip = f"{prefix}.{last}"
                if ip != gateway:
                    candidates.append(ip)
    except Exception:
        pass
    return candidates


def _send_arp_probe(interface: str, target_ip: str) -> None:
    """
    Send a raw ARP request for target_ip on the interface.

    This prompts the switch's L3 interface to respond with its MAC and IP,
    confirming the switch management IP without waiting for DHCP or CDP.
    """
    try:
        our_mac_path = f"/sys/class/net/{interface}/address"
        with open(our_mac_path) as f:
            mac_str = f.read().strip()
        src_mac = bytes(int(x, 16) for x in mac_str.split(":"))
    except Exception:
        return

    try:
        our_ip_str = _get_interface_ip(interface) or "0.0.0.0"
        src_ip = socket.inet_aton(our_ip_str)
        dst_ip = socket.inet_aton(target_ip)
    except Exception:
        return

    # Build ARP request frame.
    # Ethernet header: broadcast dst, our src, EtherType 0x0806
    eth_header = b"\xff\xff\xff\xff\xff\xff" + src_mac + b"\x08\x06"
    # ARP: HTYPE=1(Eth), PTYPE=0x0800(IP), HLEN=6, PLEN=4, OP=1(request)
    arp = struct.pack("!HHBBH", 1, 0x0800, 6, 4, 1)
    arp += src_mac + src_ip + b"\x00\x00\x00\x00\x00\x00" + dst_ip
    frame = eth_header + arp

    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
        sock.bind((interface, 0))
        sock.send(frame)
        sock.close()
        log.debug("ARP probe sent for %s on %s", target_ip, interface)
    except Exception as exc:
        log.debug("ARP probe failed for %s: %s", target_ip, exc)


def _probe_candidate_ips(
    interface:    str,
    gateway:      str,
    cancel_event: threading.Event,
) -> list[str]:
    """
    Send ARP probes to common switch management IPs and return all candidates.

    This is non-blocking — probes are sent immediately and we return
    the full candidate list. The switch will respond asynchronously,
    which discover_arp.py can capture if needed.
    """
    candidates = _build_candidate_ips(gateway)
    for ip in candidates:
        if cancel_event.is_set():
            break
        _send_arp_probe(interface, ip)
    return candidates


def _check_dhcp_option82(interface: str) -> Optional[dict]:
    """
    Check the DHCP lease file for Option 82 relay agent information.

    Option 82 is inserted by DHCP-snooping switches and contains the
    port (circuit ID) and VLAN the DHCP request arrived on. If present,
    this gives us port info without needing CDP, LLDP, or SNMP.

    This is an opportunistic check — most networks do not have Option 82
    configured. Returns None if not found or not parseable.

    The lease file is a raw binary DHCP packet. Option 82 has type 0x52
    and contains sub-options:
        Sub-option 1 (circuit ID): typically encodes port/VLAN info
        Sub-option 2 (remote ID):  typically encodes switch MAC
    """
    # Common dhcpcd lease file locations.
    lease_paths = [
        f"/var/lib/dhcpcd/{interface}.lease",
        f"/var/lib/dhcpcd5/{interface}.lease",
        f"/run/dhcpcd/{interface}.lease",
    ]

    lease_data: Optional[bytes] = None
    for path in lease_paths:
        try:
            with open(path, "rb") as f:
                lease_data = f.read()
            break
        except Exception:
            continue

    if not lease_data:
        return None

    try:
        # DHCP options start at offset 236 (after fixed fields + magic cookie).
        # Find Option 82 (0x52 = 82).
        i = 236
        while i < len(lease_data) - 2:
            opt_type = lease_data[i]
            if opt_type == 0xFF:   # end option
                break
            if opt_type == 0x00:   # pad option
                i += 1
                continue
            opt_len = lease_data[i + 1]
            opt_data = lease_data[i + 2: i + 2 + opt_len]

            if opt_type == 82 and len(opt_data) >= 2:
                log.debug("DHCP Option 82 found in lease file")
                # Parse sub-options looking for readable port info.
                j = 0
                circuit_id = b""
                while j < len(opt_data) - 2:
                    sub_type = opt_data[j]
                    sub_len  = opt_data[j + 1]
                    sub_data = opt_data[j + 2: j + 2 + sub_len]
                    if sub_type == 1:
                        circuit_id = sub_data
                    j += 2 + sub_len

                if circuit_id:
                    # Try to decode as ASCII port info (e.g. "Gi1/0/31").
                    try:
                        port_str = circuit_id.decode("ascii", errors="replace").strip()
                        # Only use if it looks like an interface name.
                        if any(c.isalpha() for c in port_str) and "/" in port_str:
                            log.debug("Option 82 circuit ID decoded as port: %s", port_str)
                            return {"port": port_str}
                    except Exception:
                        pass

            i += 2 + opt_len

    except Exception as exc:
        log.debug("Option 82 parse error: %s", exc)

    return None


# ============================================================
# Gateway IP discovery
# ============================================================

def _read_default_gateway() -> Optional[str]:
    """
    Read the default gateway from the kernel routing table.

    Parses /proc/net/route which is updated by dhcpcd when a DHCP lease
    is obtained. The default route entry has Destination=00000000 and its
    Gateway field is the IP in little-endian hex.
    """
    try:
        with open("/proc/net/route", encoding="ascii") as f:
            for line in f.readlines()[1:]:
                parts = line.strip().split()
                if len(parts) < 3:
                    continue
                if parts[1] == "00000000":
                    gw_int   = int(parts[2], 16)
                    gw_bytes = struct.pack("<I", gw_int)
                    gw_str   = socket.inet_ntoa(gw_bytes)
                    if gw_str != "0.0.0.0":
                        return gw_str
    except Exception as exc:
        log.debug("Could not read /proc/net/route: %s", exc)
    return None


def _wait_for_gateway(
    cancel_event: threading.Event,
    max_wait:     float,
) -> Optional[str]:
    """Poll the routing table until a default gateway appears or timeout."""
    deadline = time.monotonic() + max_wait
    while not cancel_event.is_set() and time.monotonic() < deadline:
        gw = _read_default_gateway()
        if gw:
            log.debug("SNMP: default gateway found: %s", gw)
            return gw
        time.sleep(0.5)
    log.debug("SNMP: no default gateway appeared within %.1fs", max_wait)
    return None


# ============================================================
# SNMP subprocess helpers
# ============================================================

def _snmp_get(
    host:      str,
    community: str,
    oid:       str,
    timeout:   Optional[int] = None,
) -> Optional[str]:
    """
    Run snmpget for a single OID and return the value as a string.

    Uses -Oqv to return the value only without type labels.
    Quotes are stripped from string values automatically.

    Returns None on any error or empty response.
    """
    t = timeout or _snmp_timeout()
    try:
        result = subprocess.run(
            [
                _SNMPGET,
                "-v2c", "-c", community,
                f"-t{t}", "-r1",
                "-Oqv",         # quiet value-only output
                host, oid,
            ],
            capture_output=True,
            text=True,
            timeout=t + 3,
        )
        if result.returncode == 0:
            value = result.stdout.strip().strip('"')
            if value and "No Such" not in value and "No such" not in value:
                return value
    except subprocess.TimeoutExpired:
        log.debug("snmpget timed out: %s %s", host, oid)
    except Exception as exc:
        log.debug("snmpget error: %s %s: %s", host, oid, exc)
    return None


def _snmp_walk(
    host:      str,
    community: str,
    oid:       str,
    timeout:   Optional[int] = None,
    max_rows:  int = 300,
) -> list[tuple[str, str]]:
    """
    Run snmpwalk for a subtree and return (oid_str, value_str) pairs.

    Uses -Oqn to return numeric OIDs and suppress type labels.
    The subprocess is killed after a hard timeout to prevent hangs
    on large tables.

    Returns an empty list on any error.
    """
    t = timeout or _snmp_timeout()
    # Hard subprocess timeout: per-request timeout × generous multiplier.
    proc_timeout = t * 15

    try:
        result = subprocess.run(
            [
                _SNMPWALK,
                "-v2c", "-c", community,
                f"-t{t}", "-r0",
                "-Oqn",         # numeric OIDs, quiet output
                host, oid,
            ],
            capture_output=True,
            text=True,
            timeout=proc_timeout,
        )
        if not result.stdout.strip():
            return []

        rows: list[tuple[str, str]] = []
        for line in result.stdout.strip().splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                rows.append((parts[0], parts[1].strip().strip('"')))
            elif len(parts) == 1:
                rows.append((parts[0], ""))
            if len(rows) >= max_rows:
                break

        return rows

    except subprocess.TimeoutExpired:
        log.debug("snmpwalk timed out: %s %s", host, oid)
    except Exception as exc:
        log.debug("snmpwalk error: %s %s: %s", host, oid, exc)
    return []


# ============================================================
# Port discovery methods
# ============================================================

def _find_port_cdp_mib(
    host:         str,
    community:    str,
    cancel_event: threading.Event,
) -> Optional[dict]:
    """
    Find our port via the Cisco CDP-MIB neighbor table.

    Walks cdpCacheDeviceId and finds the entry containing "RaspberryFluke".
    Since we send CDP triggers on link-up, the switch should add us to its
    CDP neighbor table within ~1-2 seconds of link-up.

    This is the fastest method on Cisco switches where CDP is enabled.
    """
    log.debug("SNMP: trying CDP-MIB port discovery against %s", host)

    rows = _snmp_walk(host, community, _OID_CDP_CACHE_DEVICE_ID, max_rows=100)

    for oid_str, device_id in rows:
        if cancel_event.is_set():
            return None

        if "RaspberryFluke" not in device_id:
            continue

        # OID: .1.3.6.1.4.1.9.9.23.1.2.1.1.6.<ifIndex>.<deviceIndex>
        parts = oid_str.split(".")
        try:
            if_index     = int(parts[-2])
            device_index = int(parts[-1])
        except (IndexError, ValueError):
            log.debug("SNMP: could not parse ifIndex from CDP OID: %s", oid_str)
            continue

        log.debug(
            "SNMP: CDP-MIB found RaspberryFluke at ifIndex=%d deviceIndex=%d",
            if_index, device_index,
        )

        port_name   = _snmp_get(host, community, f"{_OID_IF_DESCR}.{if_index}")
        native_vlan = _snmp_get(host, community, f"{_OID_CDP_NATIVE_VLAN}.{if_index}.{device_index}")
        voice_vlan  = _snmp_get(host, community, f"{_OID_CDP_VOICE_VLAN}.{if_index}.{device_index}")

        return {
            "port_name":  str(port_name  or "").strip(),
            "vlan":       str(native_vlan or "").strip(),
            "voice_vlan": str(voice_vlan  or "").strip(),
        }

    return None


def _find_port_lldp_mib(
    host:         str,
    community:    str,
    cancel_event: threading.Event,
) -> Optional[dict]:
    """
    Find our port via the standard IEEE 802.1AB LLDP-MIB.

    Walks lldpRemSysName and finds the entry for "RaspberryFluke".
    Works on any LLDP-capable switch regardless of vendor.
    """
    log.debug("SNMP: trying LLDP-MIB port discovery against %s", host)

    rows = _snmp_walk(host, community, _OID_LLDP_REM_SYS_NAME, max_rows=100)

    for oid_str, sys_name in rows:
        if cancel_event.is_set():
            return None

        if "RaspberryFluke" not in sys_name:
            continue

        # OID: <prefix>.<timeMark>.<localPortNum>.<remoteIndex>
        parts = oid_str.split(".")
        try:
            local_port_num = int(parts[-2])
        except (IndexError, ValueError):
            continue

        log.debug("SNMP: LLDP-MIB found RaspberryFluke at localPortNum=%d", local_port_num)

        port_desc  = _snmp_get(host, community, f"{_OID_LLDP_LOC_PORT_DESC}.{local_port_num}")
        port_name  = str(port_desc or f"port{local_port_num}").strip()

        # Try VLAN — assume localPortNum equals ifIndex on most switches.
        if_index   = local_port_num
        cisco_vlan = _snmp_get(host, community, f"{_OID_CISCO_VM_VLAN}.{if_index}")
        dot1q_vlan = _snmp_get(host, community, f"{_OID_DOT1Q_PVID}.{if_index}")
        vlan       = str(cisco_vlan or dot1q_vlan or "").strip()

        return {
            "port_name":  port_name,
            "vlan":       vlan,
            "voice_vlan": "",
        }

    return None


def _get_active_vlans(
    host:         str,
    community:    str,
    cancel_event: threading.Event,
) -> list[int]:
    """Get active VLAN IDs via Cisco VTP MIB, falling back to a default range."""
    vlans: list[int] = []

    rows = _snmp_walk(host, community, _OID_VTP_VLAN_STATE, max_rows=1000)
    for oid_str, value in rows:
        if cancel_event.is_set():
            break
        try:
            if int(value) == 1:
                vlan_id = int(oid_str.split(".")[-1])
                if 1 <= vlan_id <= 4094:
                    vlans.append(vlan_id)
        except (ValueError, IndexError):
            continue

    if vlans:
        log.debug("SNMP: found %d active VLANs via Cisco VTP MIB", len(vlans))
        return vlans[:150]

    log.debug("SNMP: VTP VLAN table empty — using default VLAN range 1-20")
    return list(range(1, 21))


def _find_port_bridge_mib(
    host:         str,
    community:    str,
    local_mac:    bytes,
    cancel_event: threading.Event,
) -> Optional[dict]:
    """
    Find our port via the standard BRIDGE-MIB MAC forwarding table.

    On Cisco switches the BRIDGE-MIB is VLAN-context specific. Uses the
    community@vlan notation to query each active VLAN until our MAC is found.
    """
    log.debug("SNMP: trying BRIDGE-MIB port discovery against %s", host)

    mac_oid = ".".join(str(b) for b in local_mac)

    # Try without VLAN context first (works on non-Cisco or VLAN 1 ports).
    bridge_port = _snmp_get(host, community, f"{_OID_BRIDGE_FDB_PORT}.{mac_oid}")
    vlan        = ""

    if bridge_port is None:
        active_vlans = _get_active_vlans(host, community, cancel_event)
        for vlan_id in active_vlans:
            if cancel_event.is_set():
                return None
            vlan_community = f"{community}@{vlan_id}"
            result = _snmp_get(host, vlan_community, f"{_OID_BRIDGE_FDB_PORT}.{mac_oid}")
            if result is not None:
                bridge_port = result
                vlan        = str(vlan_id)
                log.debug("SNMP: BRIDGE-MIB found MAC in VLAN %d, bridge port=%s", vlan_id, bridge_port)
                break

    if bridge_port is None:
        log.debug("SNMP: BRIDGE-MIB could not find our MAC on %s", host)
        return None

    try:
        bridge_port_int = int(bridge_port)
    except (ValueError, TypeError):
        return None

    if_index_raw = _snmp_get(host, community, f"{_OID_BRIDGE_PORT_IFIDX}.{bridge_port_int}")
    if if_index_raw is None:
        return None

    try:
        if_index = int(if_index_raw)
    except (ValueError, TypeError):
        return None

    port_name   = _snmp_get(host, community, f"{_OID_IF_DESCR}.{if_index}")
    cisco_vlan  = _snmp_get(host, community, f"{_OID_CISCO_VM_VLAN}.{if_index}")
    voice_vlan  = _snmp_get(host, community, f"{_OID_CISCO_VOICE_VLAN}.{if_index}")

    if not vlan:
        dot1q_vlan = _snmp_get(host, community, f"{_OID_DOT1Q_PVID}.{if_index}")
        vlan = str(cisco_vlan or dot1q_vlan or "").strip()

    return {
        "port_name":  str(port_name  or "").strip(),
        "vlan":       vlan,
        "voice_vlan": str(voice_vlan or "").strip(),
    }


# ============================================================
# Main query orchestration
# ============================================================

def _query_switch(
    gateway:      str,
    community:    str,
    local_mac:    Optional[bytes],
    cancel_event: threading.Event,
) -> Optional[dict]:
    """
    Attempt a full SNMP discovery against the switch at gateway.

    1. Confirms SNMP access by GETting sysName.
    2. Tries three port discovery methods in order: CDP-MIB, LLDP-MIB,
       BRIDGE-MIB. Returns on the first success.
    """
    # Confirm SNMP access and get switch name.
    sys_name_raw = _snmp_get(gateway, community, _OID_SYS_NAME)
    if not sys_name_raw:
        log.debug("SNMP: community '%s' failed on %s", community, gateway)
        return None

    switch_name = parse_utils.strip_domain(sys_name_raw.strip())
    log.debug("SNMP: community '%s' works on %s — switch: %s", community, gateway, switch_name)

    if cancel_event.is_set():
        return None

    # Give the switch a moment to process our CDP triggers before querying
    # the CDP neighbor table. DHCP latency usually covers this naturally.
    time.sleep(1.0)

    if cancel_event.is_set():
        return None

    # Try port discovery methods in order.
    port_info = _find_port_cdp_mib(gateway, community, cancel_event)

    if port_info is None and not cancel_event.is_set():
        port_info = _find_port_lldp_mib(gateway, community, cancel_event)

    if port_info is None and not cancel_event.is_set() and local_mac:
        port_info = _find_port_bridge_mib(gateway, community, local_mac, cancel_event)

    if port_info is None:
        log.debug("SNMP: could not determine port on %s with community '%s'", gateway, community)
        return None

    port_name = parse_utils.shorten_interface_name(port_info.get("port_name", ""))
    vlan      = parse_utils.normalize_vlan_value(port_info.get("vlan", ""))
    voice     = parse_utils.normalize_vlan_value(port_info.get("voice_vlan", ""))

    if not port_name:
        log.debug("SNMP: port name empty after discovery — discarding result")
        return None

    result = {
        "protocol":    "SNMP",
        "switch_name": switch_name,
        "switch_ip":   gateway,
        "port":        port_name,
        "vlan":        vlan,
        "voice_vlan":  voice,
    }

    log.info(
        "SNMP discovery success | switch=%s ip=%s port=%s vlan=%s voice=%s",
        result["switch_name"],
        result["switch_ip"],
        result["port"],
        result["vlan"],
        result["voice_vlan"],
    )
    return result


# ============================================================
# Public entry point
# ============================================================

def discover(
    interface:    str,
    local_mac:    Optional[bytes],
    cancel_event: threading.Event,
    timeout:      float = 180.0,
) -> Optional[dict]:
    """
    Run SNMP discovery and return the first valid result.

    This is a blocking call intended to run inside a thread managed by
    race.py. Returns as soon as a result is found or all options are
    exhausted.

    Parameters:
        interface    : Ethernet interface name (used only for ARP fallback)
        local_mac    : 6-byte interface MAC, used for BRIDGE-MIB lookup
        cancel_event : set by race.py when another discovery method wins
        timeout      : maximum seconds before returning None

    Returns:
        Normalized neighbor dict or None.
    """
    if not _SNMP_AVAILABLE:
        log.warning("SNMP discovery skipped: snmpget/snmpwalk not found")
        return None

    deadline = time.monotonic() + timeout

    # Phase 1 — Get a gateway IP to query.
    log.debug("SNMP discovery: waiting for default gateway (DHCP)...")
    gateway = _wait_for_gateway(cancel_event, max_wait=_dhcp_wait())

    if not gateway and not cancel_event.is_set():
        log.debug("SNMP discovery: DHCP timeout — trying ARP observation")
        remaining = deadline - time.monotonic()
        arp_wait  = min(_arp_wait(), max(1.0, remaining - 2.0))
        gateway   = discover_arp.get_gateway_candidate(interface, cancel_event, timeout=arp_wait)

    if not gateway:
        log.debug("SNMP discovery: no gateway found — giving up")
        return None

    if cancel_event.is_set():
        return None

    log.debug("SNMP discovery: gateway = %s", gateway)

    # Phase 1b — Check DHCP Option 82 for port info (opportunistic).
    option82 = _check_dhcp_option82(interface)
    if option82:
        log.debug("SNMP: Option 82 data found: %s", option82)

    # Phase 1c — Send ARP probes to common switch management IPs.
    # This is fire-and-forget — probes hit the wire immediately and may
    # cause the switch to populate our ARP cache for faster SNMP access.
    candidates = _probe_candidate_ips(interface, gateway, cancel_event)
    log.debug("SNMP: candidate IPs to try: %s", candidates)

    # Phase 2 — Try SNMP community strings against each candidate IP.
    communities = _get_communities()

    for candidate in candidates:
        if cancel_event.is_set() or time.monotonic() > deadline:
            return None

        for community in communities:
            if cancel_event.is_set() or time.monotonic() > deadline:
                return None

            result = _query_switch(candidate, community, local_mac, cancel_event)
            if result:
                # Merge Option 82 port data if SNMP didn't find a port.
                if option82 and not result.get("port") and option82.get("port"):
                    result["port"] = parse_utils.shorten_interface_name(option82["port"])
                return result

    log.debug("SNMP discovery: all community strings failed on all candidates")
    return None
