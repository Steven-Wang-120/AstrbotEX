"""Directory migration must not silently import a different EX checkout."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/lib'))
from mobile_paths import resolve_ex_root


class ExportPathsTests(unittest.TestCase):
    def test_published_checkout(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_ex_root(Path(__file__).resolve().parents[2]),
                             Path(__file__).resolve().parents[3])

    def test_original_layout(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            root = Path(folder)
            runtime = root / 'apps/AstrBotEX/astrbot_ex/core/runtime.py'
            runtime.parent.mkdir(parents=True)
            runtime.touch()
            self.assertEqual(resolve_ex_root(root), root / 'apps/AstrBotEX')

    def test_explicit_override_and_invalid_override(self):
        root = Path(__file__).resolve().parents[3]
        with patch.dict(os.environ, {'ASTREX_EX_ROOT': str(root)}):
            self.assertEqual(resolve_ex_root('/nonexistent-control'), root)
        with patch.dict(os.environ, {'ASTREX_EX_ROOT': '/nonexistent-ex'}):
            with self.assertRaisesRegex(RuntimeError, 'ASTREX_EX_ROOT'):
                resolve_ex_root(Path(__file__).resolve().parents[2])
