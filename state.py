"""
state.py

This file stores RaspberryFluke runtime state in memory.

What this file does:
- Keep track of the current neighbor data
- Keep track of the last successful discovery time

What this file does NOT do:
- Capture LLDP or CDP data
- Parse raw protocol output
- Initialize or control the display hardware
- Write runtime state to disk
- Track display text (the display module handles its own change detection)

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
    - last_success_time: the last time discovery/parsing succeeded

    Display change detection is handled by the display module itself.
    State only tracks neighbor data and discovery timing.
    """

    def __init__(self) -> None:
        """
        Create a new empty state object.

        At startup, the program does not yet know anything about the switch
        or port it is connected to, so all runtime values begin empty.
        """
        # The newest parsed neighbor data currently in memory.
        self.current_neighbor = None

        # The last time the app successfully captured and parsed valid data.
        # Stored as a float from time.monotonic().
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

    def set_last_success_time(self, timestamp_value: float) -> None:
        """
        Save the time of the last successful capture/parse cycle.
        """
        self.last_success_time = float(timestamp_value)

    def neighbor_changed(self, new_neighbor: dict) -> bool:
        """
        Return True if the provided neighbor data is different from
        the current active neighbor data.

        This is a simple full-dictionary comparison, which is sufficient
        because neighbor records are small and contain only strings.
        """
        return self.current_neighbor != new_neighbor