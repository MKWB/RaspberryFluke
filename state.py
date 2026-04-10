"""
state.py

This file stores RaspberryFluke runtime state in memory.

What this file does:
- Keep track of the current neighbor data
- Keep track of the previous neighbor data
- Keep track of the last known good neighbor data
- Keep track of the last text shown on the display
- Keep track of the last successful discovery time

What this file does NOT do:
- Capture LLDP or CDP data
- Parse raw protocol output
- Initialize or control the display hardware
- Write runtime state to disk

Why this file exists:
- It gives main.py one clean place to store and read runtime values
- It keeps the main loop from becoming cluttered with loose variables
- It keeps RaspberryFluke state in RAM only, which is good for SD card life
"""


class RaspberryFlukeState:
    """
    In-memory state container for RaspberryFluke.

    This class acts as the program's short-term memory while it is running.
    It stores what the application currently knows and what it last showed
    on the display.

    Main ideas:
    - current_neighbor: the most recent parsed neighbor data
    - previous_neighbor: the neighbor data that existed before the current one
    - last_good_neighbor: the most recent valid neighbor data we trust
    - last_display_text: the last text block sent to the display
    - last_success_time: the last time discovery/parsing succeeded
    """

    def __init__(self) -> None:
        """
        Create a new empty state object.

        At startup, the program does not yet know anything about the switch
        or port it is connected to, so all neighbor-related values begin empty.
        """
        # The newest parsed neighbor data currently in memory.
        self.current_neighbor = None

        # The neighbor data that existed before current_neighbor.
        # This is useful if you want to compare "before" and "after."
        self.previous_neighbor = None

        # The most recent valid neighbor data that successfully parsed.
        # This is useful when discovery briefly fails and you do not want
        # the screen to instantly forget the last good result.
        self.last_good_neighbor = None

        # The last text block that was sent to the display.
        # This helps prevent unnecessary display refreshes.
        self.last_display_text = ""

        # The last time the app successfully captured and parsed valid data.
        # Stored as a float timestamp from time.monotonic() or similar.
        self.last_success_time = 0.0

    def update_neighbor(self, neighbor: dict) -> None:
        """
        Save new valid neighbor data into state.

        What this method does:
        - Move the current neighbor into previous_neighbor
        - Save the new neighbor as current_neighbor
        - Also save it as last_good_neighbor

        This method assumes the provided neighbor data is already valid
        and already normalized by the parser layer.
        """
        self.previous_neighbor = dict(self.current_neighbor) if self.current_neighbor else None
        self.current_neighbor = dict(neighbor)
        self.last_good_neighbor = dict(neighbor)

    def set_neighbor(self, neighbor: dict) -> None:
        """
        Alternate method name for saving neighbor data.

        This does the same job as update_neighbor().
        It exists to keep state.py flexible if main.py uses either name.
        """
        self.update_neighbor(neighbor)

    def clear_neighbor(self) -> None:
        """
        Clear the active neighbor data from memory.

        Important behavior:
        - current_neighbor is cleared
        - previous_neighbor is cleared

        What is NOT cleared:
        - last_good_neighbor is kept

        Why keep last_good_neighbor?
        Because it can still be useful as a fallback reference even after the current neighbor state is considered stale.
        """
        self.current_neighbor = None
        self.previous_neighbor = None

    def has_neighbor(self) -> bool:
        """
        Return True if there is active current neighbor data.
        """
        return self.current_neighbor is not None

    def has_last_good_neighbor(self) -> bool:
        """
        Return True if there is a remembered valid neighbor record.
        """
        return self.last_good_neighbor is not None

    def set_display_text(self, text: str) -> None:
        """
        Save the last text block that was sent to the display.

        This is used so main.py can compare the new text with the old text and avoid unnecessary display updates.
        """
        self.last_display_text = text

    def clear_display_text(self) -> None:
        """
        Clear the saved display text.

        In normal use, you may not need this often, but it is useful to have available as a clean reset option.
        """
        self.last_display_text = ""

    def set_last_success_time(self, timestamp_value: float) -> None:
        """
        Save the time of the last successful capture/parse cycle.
        """
        self.last_success_time = float(timestamp_value)

    def reset(self) -> None:
        """
        Reset the entire runtime state back to startup defaults.

        Use this only when you truly want a full in-memory reset.
        """
        self.current_neighbor = None
        self.previous_neighbor = None
        self.last_good_neighbor = None
        self.last_display_text = ""
        self.last_success_time = 0.0

    def neighbor_changed(self, new_neighbor: dict) -> bool:
        """
        Return True if the provided neighbor data is different from
        the current active neighbor data.

        This is a simple full-dictionary comparison.
        That is usually enough for the RaspberryFluke because the neighbor records are small and predictable.
        """
        return self.current_neighbor != new_neighbor

    def display_text_changed(self, new_text: str) -> bool:
        """
        Return True if the provided text is different from the last
        text that was shown on the display.
        """
        return self.last_display_text != new_text

    def vlan_changed(self, new_neighbor: dict) -> bool:
        """
        Return True if the VLAN value changed compared to the current neighbor.

        If there is no current neighbor yet, this counts as a change.
        """
        if self.current_neighbor is None:
            return True

        return self.current_neighbor.get("vlan") != new_neighbor.get("vlan")

    def voice_vlan_changed(self, new_neighbor: dict) -> bool:
        """
        Return True if the voice VLAN value changed compared to the
        current neighbor.

        If there is no current neighbor yet, this counts as a change.
        """
        if self.current_neighbor is None:
            return True

        return self.current_neighbor.get("voice_vlan") != new_neighbor.get("voice_vlan")

    def get_status(self) -> dict:
        """
        Return a simple dictionary snapshot of the current runtime state.

        This is useful for debugging, testing, or future status reporting.
        """
        return {
            "has_current_neighbor": self.current_neighbor is not None,
            "has_previous_neighbor": self.previous_neighbor is not None,
            "has_last_good_neighbor": self.last_good_neighbor is not None,
            "last_display_text": self.last_display_text,
            "last_success_time": self.last_success_time,
            "current_neighbor": self.current_neighbor,
            "previous_neighbor": self.previous_neighbor,
            "last_good_neighbor": self.last_good_neighbor,
        }