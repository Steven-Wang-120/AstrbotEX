import time

class Plugin:
    def __init__(self, context):
        self.context = context

    def on_load(self):
        self.input = self.context.ros.subscribe("text_in")
        self.output = self.context.ros.publisher("text_out")

    def on_worker_step(self):
        packet = self.input.get_nowait()
        if packet is None:
            time.sleep(0.01)
            return
        # Business interpretation is on the plugin actor, not the ROS executor.
        text = packet.message.data
        self.context.topic_bus.publish_payload("ros2_echo.received",
            timestamp=time.time(), source="ros2_echo", payload={"text": text})
        if self.output.status()["resource_created"]:
            message = self.output.new_message()
            message.data = text
            self.output.publish(message)

    def on_unload(self):
        self.input.close()
        self.output.close()
