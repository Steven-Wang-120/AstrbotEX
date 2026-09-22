from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from subprocess import Popen
from typing import Any


def launch(root: Path, config: dict[str, Any]) -> Popen[str]:
    worker_mode = str(config.get("worker_mode", "bundled")).lower()
    bundled = root / "worker" / "yolo_worker.py"
    if worker_mode == "bundled":
        command = [sys.executable, str(bundled)]
    elif worker_mode == "python":
        python = str(config.get("worker_python", "")).strip() or sys.executable
        command = [python, str(bundled)]
    else:
        raise ValueError(f"unsupported worker_mode: {worker_mode}")
    config_path = root / ".worker-config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return Popen(command + ["--config", str(config_path)], cwd=str(root / "worker"), stdin=-1, stdout=-1, stderr=-2, text=True, bufsize=1, encoding="utf-8", errors="replace")
