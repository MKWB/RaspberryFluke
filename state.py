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

# Timeout used during normal steady-state polling.
_DEFAULT_TIMEOUT: float = 5.0

# Shorter timeout used during startup so early boot polling stays responsive.
# Set to 2.0 seconds to give lldpd enough time to respond on a cold Pi boot
# while still allowing fast retry cycles.
_STARTUP_TIMEOUT: float = 2.0


def _build_lldpctl_command(
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

    if output_format:
        command.extend(["-f", output_format])

    if interface:
        command.append(interface)

    return command


def _choose_timeout(
    timeout: Optional[float] = None,
    startup_mode: bool = False,
) -> float:
    """
    Choose the timeout value used for the lldpctl command.

    Parameters:
        timeout:
            Optional explicit timeout value in seconds.
            If provided, it wins over the defaults.

        startup_mode:
            If True, use the shorter startup timeout.

    Returns:
        Timeout as a float, always at least 1.0 second.
    """
    if timeout is not None:
        try:
            return max(1.0, float(timeout))
        except (TypeError, ValueError):
            pass

    if startup_mode:
        return _STARTUP_TIMEOUT

    return _DEFAULT_TIMEOUT


def _run_command(
    command: list[str],
    timeout: float,
) -> str:
    """
    Run a system command and return stdout as a string.

    Parameters:
        command:
            Command list to execute.

        timeout:
            Maximum number of seconds to wait before aborting.

    Returns:
        The command stdout as a stripped string.
        Returns an empty string on timeout or command failure.

    Notes:
        - stderr is captured so it can be logged when useful.
        - Non-zero exit status does not automatically mean a crash.
          lldpctl may return non-zero when neighbor data is not ready yet.
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
            "Command timed out after %.1f second(s): %s",
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

    This is the only public function in this module. main.py should call
    this and pass the raw text to parse_utils.parse_keyvalue_output.

    Parameters:
        interface:
            Optional interface name such as "eth0".

        timeout:
            Optional explicit timeout in seconds (float).
            If not provided, a sensible default is chosen automatically.

        startup_mode:
            If True, use a shorter timeout so boot-time polling can retry more
            quickly and reduce time-to-info.

    Returns:
        Raw lldpctl output as text.
        Returns an empty string if nothing useful is available.
    """
    selected_timeout = _choose_timeout(timeout=timeout, startup_mode=startup_mode)
    command = _build_lldpctl_command(interface=interface, output_format="keyvalue")
    raw_output = _run_command(command, timeout=selected_timeout)

    if raw_output:
        log.debug(
            "Captured %d characters of lldpctl keyvalue output",
            len(raw_output),
        )
    else:
        log.debug(
            "No lldpctl keyvalue output captured (startup_mode=%s, timeout=%.1f)",
            startup_mode,
            selected_timeout,
        )

    return raw_output
