from __future__ import annotations

import unittest

from astrbot_ex.core.topic_bus import TopicBus


class TopicBusInboxTest(unittest.TestCase):
    def test_inbox_is_bounded_and_keeps_latest_message(self) -> None:
        bus = TopicBus()
        inbox = bus.subscribe_inbox("camera.target", max_messages=1)
        try:
            bus.publish_payload("camera.target", timestamp=1.0, source="camera", payload={"seq": 1})
            bus.publish_payload("camera.target", timestamp=2.0, source="camera", payload={"seq": 2})

            message = inbox.take_latest()
            self.assertIsNotNone(message)
            self.assertEqual(message.payload, {"seq": 2})
            self.assertIsNone(inbox.get_nowait())
        finally:
            inbox.close()

        bus.publish_payload("camera.target", timestamp=3.0, source="camera", payload={"seq": 3})
        self.assertIsNone(inbox.get_nowait())


if __name__ == "__main__":
    unittest.main()
