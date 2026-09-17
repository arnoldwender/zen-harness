#!/usr/bin/env python3
"""CI helper: assert a Stop-hook output is a note to the human and not a steer.

    python3 tests/assert_stop_note.py out.json

Used by the planted-fan-out step in .github/workflows/gate.yml. Kept as a file
rather than an inline heredoc so the assertion is versioned, re-runnable and
readable on its own. Exit 0 when the output carries `systemMessage` naming
`scope-spread` and carries neither `hookSpecificOutput` nor `decision`; exit 1
otherwise, printing what it got.
"""

from __future__ import annotations

import json
import sys


def main(path: str) -> int:
    with open(path, encoding="utf-8") as fh:
        raw = fh.read().strip()
    out = json.loads(raw) if raw else {}
    note = out.get("systemMessage", "")
    if "scope-spread" not in note:
        print(f"no note to the human about the planted fan-out: {out!r}")
        return 1
    if "hookSpecificOutput" in out or "decision" in out:
        print(f"the advisory hook steered the agent: {out!r}")
        return 1
    print("planted fan-out reached the human and did not steer the agent")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "out.json"))
