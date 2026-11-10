"""Standalone development entry for the same EX-owned chat subsystem."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import time

from .lifecycle import ChatLifecycle


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the astrEX chat skeleton without the robot runtime")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--data-dir", type=Path, default=Path("runtime/chat-development"))
    parser.add_argument("--demo", action="store_true", help="Loopback-only demo: alice/bob, password astrc-demo")
    args = parser.parse_args()
    lifecycle = ChatLifecycle(args.data_dir, host=args.host, port=args.port, demo=args.demo,
                              invite_token=os.environ.get("ASTREX_CHAT_INVITE_TOKEN"),
                              bot_id=int(os.environ["ASTREX_CHAT_BOT_ID"]) if os.environ.get("ASTREX_CHAT_BOT_ID") else None,
                              reverse_url=os.environ.get("ASTREX_CHAT_ONEBOT_REVERSE_URL"),
                              bot_token=os.environ.get("ASTREX_CHAT_ONEBOT_TOKEN"))
    try:
        lifecycle.start()
        print(f"astrEX chat: http://{args.host}:{lifecycle.port}/api/v1/chat/health", flush=True)
        if args.demo:
            print("Local demo accounts: alice / bob; password: astrc-demo", flush=True)
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        lifecycle.close()


if __name__ == "__main__":
    main()
