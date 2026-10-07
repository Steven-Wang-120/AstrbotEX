"""Run the unchanged real Dispatcher stress case with SQL timing and thread dumps.

From any directory:
    python -B /path/to/AstrBotEX/scripts/diagnose_dispatcher_stress.py /path/to/AstrBotEX

Redirect stdout and stderr to a new external evidence file. No model or hardware
is used; the original test owns temporary SQLite data and cleanup. This wrapper
does not change its 1000 sequences, assertions, Future timeouts or random seed.

Thread stacks repeat every 8 seconds. --dump-mode auto uses a Python watchdog
on Windows and native faulthandler elsewhere; python/native explicitly select
one. Python dumping captures no locals, but cannot run while another thread
holds the GIL indefinitely. Use an external process timeout for that case.
"""
from __future__ import annotations

import argparse
import faulthandler
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
import traceback
import unittest


TEST_NAME = (
    "tests.test_action_dispatcher.DispatcherTests."
    "test_1000_real_dispatcher_state_sequences_with_resources_and_rearm"
)


class Observed(sqlite3.Connection):
    def commit(self):
        start = time.monotonic()
        try:
            return super().commit()
        finally:
            elapsed = time.monotonic() - start
            if elapsed > .1:
                print("SLOW_COMMIT", elapsed, flush=True)

    def execute(self, *args, **kwargs):
        start = time.monotonic()
        try:
            return super().execute(*args, **kwargs)
        finally:
            elapsed = time.monotonic() - start
            if elapsed > .1:
                print("SLOW_SQL", args[0][:90], elapsed, flush=True)


def _python_watchdog(stop, stream):
    while not stop.wait(8):
        print("PYTHON_THREAD_DUMP interval=8s (no locals)", file=stream, flush=True)
        frames = sys._current_frames()
        frame = None
        try:
            for ident, frame in frames.items():
                if stop.is_set():
                    break
                print(f"Thread 0x{ident:x} (most recent call last):", file=stream)
                traceback.print_stack(frame, file=stream)
            stream.flush()
        finally:
            # Do not retain frames (and their locals) between dumps.
            del frames
            del frame


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path,
                        default=Path(__file__).resolve().parents[1],
                        help="AstrBotEX checkout root; defaults to this script's checkout")
    parser.add_argument("--check-import", action="store_true",
                        help="load and verify the named test without running stress sequences")
    parser.add_argument("--dump-mode", choices=("auto", "python", "native"), default="auto",
                        help="auto: Python watchdog on Windows, native elsewhere; stacks every 8s")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    test_path = root / "tests/test_action_dispatcher.py"
    if not test_path.is_file() or not (root / "astrbot_ex").is_dir():
        parser.error("root must contain astrbot_ex and tests/test_action_dispatcher.py")
    old_path = sys.path[:]
    sys.path.insert(0, str(root))
    old_connect = sqlite3.connect
    stop = threading.Event()
    watchdog = None
    native_started = False
    sqlite3.connect = lambda *a, **kw: old_connect(*a, **{**kw, "factory": Observed})
    try:
        suite = unittest.defaultTestLoader.loadTestsFromName(TEST_NAME)
        module = sys.modules.get("tests.test_action_dispatcher")
        cases = list(suite)
        if (module is None or Path(module.__file__).resolve() != test_path.resolve()
                or len(cases) != 1 or cases[0].id() != TEST_NAME):
            print("TEST_IMPORT_FAILED", TEST_NAME, file=sys.stderr, flush=True)
            return 2
        files = {}
        for imported in list(sys.modules.values()):
            filename = getattr(imported, "__file__", None)
            if not filename:
                continue
            path = Path(filename).resolve()
            if path.is_relative_to(root) and path.is_file():
                files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        print("IMPORTED_SOURCE_HASHES", json.dumps(files, sort_keys=True), flush=True)
        if args.check_import:
            print("TEST_IMPORT_OK", TEST_NAME, flush=True)
            return 0
        mode = args.dump_mode
        if mode == "auto":
            mode = "python" if sys.platform == "win32" else "native"
        print("STACK_DUMP_MODE", mode, "interval=8s", flush=True)
        if mode == "python":
            print("PYTHON_WATCHDOG_LIMIT: cannot dump while another thread holds the GIL "
                  "indefinitely; use an external process timeout.", file=sys.stderr, flush=True)
            watchdog = threading.Thread(target=_python_watchdog, args=(stop, sys.stderr),
                                        name="dispatcher-diagnostic-watchdog", daemon=True)
            watchdog.start()
        else:
            faulthandler.dump_traceback_later(8, repeat=True)
            native_started = True
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1
    finally:
        # Restore the connection hook even if cancellation or output raises.
        sqlite3.connect = old_connect
        sys.path[:] = old_path
        stop.set()
        if native_started:
            faulthandler.cancel_dump_traceback_later()
        if watchdog is not None and watchdog.ident is not None:
            watchdog.join(timeout=2)
            if watchdog.is_alive():
                print("WATCHDOG_CLEANUP_INCOMPLETE: still alive after 2s join; daemon thread "
                      "may be blocked on diagnostic output.", file=sys.stderr, flush=True)
            else:
                print("PYTHON_WATCHDOG_STOPPED", file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
