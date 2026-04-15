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

# Default timeout values.
# The normal timeout is for steady-state polling.
# The startup timeout is shorter so boot-time polling can stay responsive.
DEFAULT_TIMEOUT = 5
STARTUP_TIMEOUT = 1


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

    # Ask lldpctl for predictable output when possible.
    # Keyvalue output is easier for the parser modules to work with.
    if output_format:
        command.extend(["-f", output_format])

    # Limit the command to one interface when requested.
    if interface:
        command.append(interface)

    return command


def choose_timeout(
    timeout: Optional[float] = None,
    startup_mode: bool = False,
) -> int:
    """
    Choose the timeout value used for the lldpctl command.

    Parameters:
        timeout:
            Optional explicit timeout value.
            If provided, it wins.

        startup_mode:
            If True, use the shorter startup timeout.
            This is useful during early boot when we want fast retries.

    Returns:
        Timeout as a whole number of seconds, always at least 1.
    """
    if timeout is not None:
        try:
            return max(1, int(float(timeout)))
        except (TypeError, ValueError):
            pass

    if startup_mode:
        return STARTUP_TIMEOUT

    return DEFAULT_TIMEOUT


def run_command(
    command: list[str],
    timeout: int,
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
        - stderr is captured so we can log it when useful.
        - Non-zero exit status does not automatically mean a crash.
          lldpctl may simply not have neighbor data yet.
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
        log.debug(
            "Command timed out after %s second(s): %s",
            timeout,
            " ".join(command),
        )
        return ""
    except FileNotFoundError:
        log.error("Command not found: %s", command[0])
        return ""
    except Exception as exc:
        log.exception(
            "Unexpected error while running command %s: %s",
            " ".join(command),
            exc,
        )
        return ""

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()

    # lldpctl can return non-zero when neighbor data is not ready yet.
    # That is normal enough that we should log it lightly.
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
    timeout: Optional[float] = None,
    startup_mode: bool = False,
) -> str:
    """
    Capture raw neighbor data using lldpctl in keyvalue format.

    This should be the main function that main.py calls.

    Parameters:
        interface:
            Optional interface name such as "eth0".

        timeout:
            Optional explicit timeout in seconds.
            If not provided, this function chooses a sensible timeout.

        startup_mode:
            If True, use a shorter timeout so boot-time polling can retry more
            quickly and reduce time-to-info.

    Returns:
        Raw lldpctl output as text.
        Returns an empty string if nothing useful is available.
    """
    selected_timeout = choose_timeout(timeout=timeout, startup_mode=startup_mode)
    command = build_lldpctl_command(interface=interface, output_format="keyvalue")
    raw_output = run_command(command, timeout=selected_timeout)

    if raw_output:
        log.debug(
            "Captured %d characters of lldpctl keyvalue output",
            len(raw_output),
        )
    else:
        log.debug(
            "No lldpctl keyvalue output captured (startup_mode=%s, timeout=%s)",
            startup_mode,
            selected_timeout,
        )

    return raw_output