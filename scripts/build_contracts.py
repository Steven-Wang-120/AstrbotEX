"""Concatenate the two B00 contract modules into one self-contained file.

The contract rules are authored in two focused modules so they stay readable:

    astrbot_ex/core/actions/models.py    command, status, action declaration v2
    astrbot_ex/core/decision/models.py   goal submit/cancel/renew, state, feedback

A.E.B is a flat plugin directory, so its mirror must be a single top-level
module. This script produces that single module by concatenating the two
sources, stripping the docstring/import block of the second, and naming the
symbols it needed from the first.

Run from the AstrBotEX repository root:

    python scripts/build_contracts.py
    python scripts/build_contracts.py --check    # verify the file is current

Then regenerate the A.E.B mirror:

    python scripts/sync_contract_mirror.py
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACTIONS = ROOT / "astrbot_ex" / "core" / "actions" / "models.py"
DECISION = ROOT / "astrbot_ex" / "core" / "decision" / "models.py"
OUT = ROOT / "astrbot_ex" / "core" / "contracts.py"

HEADER = '''"""B00 frozen cross-endpoint contracts (canonical, self-contained).

GENERATED FILE - do not edit by hand; edit the two source modules listed below
and rerun ``python scripts/build_contracts.py``.

Sources
-------
    astrbot_ex/core/actions/models.py    command, status, action declaration v2
    astrbot_ex/core/decision/models.py   goal submit/cancel/renew, state, feedback

This module imports only the Python standard library, so a byte-identical copy
can live in the A.E.B plugin directory and both sides are then guaranteed to
parse the same payload into the same result:

    AstrBotEX:  astrbot_ex/core/contracts.py
    A.E.B:      astrbot_plugin_astrbotex_interaction/task_contracts.py

Frozen surface
--------------
* transport: ``astrbotex-zmq`` envelope version 1, unchanged. Business payloads
  carry ``schema_version = 1``.
* methods: the seven ``decision.*`` methods.
* phases: accepted / pending_cancel / active / blocked / rejected. Only
  ``active`` enters decision.
* action statuses: admitted -> accepted -> running -> terminal. Terminal states
  are mutually exclusive and never move backwards. ``canceled`` requires plugin
  stop evidence.
* action declaration v2; v1 legacy topic actions stay isolated both ways.

See ``docs/DECISION-CONTRACT.md`` for parser-backed field/state/error tables
and clearly labeled planned B02/B04/B08 surfaces.
"""

# NOTE: the A.E.B mirror is executed as a top-level module, so this generated
# file must not contain package-relative imports.
'''

_DOCSTRING = re.compile(r'^"""[\s\S]*?"""\n')
_DECISION_IMPORT = re.compile(
    r"^from astrbot_ex\.core\.actions\.models import \([^)]*\)\n", re.MULTILINE
)
_DECISION_IMPORT_ONE = re.compile(
    r"^from astrbot_ex\.core\.actions\.models import [^\n(]*\n", re.MULTILINE
)
_DUNDER_ALL = re.compile(r"^__all__ = \[[\s\S]*?^\]\n", re.MULTILINE)


def _strip(path: Path) -> str:
    """Remove docstring (caller handles), package imports and __all__."""
    text = path.read_text(encoding="utf-8")
    text = _DECISION_IMPORT.sub("", text)
    text = _DECISION_IMPORT_ONE.sub("", text)
    text = _DUNDER_ALL.sub("", text)
    return text


def _body(path: Path) -> str:
    text = _strip(path)
    text = _DOCSTRING.sub("", text, count=1)
    return text.strip("\n")


def _definitions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            identifiers = [target.id for target in targets if isinstance(target, ast.Name)]
            for name in identifiers:
                if name in names:
                    raise ValueError(f"duplicate top-level definition {name!r} in {path}")
                names.add(name)
            continue
        else:
            continue
        if name in names:
            raise ValueError(f"duplicate top-level definition {name!r} in {path}")
        names.add(name)
    return names


def build() -> str:
    collisions = (_definitions(ACTIONS) & _definitions(DECISION)) - {"__all__"}
    if collisions:
        raise ValueError(f"contract definitions collide: {', '.join(sorted(collisions))}")
    actions = _body(ACTIONS)
    decision = _body(DECISION)
    decision = re.sub(
        r"^from __future__ import annotations\n", "", decision, count=1, flags=re.MULTILINE
    )
    return HEADER + "\n\n" + actions + "\n\n\n# " + "=" * 72 + "\n# Decision channel\n# " + "=" * 72 + "\n\n" + decision + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the file is stale")
    args = parser.parse_args(argv)

    content = build()
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.is_file() else ""
        if current != content:
            print("contracts.py is stale; run: python scripts/build_contracts.py", file=sys.stderr)
            return 1
        print("contracts.py is current")
        return 0

    OUT.write_text(content, encoding="utf-8")
    print(f"wrote {OUT} ({len(content.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
