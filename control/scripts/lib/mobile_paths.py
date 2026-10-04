"""Resolve the EX checkout for the published and original project layouts."""
import os
from pathlib import Path


def resolve_ex_root(control_root):
    root = Path(control_root).resolve()
    override = os.environ.get('ASTREX_EX_ROOT')
    candidates = [Path(override).expanduser()] if override else [root / 'apps/AstrBotEX', root.parent]
    for candidate in candidates:
        candidate = candidate.resolve()
        if (candidate / 'astrbot_ex/core/runtime.py').is_file():
            return candidate
    raise RuntimeError('EX checkout not found; set ASTREX_EX_ROOT to the directory containing astrbot_ex/')
