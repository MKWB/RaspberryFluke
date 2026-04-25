"""
race.py

Parallel discovery race — runs all discovery methods simultaneously and
returns the fastest valid result.

Three methods run in parallel as threads:
  1. SNMP discovery     (discover_snmp)    — primary path, fastest on most networks
  2. Passive LLDP/CDP   (discover_passive) — fallback when SNMP is unavailable

Both threads post results to a shared queue. The first result placed in the
queue is returned to main.py. All remaining threads are signalled to stop via
a shared cancel event.

Why this design:
  - No method waits for another. The winner is whoever responds first.
  - SNMP and passive each have clean, independent code paths.
  - main.py stays simple — one call in, one result out.
  - Adding a new discovery method in future requires only adding a thread here.

What this file does:
  - Send LLDP and CDP trigger frames immediately on link-up
  - Spawn discovery threads
  - Return the first valid result
  - Signal all threads to stop once a winner is found

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
# Allows the queue reader to count how many threads have finished.
_NO_RESULT = object()


def run(
    interface:    str,
    local_mac:    Optional[bytes],
    cancel_event: threading.Event,
    timeout:      float = 180.0,
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
                       Also set internally when a result is found, so all
                       remaining threads stop promptly.
        timeout      : maximum seconds for any single discovery method

    Returns:
        The first valid neighbor dict received from any discovery thread,
        or None if all methods timed out or were cancelled.
    """
    # Send LLDP and CDP trigger frames immediately so the switch starts
    # processing our presence before any discovery thread begins its work.
    # This gives SNMP the best chance of finding us in the CDP/LLDP
    # neighbor table when it queries ~3-5 seconds later.
    log.debug("Race: sending triggers on %s", interface)
    trigger.send_all_triggers(interface)

    # Internal event to signal all threads to stop once a winner is found.
    # We use a separate event from the caller's cancel_event so we can set
    # it without affecting the caller's own shutdown logic.
    internal_stop = threading.Event()

    result_queue: queue.Queue = queue.Queue()
    num_threads = 2

    def _run_snmp():
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

        if not internal_stop.is_set() and not cancel_event.is_set() and result:
            result_queue.put(result)
        else:
            result_queue.put(_NO_RESULT)

    def _run_passive():
        try:
            result = discover_passive.discover(
                interface=interface,
                local_mac=local_mac,
                cancel_event=internal_stop,
                timeout=timeout,
            )
        except Exception as exc:
            log.exception("Passive discovery thread raised an exception: %s", exc)
            result = None

        if not internal_stop.is_set() and not cancel_event.is_set() and result:
            result_queue.put(result)
        else:
            result_queue.put(_NO_RESULT)

    # Spawn threads.
    snmp_thread = threading.Thread(
        target=_run_snmp,
        name="rf-snmp",
        daemon=True,
    )
    passive_thread = threading.Thread(
        target=_run_passive,
        name="rf-passive",
        daemon=True,
    )

    log.debug("Race: starting discovery threads on %s", interface)
    snmp_thread.start()
    passive_thread.start()

    # Wait for results.
    winner:       Optional[dict] = None
    threads_done: int            = 0

    while threads_done < num_threads:
        # Exit early if main.py cancels (e.g. link dropped).
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

        # A thread found something.
        winner = item
        log.debug(
            "Race: winner on %s is protocol=%s",
            interface,
            winner.get("protocol", "?"),
        )
        internal_stop.set()   # tell remaining threads to stop
        break

    # Signal all threads to stop and wait for them to finish.
    internal_stop.set()
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
        log.info("Race: no result on %s (all methods exhausted or cancelled)", interface)

    return winner
