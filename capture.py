"""
capture.py

This module is responsible for collecting raw neighbor-discovery data from
lldpd / lldpctl.

What this file should do:
- Run lldpctl
- Return the raw command output as text
- Keep subprocess handling in one place
- Fail cleanly if no neighbor data is available yet

What this file should NOT do:
- Parse LLDP fields
- Parse CDP fields
- Update application state
- Talk directly to the display

That work belongs in other modules.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Optional


log = logging.getLogger(__name__)


def build_lldpctl_command(
    interface: Optional[str] = None,
    output_format: str = "keyvalue",
) -> list[str]:
    """
    Build the lldpctl command as a list so it can be passed safely to subprocess.

    Parameters:
        interface:
            Optional interface name such as "eth0".
            If provided, lldpctl will be limited to that interface.
        output_format:
            The lldpctl output format to request.
            "keyvalue" is the most useful for machine parsing.

    Returns:
        A subprocess-ready command list.
    """
    command = ["lldpctl"]

    # Ask lldpctl for a predictable output format when possible.
    # "keyvalue" is usually easier to parse than the default human-readable text.
    if output_format:
        command.extend(["-f", output_format])

    # Limit the command to a specific interface when requested.
    if interface:
        command.append(interface)

    return command


def run_command(
    command: list[str],
    timeout: int = 10,
) -> str:
    """
    Run a system command and return stdout as a string.

    This helper keeps subprocess details in one place so the capture logic stays
    easier to read and maintain.

    Parameters:
        command:
            Command list to execute.
        timeout:
            Maximum number of seconds to wait before aborting.

    Returns:
        The command stdout as a stripped string.

    Notes:
        - Returns an empty string on timeout or command failure.
        - stderr is captured only so we can log it if needed.
    """
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log.warning("Command timed out after %s seconds: %s", timeout, " ".join(command))
        return ""
    except FileNotFoundError:
        log.error("Command not found: %s", command[0])
        return ""
    except Exception as exc:
        log.exception("Unexpected error while running command %s: %s", " ".join(command), exc)
        return ""

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()

    # lldpctl can return non-zero exit codes in some cases where there is simply
    # no usable neighbor data yet. We log lightly and return what we got.
    if result.returncode != 0:
        if stderr:
            log.debug(
                "Command returned non-zero exit status %s: %s | stderr=%s",
                result.returncode,
                " ".join(command),
                stderr,
            )
        else:
            log.debug(
                "Command returned non-zero exit status %s: %s",
                result.returncode,
                " ".join(command),
            )

    return stdout


def capture_neighbors(
    interface: Optional[str] = None,
    timeout: int = 10,
) -> str:
    """
    Capture raw neighbor data using lldpctl in keyvalue format.

    This should be the main function that main.py calls.

    Parameters:
        interface:
            Optional interface name such as "eth0".
        timeout:
            Maximum number of seconds to wait for lldpctl.

    Returns:
        Raw lldpctl output as text.
        Returns an empty string if nothing useful is available.
    """
    command = build_lldpctl_command(interface=interface, output_format="keyvalue")
    raw_output = run_command(command, timeout=timeout)

    if raw_output:
        log.debug("Captured %d characters of lldpctl keyvalue output", len(raw_output))
    else:
        log.debug("No lldpctl keyvalue output captured")

    return raw_output


def capture_neighbors_plain(
    interface: Optional[str] = None,
    timeout: int = 10,
) -> str:
    """
    Capture raw neighbor data using lldpctl in its default plain-text format.

    This is mainly useful as a fallback or for troubleshooting.
    Your normal code path should use keyvalue format instead.

    Parameters:
        interface:
            Optional interface name such as "eth0".
        timeout:
            Maximum number of seconds to wait for lldpctl.

    Returns:
        Raw plain-text lldpctl output.
    """
    # Passing an empty output format means: use lldpctl default output.
    command = build_lldpctl_command(interface=interface, output_format="")
    raw_output = run_command(command, timeout=timeout)

    if raw_output:
        log.debug("Captured %d characters of plain lldpctl output", len(raw_output))
    else:
        log.debug("No plain lldpctl output captured")

    return raw_output


def capture_neighbors_best_effort(
    interface: Optional[str] = None,
    timeout: int = 10,
) -> str:
    """
    Try to capture neighbor data in the preferred format first, then fall back
    to plain text if needed.

    This can be useful during development or when dealing with an environment
    where lldpctl behaves unexpectedly.

    Parameters:
        interface:
            Optional interface name such as "eth0".
        timeout:
            Maximum number of seconds to wait for each attempt.

    Returns:
        Raw neighbor data as text.
        Prefers keyvalue format when available.
    """
    raw_output = capture_neighbors(interface=interface, timeout=timeout)
    if raw_output:
        return raw_output

    log.debug("Falling back to plain lldpctl output")
    return capture_neighbors_plain(interface=interface, timeout=timeout)


def raw_has_neighbor_data(raw_output: str) -> bool:
    """
    Perform a very lightweight check to see whether the raw output appears to
    contain neighbor information.

    This is intentionally simple. Real field extraction should happen in the
    parser modules, not here.

    Parameters:
        raw_output:
            Raw text returned by lldpctl.

    Returns:
        True if the text looks non-empty and possibly useful.
    """
    if not raw_output:
        return False

    # If lldpctl returned any non-empty output at all, that is usually enough
    # for the parser modules to attempt processing.
    return bool(raw_output.strip())