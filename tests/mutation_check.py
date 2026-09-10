#!/usr/bin/env python3
"""Prove the proportion gate's tests actually defend it.

    python3 tests/mutation_check.py

For each check in gate/proportion.py: delete it, run the suite, and require the
suite to go RED. A test that still passes with the mechanism removed is not
testing the mechanism — it is decoration that reports green forever.

Exit 0 when every mutant was killed; 1 when any survived; 2 when this script
itself could not run (same contract as the gate).

The file is restored from an in-memory copy in a `finally`, never with
`git checkout`: this repo may hold uncommitted work, and a checkout to undo a
mutation would take that work with it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "gate" / "proportion.py"

# (name, the call in main() to neuter, what to leave in its place)
MUTANTS = [
    ("CHECK 1 scope-spread",
     "check_scope_spread(files, cfg, findings)", ""),
    ("CHECK 2 cleanup-ratio",
     "check_cleanup_ratio(files, stats, cfg, findings)", ""),
    ("CHECK 3 undeclared-refactor",
     "check_undeclared_refactor(files, stats, messages, cfg, findings, notes)", ""),
    ("CHECK 4 mixed-commit",
     "check_mixed_commit(files, stats, cfg, findings)", ""),
    # Not a check but load-bearing all the same: without the escape valve the
    # gate has no way to be satisfied by doing the right thing, and a gate you
    # cannot satisfy gets uninstalled. The tests must notice if it disappears.
    ("THE ESCAPE VALVE",
     "declared = bool(args.scope) or bool(SCOPE_RE.search(messages))",
     "declared = False"),
    # The exemptions are the other half of survivability: a gate that fires on
    # every lockfile bump is a gate nobody keeps.
    ("THE ALLOWLIST",
     "cur.exempt = is_exempt(cur.path, allow)",
     "cur.exempt = False"),
]


def run_suite() -> bool:
    """True when the suite is green."""
    r = subprocess.run([sys.executable, "-m", "pytest", str(ROOT / "tests"), "-q",
                        "-x", "--no-header"],
                       capture_output=True, text=True, cwd=ROOT, check=False)
    return r.returncode == 0


def main() -> int:
    original = GATE.read_text(encoding="utf-8")

    if not run_suite():
        print("the suite is RED before any mutation — fix that first", file=sys.stderr)
        return 2

    survivors: list[str] = []
    try:
        for name, call, replacement in MUTANTS:
            if call not in original:
                print(f"  ?? {name}: call not found in gate — mutation list is stale")
                survivors.append(f"{name} (stale)")
                continue
            # `count` matters: `cur.exempt = is_exempt(...)` appears twice in the
            # parser, once per path source. Replacing only the first would leave
            # the mechanism half alive and the mutant killable for the wrong
            # reason, which teaches nothing.
            mutated = original.replace(call, replacement or "pass")
            GATE.write_text(mutated, encoding="utf-8")
            if run_suite():
                print(f"  SURVIVED  {name} — removed it and the suite stayed green")
                survivors.append(name)
            else:
                print(f"  killed    {name}")
    finally:
        GATE.write_text(original, encoding="utf-8")

    # The restore itself is verified. A mutation runner that leaves the file
    # mutated has done more harm than the bug it was hunting.
    if GATE.read_text(encoding="utf-8") != original:
        print("gate file was NOT restored cleanly", file=sys.stderr)
        return 2

    if survivors:
        print(f"\n{len(survivors)} mutant(s) survived: {', '.join(survivors)}")
        return 1
    print(f"\nall {len(MUTANTS)} mutants killed; gate restored and verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
