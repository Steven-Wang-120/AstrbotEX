from __future__ import annotations

import argparse
import json
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MockVisionHandler(BaseHTTPRequestHandler):
    frame_id = 0

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_json({"ok": True})
            return
        if self.path == "/vision/latest":
            type(self).frame_id += 1
            self._send_json(
                {
                    "ok": True,
                    "frame_id": type(self).frame_id,
                    "timestamp": time.time(),
                    "source": "mock_camera",
                    "latency_ms": 8,
                    "objects": [
                        {
                            "label": "blue_ball",
                            "confidence": 0.92,
                            "bbox_px": [302, 210, 38, 38],
                            "center_px": [321, 229],
                        }
                    ],
                }
            )
            return
        if self.path in {"/vision/stream", "/vision/snapshot"}:
            self._send_json({"ok": False, "error": "mock image stream is not implemented"}, HTTPStatus.NOT_IMPLEMENTED)
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args) -> None:
        print(f"{self.address_string()} - {format % args}")

    def _send_json(self, payload, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a mock Vision Service for AstrBotEX integration tests.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), MockVisionHandler)
    print(f"Mock Vision Service listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping mock vision service...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
