"""Test-only ROS 2 image probe used by the no-observer acceptance scenario.

This plugin exists so the acceptance script can watch an EX *callback counter*
go from 0 to N without adding a second subscriber to the graph. It:

- subscribes to one ``sensor_msgs/msg/CompressedImage`` topic only;
- declares ``diagnostics.mode=periodic`` so the generic port health can report
  ``no_data`` / ``stale`` / ``receiving`` for it;
- consumes in the plugin actor and counts, nothing else: no inference, no
  publisher, no TopicBus payload, no world/object protocol, no action.

It is loaded into a private acceptance instance and removed afterwards. It is
disabled by default and must never be enabled in a production profile.

The count is kept in memory only. ``status`` exists for the acceptance script's
own bookkeeping; EX's port diagnostics remain the authoritative evidence, and a
local publish success would never be treated as proof that EX received data.
"""
import time

# Cap the reported sample count so a long run cannot grow the status payload.
MAX_REPORTED = 10 ** 9


class Plugin:
    def __init__(self, context):
        self.context = context
        self.input = None
        self.received = 0
        self.last_size = None

    def on_load(self):
        self.input = self.context.ros.subscribe("image")

    def on_worker_step(self):
        packet = self.input.get_nowait()
        if packet is None:
            # Nothing to consume: yield instead of spinning the actor thread.
            time.sleep(0.01)
            return
        self.received = min(self.received + 1, MAX_REPORTED)
        data = getattr(packet.message, "data", None)
        self.last_size = len(data) if isinstance(data, (bytes, bytearray)) else None

    def on_unload(self):
        if self.input is not None:
            self.input.close()
            self.input = None
