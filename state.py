"""
state.py

This file stores RaspberryFluke runtime state in memory.

What this file does:
- Keep track of the current neighbor data
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

    Main ideas:
    - current_neighbor: the most recent parsed neighbor data
    - last_display_text: the last text block sent to the display
    - last_success_time: the last time discovery/parsing succeeded
    """

    def __init__(self) -> None:
        """
        Create a new empty state object.

        At startup, the program does not yet know anything about the switch
        or port it is connected to, so all runtime values begin empty.
        """
        # The newest parsed neighbor data currently in memory.
        self.current_neighbor = None

        # The last text block that was sent to the display.
        # This helps prevent unnecessary display refreshes.
        self.last_display_text = ""

        # The last time the app successfully captured and parsed valid data.
        # Stored as a float timestamp from time.monotonic() or similar.
        self.last_success_time = 0.0

    def update_neighbor(self, neighbor: dict) -> None:
        """
        Save new valid neighbor data into state.

        This method assumes the provided neighbor data is already valid
        and already normalized by the parser layer.
        """
        self.current_neighbor = dict(neighbor)

    def clear_neighbor(self) -> None:
        """
        Clear the active neighbor data from memory.
        """
        self.current_neighbor = None

    def has_neighbor(self) -> bool:
        """
        Return True if there is active current neighbor data.
        """
        return self.current_neighbor is not None

    def set_display_text(self, text: str) -> None:
        """
        Save the last text block that was sent to the display.

        This is used so main.py can compare the new text with the old text
        and avoid unnecessary display updates.
        """
        self.last_display_text = text

    def clear_display_text(self) -> None:
        """
        Clear the saved display text.
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
        self.last_display_text = ""
        self.last_success_time = 0.0

    def neighbor_changed(self, new_neighbor: dict) -> bool:
        """
        Return True if the provided neighbor data is different from
        the current active neighbor data.

        This is a simple full-dictionary comparison.
        That is usually enough for RaspberryFluke because the neighbor
        records are small and predictable.
        """
        return self.current_neighbor != new_neighbor

    def display_text_changed(self, new_text: str) -> bool:
        """
        Return True if the provided text is different from the last
        text that was shown on the display.
        """
        return self.last_display_text != new_text