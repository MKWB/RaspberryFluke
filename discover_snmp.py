"""
discover_snmp.py

SNMP-first active switch port discovery.

This is the primary and fastest discovery path. Rather than waiting for the
switch to broadcast CDP or LLDP advertisements (which can take 20-60 seconds),
this module actively queries the switch via SNMP the moment a gateway IP is
available from DHCP or ARP observation.

Discovery sequence:
  1. Wait for a default gateway IP to appear in the routing table (DHCP).
     If DHCP times out, ask discover_arp for a candidate gateway.
  2. Try each community string in order against the gateway.
  3. For each working community string:
     a. GET sysName to confirm SNMP access and get the switch hostname.
     b. Find which port we are connected to via:
        - CDP-MIB  : walk cdpCacheDeviceId to find "RaspberryFluke"
        - LLDP-MIB : walk lldpRemSysName to find "RaspberryFluke"
        - BRIDGE-MIB: walk MAC forwarding table to find our MAC address
     c. Retrieve port name, VLAN, and voice VLAN via IF-MIB and Cisco MIBs.
  4. Return the first complete result.

Expected performance:
  - DHCP available, SNMP public:   2-5 seconds total
  - DHCP unavailable, ARP fallback: 5-10 seconds total
  - SNMP unavailable:              returns None (passive takes over)

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
import socket
import struct
import threading
import time
from typing import Optional

import discover_arp
import parse_utils
import rfconfig


log = logging.getLogger(__name__)


# ============================================================
# SNMP import — pysnmp-lextudio
# ============================================================

# ============================================================
# SNMP import — supports pysnmp 4.x (classic hlapi) and 6.x (asyncio sync)
# ============================================================

import warnings as _warnings

_SNMP_AVAILABLE = False

# Attempt 1: classic hlapi API (pysnmp 4.x, pysnmp-lextudio).
# The RuntimeWarning from pysnmp-lextudio's deprecation notice is suppressed
# here — it does not indicate a failure, just that the package recommends
# switching to pysnmp directly.
with _warnings.catch_warnings():
    _warnings.simplefilter("ignore", RuntimeWarning)
    try:
        from pysnmp.hlapi import (
            CommunityData,
            ContextData,
            ObjectIdentity,
            ObjectType,
            SnmpEngine,
            UdpTransportTarget,
            getCmd,
            nextCmd,
        )
        _SNMP_AVAILABLE = True
    except ImportError:
        pass

# Attempt 2: modern pysnmp 6.x sync API.
if not _SNMP_AVAILABLE:
    try:
        from pysnmp.hlapi.v3arch.asyncio.sync import (  # type: ignore[no-redef]
            CommunityData,
            ContextData,
            ObjectIdentity,
            ObjectType,
            SnmpEngine,
            UdpTransportTarget,
            getCmd,
            nextCmd,
        )
        _SNMP_AVAILABLE = True
    except ImportError:
        pass

if not _SNMP_AVAILABLE:
    log.warning(
        "pysnmp is not installed. SNMP discovery is disabled. "
        "Install it with: pip install pysnmp --break-system-packages"
    )


# ============================================================
# OID constants
# ============================================================

# Standard MIBs
_OID_SYS_NAME       = "1.3.6.1.2.1.1.5.0"   # sysName
_OID_SYS_DESCR      = "1.3.6.1.2.1.1.1.0"   # sysDescr
_OID_IF_DESCR       = "1.3.6.1.2.1.2.2.1.2" # ifDescr.<ifIndex>

# BRIDGE-MIB (dot1dTp)
_OID_BRIDGE_FDB_PORT    = "1.3.6.1.2.1.17.4.3.1.2"  # dot1dTpFdbPort.<mac>
_OID_BRIDGE_PORT_IFIDX  = "1.3.6.1.2.1.17.1.4.1.2"  # dot1dBasePortIfIndex.<port>

# Cisco CDP-MIB (cdpCache)
_OID_CDP_CACHE_DEVICE_ID   = "1.3.6.1.4.1.9.9.23.1.2.1.1.6"   # cdpCacheDeviceId
_OID_CDP_CACHE_NATIVE_VLAN = "1.3.6.1.4.1.9.9.23.1.2.1.1.11"  # cdpCacheNativeVLAN
_OID_CDP_CACHE_VOICE_VLAN  = "1.3.6.1.4.1.9.9.23.1.2.1.1.21"  # cdpCacheVoiceVLAN

# Cisco VLAN Membership MIB
_OID_CISCO_VM_VLAN     = "1.3.6.1.4.1.9.9.68.1.2.2.1.2"   # vmVlan.<ifIndex>
_OID_CISCO_VOICE_VLAN  = "1.3.6.1.4.1.9.9.432.1.1.1.1.2"  # cVoiceVlanIfVoiceVlan.<ifIndex>

# Q-BRIDGE-MIB
_OID_DOT1Q_PVID        = "1.3.6.1.2.1.17.7.1.4.5.1.1"     # dot1qPvid.<ifIndex>

# Cisco VTP VLAN state (for VLAN list)
_OID_VTP_VLAN_STATE    = "1.3.6.1.4.1.9.9.46.1.3.1.1.2"   # vtpVlanState

# LLDP-MIB (IEEE 802.1AB)
_OID_LLDP_REM_SYS_NAME = "1.0.8802.1.1.2.1.4.1.1.9"       # lldpRemSysName
_OID_LLDP_LOC_PORT_DESC = "1.0.8802.1.1.2.1.3.7.1.4"      # lldpLocPortDesc.<portNum>


# ============================================================
# Configuration helpers
# ============================================================

def _get_communities() -> list[str]:
    """
    Build the ordered list of SNMP community strings to try.

    The user-configured string (if set) is always tried first.
    """
    built_in = list(getattr(rfconfig, "SNMP_COMMUNITY_STRINGS", [
        "public", "cisco", "community", "private",
        "manager", "snmp", "monitor", "readonly",
    ]))

    user = str(getattr(rfconfig, "SNMP_USER_COMMUNITY", "")).strip()
    if user and user not in built_in:
        return [user] + built_in
    return built_in


def _snmp_timeout() -> float:
    try:
        return max(0.5, float(getattr(rfconfig, "SNMP_TIMEOUT", 2.0)))
    except (TypeError, ValueError):
        return 2.0


def _snmp_retries() -> int:
    try:
        return max(0, int(getattr(rfconfig, "SNMP_RETRIES", 1)))
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
# Gateway IP discovery
# ============================================================

def _read_default_gateway() -> Optional[str]:
    """
    Read the default gateway from the kernel routing table.

    Parses /proc/net/route, which is updated by dhcpcd when a DHCP
    lease is obtained. The default route entry has Destination = 00000000
    and its Gateway field is the gateway IP in little-endian hex.

    Returns an IPv4 address string or None if no default route exists.
    """
    try:
        with open("/proc/net/route", encoding="ascii") as f:
            for line in f.readlines()[1:]:
                parts = line.strip().split()
                if len(parts) < 3:
                    continue
                if parts[1] == "00000000":   # default route
                    gw_int   = int(parts[2], 16)
                    gw_bytes = struct.pack("<I", gw_int)   # little-endian
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
    """
    Poll the routing table until a default gateway appears or timeout.

    Returns the gateway IP string or None.
    """
    deadline = time.monotonic() + max_wait
    poll_interval = 0.5

    while not cancel_event.is_set() and time.monotonic() < deadline:
        gw = _read_default_gateway()
        if gw:
            log.debug("SNMP: default gateway found via routing table: %s", gw)
            return gw
        time.sleep(poll_interval)

    log.debug("SNMP: no default gateway appeared within %.1fs", max_wait)
    return None


# ============================================================
# SNMP session helper
# ============================================================

class _Session:
    """
    Thin wrapper around pysnmp hlapi for one (host, community) pair.

    One instance is created per community string attempt inside
    _query_switch(). The SnmpEngine is created once and reused across
    all GETs and walks within the same session for efficiency.
    """

    def __init__(self, host: str, community: str) -> None:
        self.host      = host
        self.community = community
        self._engine   = SnmpEngine()
        self._timeout  = _snmp_timeout()
        self._retries  = _snmp_retries()

    def _transport(self, community: Optional[str] = None) -> "UdpTransportTarget":
        return UdpTransportTarget(
            (self.host, 161),
            timeout=self._timeout,
            retries=self._retries,
        )

    def _community_data(self, community: Optional[str] = None) -> "CommunityData":
        return CommunityData(community or self.community, mpModel=1)

    def get(self, oid: str, community: Optional[str] = None) -> Optional[object]:
        """
        SNMP GET for a single scalar OID.

        Returns the raw pysnmp value object, or None on any error.
        The caller converts to str/int as needed.
        """
        try:
            errorIndication, errorStatus, errorIndex, varBinds = next(
                getCmd(
                    self._engine,
                    self._community_data(community),
                    self._transport(),
                    ContextData(),
                    ObjectType(ObjectIdentity(oid)),
                )
            )
            if errorIndication or errorStatus:
                return None
            for varBind in varBinds:
                return varBind[1]
        except Exception as exc:
            log.debug("SNMP GET %s: %s", oid, exc)
        return None

    def walk(
        self,
        oid:       str,
        max_rows:  int = 300,
        community: Optional[str] = None,
    ):
        """
        SNMP GETNEXT walk of a subtree.

        Yields (oid_str, value) pairs. Stops at the end of the subtree
        or after max_rows rows. Any error terminates the walk silently.
        """
        try:
            for errorIndication, errorStatus, errorIndex, varBinds in nextCmd(
                self._engine,
                self._community_data(community),
                self._transport(),
                ContextData(),
                ObjectType(ObjectIdentity(oid)),
                lexicographicMode=False,
                maxRows=max_rows,
            ):
                if errorIndication or errorStatus:
                    return
                for varBind in varBinds:
                    yield str(varBind[0]), varBind[1]
        except Exception as exc:
            log.debug("SNMP walk %s: %s", oid, exc)


# ============================================================
# Port discovery methods
# ============================================================

def _find_port_cdp_mib(
    session:      _Session,
    cancel_event: threading.Event,
) -> Optional[dict]:
    """
    Find our port via the Cisco CDP-MIB neighbor table.

    Walks cdpCacheDeviceId and finds the entry where the device ID
    contains "RaspberryFluke". Since we send CDP triggers on link-up,
    the switch should add us to its CDP neighbor table within ~1-2 seconds.

    This is the fastest method on Cisco switches where CDP is enabled.

    Returns a partial port info dict or None.
    """
    log.debug("SNMP: trying CDP-MIB port discovery against %s", session.host)

    for oid_str, value in session.walk(_OID_CDP_CACHE_DEVICE_ID, max_rows=200):
        if cancel_event.is_set():
            return None

        device_id = str(value).strip()
        if "RaspberryFluke" not in device_id:
            continue

        # OID = <prefix>.<ifIndex>.<deviceIndex>
        # e.g. 1.3.6.1.4.1.9.9.23.1.2.1.1.6.31.1 → ifIndex=31, deviceIndex=1
        parts = oid_str.split(".")
        try:
            if_index     = int(parts[-2])
            device_index = int(parts[-1])
        except (IndexError, ValueError):
            log.debug("SNMP: could not parse ifIndex from CDP OID: %s", oid_str)
            continue

        log.debug(
            "SNMP: CDP-MIB found RaspberryFluke at ifIndex=%d deviceIndex=%d",
            if_index,
            device_index,
        )

        # Get port name, native VLAN, and voice VLAN using the ifIndex.
        port_name   = session.get(f"{_OID_IF_DESCR}.{if_index}")
        native_vlan = session.get(f"{_OID_CDP_CACHE_NATIVE_VLAN}.{if_index}.{device_index}")
        voice_vlan  = session.get(f"{_OID_CDP_CACHE_VOICE_VLAN}.{if_index}.{device_index}")

        return {
            "port_name":   str(port_name  or "").strip(),
            "vlan":        str(native_vlan or "").strip(),
            "voice_vlan":  str(voice_vlan  or "").strip(),
        }

    return None


def _find_port_lldp_mib(
    session:      _Session,
    cancel_event: threading.Event,
) -> Optional[dict]:
    """
    Find our port via the standard IEEE 802.1AB LLDP-MIB.

    Walks lldpRemSysName and finds the entry for "RaspberryFluke".
    Works on any LLDP-capable switch regardless of vendor.

    Returns a partial port info dict or None.
    """
    log.debug("SNMP: trying LLDP-MIB port discovery against %s", session.host)

    for oid_str, value in session.walk(_OID_LLDP_REM_SYS_NAME, max_rows=200):
        if cancel_event.is_set():
            return None

        sys_name = str(value).strip()
        if "RaspberryFluke" not in sys_name:
            continue

        # OID = <prefix>.<timeMark>.<localPortNum>.<remoteIndex>
        parts = oid_str.split(".")
        try:
            local_port_num = int(parts[-2])
        except (IndexError, ValueError):
            log.debug("SNMP: could not parse localPortNum from LLDP OID: %s", oid_str)
            continue

        log.debug(
            "SNMP: LLDP-MIB found RaspberryFluke at localPortNum=%d",
            local_port_num,
        )

        # lldpLocPortDesc.<portNum> gives the local port description.
        # On most switches localPortNum equals ifIndex.
        port_desc = session.get(f"{_OID_LLDP_LOC_PORT_DESC}.{local_port_num}")
        port_name = str(port_desc or f"port{local_port_num}").strip()

        # Try to get VLAN via ifIndex (assume localPortNum == ifIndex).
        if_index  = local_port_num
        cisco_vlan = session.get(f"{_OID_CISCO_VM_VLAN}.{if_index}")
        dot1q_vlan = session.get(f"{_OID_DOT1Q_PVID}.{if_index}")
        vlan       = str(cisco_vlan or dot1q_vlan or "").strip()

        return {
            "port_name":  port_name,
            "vlan":       vlan,
            "voice_vlan": "",
        }

    return None


def _get_active_vlans(
    session:      _Session,
    cancel_event: threading.Event,
) -> list[int]:
    """
    Get a list of active VLAN IDs on the switch.

    Tries Cisco VTP VLAN state table first. Falls back to a reasonable
    default if neither Cisco nor Q-BRIDGE VLAN tables are accessible.
    """
    vlans: list[int] = []

    # Try Cisco VTP VLAN state table.
    for oid_str, value in session.walk(_OID_VTP_VLAN_STATE, max_rows=1000):
        if cancel_event.is_set():
            break
        try:
            state   = int(value)
            vlan_id = int(oid_str.split(".")[-1])
            if state == 1 and 1 <= vlan_id <= 4094:
                vlans.append(vlan_id)
        except (ValueError, IndexError):
            continue

    if vlans:
        log.debug("SNMP: found %d active VLANs via Cisco VTP MIB", len(vlans))
        return vlans[:150]

    log.debug("SNMP: VTP VLAN table empty — using default VLAN range")
    return list(range(1, 21))   # fallback: try VLANs 1-20


def _find_port_bridge_mib(
    session:      _Session,
    local_mac:    bytes,
    cancel_event: threading.Event,
) -> Optional[dict]:
    """
    Find our port via the standard BRIDGE-MIB MAC forwarding table.

    On Cisco switches, the BRIDGE-MIB is VLAN-context specific. We must
    use the community string with VLAN index notation (e.g. "public@2001")
    to query the forwarding table for a specific VLAN. This function gets
    the active VLAN list first and iterates through them until our MAC
    is found.

    This is the most universal method but also the slowest. It serves as
    the last resort when CDP-MIB and LLDP-MIB both fail.

    Returns a partial port info dict or None.
    """
    log.debug("SNMP: trying BRIDGE-MIB port discovery against %s", session.host)

    mac_oid = ".".join(str(b) for b in local_mac)

    # First try without VLAN context (works on non-Cisco or VLAN 1 ports).
    bridge_port = session.get(f"{_OID_BRIDGE_FDB_PORT}.{mac_oid}")

    vlan = ""
    if bridge_port is not None:
        log.debug("SNMP: BRIDGE-MIB found MAC in default context, bridge port=%s", bridge_port)
    else:
        # Get active VLANs and try each with community@vlan notation.
        active_vlans = _get_active_vlans(session, cancel_event)

        for vlan_id in active_vlans:
            if cancel_event.is_set():
                return None

            vlan_community = f"{session.community}@{vlan_id}"
            result = session.get(
                f"{_OID_BRIDGE_FDB_PORT}.{mac_oid}",
                community=vlan_community,
            )
            if result is not None:
                bridge_port = result
                vlan        = str(vlan_id)
                log.debug(
                    "SNMP: BRIDGE-MIB found MAC in VLAN %d, bridge port=%s",
                    vlan_id,
                    bridge_port,
                )
                break

    if bridge_port is None:
        log.debug("SNMP: BRIDGE-MIB could not find our MAC on %s", session.host)
        return None

    # Map bridge port number → ifIndex → interface name.
    try:
        bridge_port_int = int(bridge_port)
    except (ValueError, TypeError):
        return None

    if_index = session.get(f"{_OID_BRIDGE_PORT_IFIDX}.{bridge_port_int}")
    if if_index is None:
        return None

    try:
        if_index_int = int(if_index)
    except (ValueError, TypeError):
        return None

    port_name   = session.get(f"{_OID_IF_DESCR}.{if_index_int}")
    cisco_vlan  = session.get(f"{_OID_CISCO_VM_VLAN}.{if_index_int}")
    voice_vlan  = session.get(f"{_OID_CISCO_VOICE_VLAN}.{if_index_int}")

    # If VLAN wasn't determined from VLAN context, try Cisco/Q-BRIDGE MIBs.
    if not vlan:
        dot1q_vlan = session.get(f"{_OID_DOT1Q_PVID}.{if_index_int}")
        vlan = str(cisco_vlan or dot1q_vlan or "").strip()

    return {
        "port_name":  str(port_name  or "").strip(),
        "vlan":       vlan or str(cisco_vlan or "").strip(),
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

    Returns a normalized neighbor dict or None.
    """
    if not _SNMP_AVAILABLE:
        return None

    session = _Session(gateway, community)

    # Confirm SNMP access.
    sys_name_raw = session.get(_OID_SYS_NAME)
    if sys_name_raw is None:
        log.debug("SNMP: community '%s' failed on %s", community, gateway)
        return None

    switch_name = parse_utils.strip_domain(str(sys_name_raw).strip())
    log.debug(
        "SNMP: community '%s' works on %s — switch name: %s",
        community,
        gateway,
        switch_name,
    )

    if cancel_event.is_set():
        return None

    # Give the switch a moment to process our CDP triggers before querying
    # the CDP neighbor table. This sleep is usually covered by DHCP latency
    # but is kept as a safety margin for fast DHCP environments.
    time.sleep(1.0)

    if cancel_event.is_set():
        return None

    # Try port discovery methods in order.
    port_info = None

    port_info = _find_port_cdp_mib(session, cancel_event)

    if port_info is None and not cancel_event.is_set():
        port_info = _find_port_lldp_mib(session, cancel_event)

    if port_info is None and not cancel_event.is_set() and local_mac:
        port_info = _find_port_bridge_mib(session, local_mac, cancel_event)

    if port_info is None:
        log.debug(
            "SNMP: could not determine port on %s with community '%s'",
            gateway,
            community,
        )
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
    race.py. It returns as soon as a result is found or conditions make
    further querying impossible.

    Parameters:
        interface    : Ethernet interface name (used only for ARP fallback)
        local_mac    : 6-byte interface MAC, used for BRIDGE-MIB lookup
        cancel_event : set by race.py when another discovery method wins
        timeout      : maximum seconds before returning None

    Returns:
        Normalized neighbor dict or None.
    """
    if not _SNMP_AVAILABLE:
        log.warning("SNMP discovery skipped: pysnmp-lextudio not installed")
        return None

    deadline = time.monotonic() + timeout

    # Phase 1 — Get a gateway IP to query.
    log.debug("SNMP discovery: waiting for default gateway (DHCP)...")
    gateway = _wait_for_gateway(cancel_event, max_wait=_dhcp_wait())

    if not gateway and not cancel_event.is_set():
        log.debug("SNMP discovery: DHCP timeout — trying ARP observation")
        remaining = deadline - time.monotonic()
        arp_wait  = min(_arp_wait(), max(1.0, remaining - 2.0))
        gateway   = discover_arp.get_gateway_candidate(
            interface, cancel_event, timeout=arp_wait
        )

    if not gateway:
        log.debug("SNMP discovery: no gateway found — giving up")
        return None

    if cancel_event.is_set():
        return None

    log.debug("SNMP discovery: gateway = %s", gateway)

    # Phase 2 — Try SNMP community strings in order.
    communities = _get_communities()

    for community in communities:
        if cancel_event.is_set() or time.monotonic() > deadline:
            return None

        result = _query_switch(gateway, community, local_mac, cancel_event)
        if result:
            return result

    log.debug("SNMP discovery: all community strings failed on %s", gateway)
    return None