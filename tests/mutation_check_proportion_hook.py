#!/usr/bin/env python3
"""Prove the live hook's tests actually defend it.

    python3 tests/mutation_check_proportion_hook.py

For each mechanism in hooks/proportion-at-stop.py: neuter it in a scratch COPY,
run the hook's suite against the copy, and require the suite to go RED. The
real hook is never rewritten; its bytes are compared before and after anyway.
The gate's own mechanisms are defended by tests/mutation_check.py.

Exit 0 when every mutant was killed; 1 when any survived; 2 when this script
itself could not run.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "proportion-at-stop.py"
SUITE = ROOT / "tests" / "test_proportion_hook.py"

MUTANTS = [
    ("ADVISORY the default steers the agent instead of telling the human",
     'MODE = os.environ.get("PROPORTION_HOOK_MODE", "notify")',
     'MODE = os.environ.get("PROPORTION_HOOK_MODE", "feedback")'),
    ("LOOP feedback mode nudges again on a continuation, and repeats a finding",
     "    if mode != \"notify\" and (already or repeated):\n        mode = \"notify\"",
     "    pass"),
    ("TURN the base is always the dirty tree, so a committed fan-out is invisible",
     '    prev = str((previous or {}).get("head") or "")', '    prev = ""'),
    ("MEASURE a clean tree with no base is judged against the whole branch",
     '    return "HEAD" if dirty else None', '    return "HEAD"'),
    ("FINDING a fan-out produces no text",
     "    if not findings:\n        receipt(verdict=\"declared\" if \"waived\" in out else \"ok\", head=head, base=base, ms=ms, **common)\n        return 0",
     "    if True:\n        receipt(verdict=\"declared\" if \"waived\" in out else \"ok\", head=head, base=base, ms=ms, **common)\n        return 0"),
    ("EVIDENCE a gate that could not judge reads as clean",
     "    if code == 2:", "    if False:"),
]


def run_suite(hook: Path) -> bool:
    env = {**os.environ, "PROPORTION_HOOK_UNDER_TEST": str(hook),
           "PROPORTION_GATE": str(ROOT / "gate" / "proportion.py")}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(SUITE), "-q", "-x", "--no-header",
         "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=ROOT, env=env, check=False)
    return result.returncode == 0


def main() -> int:
    original = HOOK.read_text(encoding="utf-8")
    if not run_suite(HOOK):
        print("the suite is RED before any mutation — fix that first", file=sys.stderr)
        return 2
    survivors: list[str] = []
    with tempfile.TemporaryDirectory() as scratch:
        mutant = Path(scratch) / "proportion-at-stop.py"
        for name, old, new in MUTANTS:
            if original.count(old) != 1:
                print(f"  ?? {name}: anchor appears {original.count(old)} times — the "
                      f"mutation list is stale, so this script is measuring nothing")
                survivors.append(f"{name} (stale)")
                continue
            mutant.write_text(original.replace(old, new, 1), encoding="utf-8")
            if run_suite(mutant):
                print(f"  SURVIVED  {name} — removed it and the suite stayed green")
                survivors.append(name)
            else:
                print(f"  killed    {name}")
    if HOOK.read_text(encoding="utf-8") != original:
        print("the real hook file changed during the run — it must never be touched",
              file=sys.stderr)
        return 2
    if survivors:
        print(f"\n{len(survivors)} mutant(s) survived: {', '.join(survivors)}")
        return 1
    print(f"\nall {len(MUTANTS)} mutants killed; the real hook was never rewritten")
    return 0


if __name__ == "__main__":
    sys.exit(main())
