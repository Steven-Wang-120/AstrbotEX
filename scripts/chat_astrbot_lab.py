"""Verify real AstrBot commands through an isolated EX chat service."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--image", required=True, help="Preinstalled image; never pulled")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--flutter")
    parser.add_argument("--astrc-root", type=Path)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--check-only", action="store_true", help="Provision and verify the manual lab, then stop")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    if bool(args.flutter) != bool(args.astrc_root):
        parser.error("Supply --flutter and --astrc-root together")
    if args.serve and not args.data_dir:
        parser.error("--serve requires --data-dir")
    if not args.serve and (args.data_dir or args.host != "127.0.0.1"):
        parser.error("Automated checks require temporary data and loopback")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from astrbot_ex.core.chat.lifecycle import ChatLifecycle
    from astrbot_ex.core.chat.native_api import create_app
    from astrbot_ex.core.chat.onebot11.transport import ReverseConnector

    def docker(*command: str) -> str:
        result = subprocess.run([args.docker, *command], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=90)
        if result.returncode:
            raise RuntimeError(f"Docker {command[0]} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    if args.serve and args.flutter:
        parser.error("Manual labs use generated credentials; Dart checks require automated mode")
    if args.check_only and not args.serve:
        parser.error("--check-only requires --serve")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        host_port = reservation.getsockname()[1]
    image = json.loads(docker("image", "inspect", args.image))[0]
    name = "astrc-lab-" + uuid4().hex[:12]
    token = secrets.token_urlsafe(32)
    listener = None
    created = False
    temporary = tempfile.TemporaryDirectory(prefix="astrc-astrbot-")
    work = Path(temporary.name)
    data_root = args.data_dir.resolve() if args.serve else work / "ex"
    bot_root = work / "astrbot"
    (bot_root / "data").mkdir(parents=True)
    config = {
        "platform": [{"id": "astrc_acceptance", "type": "aiocqhttp", "enable": True,
                      "ws_reverse_host": "0.0.0.0", "ws_reverse_port": 6199,
                      "ws_reverse_token": token}],
        "platform_settings": {"enable_id_white_list": False, "reply_with_mention": True,
                              "reply_with_quote": True},
        "dashboard": {"enable": False}, "provider": [], "provider_sources": [],
        "provider_settings": {"enable": False}, "disable_metrics": True,
        "t2i": False, "log_level": "INFO",
    }
    (bot_root / "data" / "cmd_config.json").write_text(json.dumps(config), encoding="utf-8")
    bootstrap = (
        "import asyncio; import main; main.check_env(); "
        "broker=main.LogBroker(); main.LogManager.set_queue_handler(main.logger,broker); "
        "loader=main.InitialLoader(main.db_helper,broker); asyncio.run(loader.start())"
    )
    try:
        docker("run", "--detach", "--pull=never", "--name", name,
               "--publish", f"127.0.0.1:{host_port}:6199", "--env", "ASTRBOT_ROOT=/lab",
               "--mount", f"type=bind,source={bot_root},target=/lab",
               "--entrypoint", "python", image["Id"], "-c", bootstrap)
        created = True
        port = int(json.loads(docker("inspect", name))[0]["NetworkSettings"]["Ports"]["6199/tcp"][0]["HostPort"])
        listener = ChatLifecycle(data_root, host=args.host, port=args.port, demo=not args.serve)
        if args.serve:
            account_path = data_root / "TEST_ACCOUNTS.json"
            if account_path.exists():
                accounts = json.loads(account_path.read_text(encoding="utf-8"))
            else:
                with listener.repository.transaction() as db:
                    if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                        raise RuntimeError("Manual lab needs an empty database or its TEST_ACCOUNTS.json")
                password = secrets.token_urlsafe(15)
                alice = listener.service.create_user("alice", password, "Alice")
                bob = listener.service.create_user("bob", password, "Bob")
                bot = listener.service.create_user("astrbot", secrets.token_urlsafe(32), "AstrBot", "bot")
                listener.service.create_group(int(alice["user_id"]), "astrC 手动联调", [bob["user_id"], bot["user_id"]])
                accounts = {"alice": alice["user_id"], "bob": bob["user_id"], "bot": bot["user_id"], "password": password}
                account_path.write_text(json.dumps(accounts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            accounts = {"alice": "1", "bob": "2", "bot": "3", "password": "astrc-demo"}
        listener.connector = ReverseConnector(listener.service, int(accounts["bot"]), f"ws://127.0.0.1:{port}/ws", token)
        observed_actions = []
        dispatch = listener.connector.dispatcher.dispatch
        def observe(request):
            result = dispatch(request)
            if request.get("action") in {"get_msg", "get_group_member_info", "get_stranger_info"}:
                observed_actions.append({"action": request["action"], "params": request.get("params", {}), "status": result["status"]})
            return result
        listener.connector.dispatcher.dispatch = observe
        listener.app = create_app(listener.service, demo=not args.serve, connector=listener.connector)
        listener.start()
        deadline = time.monotonic() + 60
        while not listener.connector.connected:
            if time.monotonic() > deadline:
                raise RuntimeError("Real AstrBot reverse WebSocket did not connect within 60 seconds")
            if not json.loads(docker("inspect", name))[0]["State"]["Running"]:
                raise RuntimeError("Isolated AstrBot exited during startup")
            time.sleep(0.25)
        metadata_code = (
            "import json,hashlib; from pathlib import Path; import astrbot; "
            "files=['astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py',"
            "'astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py',"
            "'astrbot/builtin_stars/builtin_commands/main.py',"
            "'astrbot/builtin_stars/builtin_commands/commands/sid.py']; "
            "print(json.dumps({'version':astrbot.__version__,'source_sha256':"
            "{p:hashlib.sha256(Path('/AstrBot',p).read_bytes()).hexdigest() for p in files}}))"
        )
        metadata = json.loads(docker("exec", name, "python", "-c", metadata_code))
        report = {"schema_version": 1, "tested_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                  "astrbot_image": args.image, "astrbot_image_id": image["Id"], "astrbot": metadata,
                  "source_unchanged": True, "scenarios": [],
                  "limitations": ["Android/iOS not verified", "QQ/NapCat comparison not run",
                                  "Third-party plugins and LLM providers not verified", "Media not implemented"]}
        probe_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
        base = f"http://{probe_host}:{listener.port}/api/v1/chat"
        with httpx.Client(base_url=base, trust_env=False, timeout=10) as client:
            sessions = {}
            try:
                for username in ("alice", "bob"):
                    result = client.post("/auth/login", json={"username": username, "password": accounts["password"], "device_id": "acceptance-" + username})
                    result.raise_for_status()
                    sessions[username] = {"Authorization": "Bearer " + result.json()["access_token"]}
                groups = client.get("/conversations", headers=sessions["alice"])
                groups.raise_for_status()
                group = next(c for c in groups.json()["items"] if c["kind"] == "group")
                direct = client.post("/conversations/direct", headers=sessions["bob"], json={"target_user_id": accounts["bot"]})
                direct.raise_for_status()
                scenarios = (("alice-group-command", "alice", group), ("bob-group-command", "bob", group),
                             ("bob-private-command", "bob", direct.json()),
                             ("alice-group-mention-reply", "alice", group),
                             ("alice-group-after-reconnect", "alice", group))
                for scenario, username, conversation in scenarios:
                    if scenario == "alice-group-after-reconnect":
                        docker("restart", "--timeout", "5", name)
                    cid = conversation["conversation_id"]
                    segments = [{"type": "text", "data": {"text": "/sid"}}]
                    quote_id = None
                    if scenario == "alice-group-mention-reply":
                        # Quote a message in this group, not the preceding private reply.
                        prior = client.get(f"/conversations/{cid}/messages", headers=sessions[username])
                        prior.raise_for_status()
                        quote_id = next(m["message_id"] for m in reversed(prior.json()["items"]) if m["sender_id"] == accounts["bot"])
                        segments = [{"type": "reply", "data": {"message_id": quote_id}},
                                    {"type": "mention", "data": {"user_id": accounts["bot"]}}, *segments]
                    result = client.post(f"/conversations/{cid}/messages", headers=sessions[username],
                                         json={"client_message_id": str(uuid4()), "segments": segments})
                    result.raise_for_status()
                    sent_id = result.json()["message_id"]
                    deadline = time.monotonic() + 30
                    while True:
                        history = client.get(f"/conversations/{cid}/messages", headers=sessions[username])
                        history.raise_for_status()
                        replies = [m for m in history.json()["items"] if m["sender_id"] == accounts["bot"] and int(m["message_id"]) > int(sent_id)]
                        if replies:
                            reply = replies[0]
                            text = "".join(s["data"]["text"] for s in reply["segments"] if s["type"] == "text")
                            if "UMO:" not in text or f"UID: 「{accounts[username]}」" not in text:
                                raise RuntimeError(f"Wrong identity or reply for {scenario}")
                            if not any(s["type"] == "reply" and s["data"]["message_id"] == sent_id for s in reply["segments"]):
                                raise RuntimeError(f"Missing native quote in real bot reply for {scenario}")
                            if conversation["kind"] == "group" and not any(s["type"] == "mention" and s["data"]["user_id"] == accounts[username] for s in reply["segments"]):
                                raise RuntimeError(f"Missing native mention in real bot reply for {scenario}")
                            if quote_id is not None:
                                expected = {"get_msg", "get_group_member_info", "get_stranger_info"}
                                successful = {a["action"] for a in observed_actions if a["status"] == "ok"}
                                if not expected <= successful or not any(a["action"] == "get_msg" and str(a["params"].get("message_id")) == quote_id and a["status"] == "ok" for a in observed_actions):
                                    raise RuntimeError("Real AstrBot did not resolve incoming mention and quote")
                            report["scenarios"].append({"name": scenario, "status": "passed", "sender_id": accounts[username],
                                                        "message_id": sent_id, "reply_id": reply["message_id"]})
                            break
                        if time.monotonic() > deadline:
                            raise RuntimeError(f"Real AstrBot /sid reply missing for {scenario}")
                        time.sleep(0.1)
                other = client.get(f"/conversations/{direct.json()['conversation_id']}/messages", headers=sessions["alice"])
                if other.status_code != 403:
                    raise RuntimeError("A different account could read the private bot conversation")
                report["scenarios"].append({"name": "private-membership-denied", "status": "passed"})
            finally:
                for headers in sessions.values():
                    client.post("/auth/logout", headers=headers)
        if args.flutter:
            result = subprocess.run([args.flutter, "test", "test/astrbot_roundtrip_test.dart",
                                     f"--dart-define=ASTRC_TEST_SERVER=http://127.0.0.1:{listener.port}"],
                                    cwd=args.astrc_root, check=False)
            if result.returncode:
                raise RuntimeError("Real Dart client / AstrBot integration failed")
            report["scenarios"].append({"name": "real-dart-clients-astrbot-roundtrip", "status": "passed"})
        report["observed_adapter_actions"] = observed_actions
        report["status"] = "passed"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        if args.serve:
            print(f"\nManual lab: {args.host}:{listener.port}; normal mode, isolated test database.", flush=True)
            print(f"Accounts: alice / bob; password: {accounts['password']}", flush=True)
            print(f"Send /sid in the group or privately to user ID {accounts['bot']}. Ctrl+C stops both services.", flush=True)
            while not args.check_only:
                time.sleep(0.5)
        return 0
    except KeyboardInterrupt:
        return 0 if args.serve else 130
    except Exception:
        if created:
            logs = docker("logs", "--tail", "45", name)
            safe = [line for line in logs.splitlines() if not any(word in line.lower() for word in ("password", "token", "secret", "密码"))]
            print("\n".join(safe).replace(token, "[redacted]"), file=sys.stderr)
        raise
    finally:
        try:
            if listener is not None:
                listener.close()
        finally:
            if created:
                docker("rm", "--force", name)
            temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
