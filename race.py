"""
race.py

Parallel discovery race — runs all discovery methods simultaneously and
returns the fastest valid result.

Two methods run in parallel as threads:
  1. SNMP discovery   (discover_snmp)    — primary path, fastest on most networks
  2. Passive LLDP/CDP (discover_passive) — fallback when SNMP unavailable

A third thread runs a persistent CDP burst throughout the discovery window.

Race condition fix:
  The passive capture socket is opened BEFORE triggers are sent. A
  threading.Event (socket_ready) is set by discover_passive the instant the
  socket is open and listening. race.py waits on this event before sending
  any trigger frames. This guarantees no frames from the switch are missed
  because our socket wasn't open yet when the switch responded.

Persistent CDP burst:
  Rather than a one-shot burst at the start, CDP frames are sent continuously
  at 100ms intervals throughout the entire discovery window. This maximises
  the chance of catching the switch's CDP polling cycle on platforms like
  the Catalyst 6500 that do not support immediate CDP response.

What this file does:
  - Open passive listener socket first
  - Wait for socket confirmation before sending triggers
  - Start persistent CDP burst thread
  - Spawn SNMP and passive discovery threads
  - Return the first valid result from either thread
  - Cancel all threads once a winner is found

What this file does NOT do:
  - Implement any discovery logic
  - Parse frames
  - Query SNMP
  - Talk to the display
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Optional

import discover_passive
import discover_snmp
import trigger


log = logging.getLogger(__name__)

# Sentinel placed in the result queue by a thread that found nothing.
_NO_RESULT = object()


def run(
    interface:    str,
    local_mac:    Optional[bytes],
    cancel_event: threading.Event,
    timeout:      float = 120.0,
) -> Optional[dict]:
    """
    Run all discovery methods in parallel and return the first valid result.

    Blocks until one method succeeds, all methods exhaust their options, or
    the cancel_event is set (e.g. link went down while discovering).

    Parameters:
        interface    : Ethernet interface name, e.g. "eth0"
        local_mac    : 6-byte MAC of the interface for self-frame filtering
                       and BRIDGE-MIB MAC lookup. May be None.
        cancel_event : set by main.py if link drops during discovery.
                       Also set internally when a result is found.
        timeout      : maximum seconds for any single discovery method

    Returns:
        The first valid neighbor dict received from any discovery thread,
        or None if all methods timed out or were cancelled.
    """
    # Internal stop event signals all threads to exit once a winner is found.
    # Separate from cancel_event so we don't affect main.py's shutdown logic.
    internal_stop = threading.Event()

    # socket_ready is set by discover_passive the moment its AF_PACKET socket
    # is open and listening. We wait on this before sending any triggers so
    # that switch responses are never missed due to a timing gap.
    socket_ready = threading.Event()

    result_queue: queue.Queue = queue.Queue()
    num_threads = 2

    def _run_passive() -> None:
        try:
            result = discover_passive.discover(
                interface=interface,
                local_mac=local_mac,
                cancel_event=internal_stop,
                timeout=timeout,
                socket_ready=socket_ready,
            )
        except Exception as exc:
            log.exception("Passive discovery thread raised an exception: %s", exc)
            result = None
            # Ensure socket_ready is set even on failure.
            socket_ready.set()

        if result and not internal_stop.is_set() and not cancel_event.is_set():
            result_queue.put(result)
        else:
            result_queue.put(_NO_RESULT)

    def _run_snmp() -> None:
        try:
            result = discover_snmp.discover(
                interface=interface,
                local_mac=local_mac,
                cancel_event=internal_stop,
                timeout=timeout,
            )
        except Exception as exc:
            log.exception("SNMP discovery thread raised an exception: %s", exc)
            result = None

        if result and not internal_stop.is_set() and not cancel_event.is_set():
            result_queue.put(result)
        else:
            result_queue.put(_NO_RESULT)

    # ---- Step 1: Start passive thread FIRST so socket opens immediately ----
    passive_thread = threading.Thread(
        target=_run_passive,
        name="rf-passive",
        daemon=True,
    )
    passive_thread.start()

    # ---- Step 2: Wait for passive socket to confirm open (near-instant) ----
    # Timeout of 2.0s is a safety net — in practice this fires in <50ms.
    if not socket_ready.wait(timeout=2.0):
        log.warning("Race: passive socket did not open within 2s on %s", interface)

    # ---- Step 3: Send triggers NOW that the socket is listening ----
    log.debug("Race: socket ready — sending triggers on %s", interface)
    trigger.send_all_triggers(interface)

    # ---- Step 4: Start persistent CDP burst thread ----
    # Sends CDP frames every 100ms throughout the entire discovery window.
    burst_thread = trigger.start_persistent_cdp_burst(interface, internal_stop)

    # ---- Step 5: Start SNMP thread ----
    snmp_thread = threading.Thread(
        target=_run_snmp,
        name="rf-snmp",
        daemon=True,
    )
    snmp_thread.start()

    log.debug("Race: all discovery threads running on %s", interface)

    # ---- Step 6: Wait for first result ----
    winner:       Optional[dict] = None
    threads_done: int            = 0

    while threads_done < num_threads:
        if cancel_event.is_set():
            log.debug("Race: cancelled by caller on %s", interface)
            internal_stop.set()
            break

        try:
            item = result_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        if item is _NO_RESULT:
            threads_done += 1
            continue

        winner = item
        log.debug(
            "Race: winner on %s is protocol=%s",
            interface,
            winner.get("protocol", "?"),
        )
        internal_stop.set()
        break

    # ---- Step 7: Clean up all threads ----
    internal_stop.set()
    burst_thread.join(timeout=2.0)
    snmp_thread.join(timeout=5.0)
    passive_thread.join(timeout=5.0)

    if winner:
        log.info(
            "Race complete | protocol=%s switch=%s port=%s vlan=%s voice=%s",
            winner.get("protocol"),
            winner.get("switch_name"),
            winner.get("port"),
            winner.get("vlan"),
            winner.get("voice_vlan"),
        )
    else:
        log.info(
            "Race: no result on %s (all methods exhausted or cancelled)",
            interface,
        )

    return winner
