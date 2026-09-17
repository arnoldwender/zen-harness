#!/usr/bin/env python3
"""The Zen Harness — proportion, weighed when the agent stops.

A Claude Code `Stop` hook. When the agent finishes its turn, this runs the
proportion gate (`gate/proportion.py`) over what the turn changed and, if the
change is larger than the task it was sent for — nine files for a one-line
fix, a diff that is mostly reformatting, a file rewritten that nothing names —
tells the HUMAN. Not the agent, by default: this gate reasons about intent it
can only infer, so its output is advice a person weighs, exactly as in CI.
See hooks/README.md for the wiring and the README for why.

    "hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "python3 /abs/path/to/zen-harness/hooks/proportion-at-stop.py",
         "timeout": 30}]}]}

WHY AT STOP, AND WHY TO THE HUMAN
---------------------------------
Proportion is a property of the whole change, so no single edit can be judged;
the end of the turn is the first moment the diff is a diff. And the gate's own
README calls it the noisiest falsifier in the family, kept advisory on purpose:
a finding is a red X a human reads and can merge past. A Stop hook that sent
the agent round again on every "nine files" would turn advice into a leash.
So the default mode is `notify` — `systemMessage`, shown to the person, never
steering the agent — and `feedback` is opt-in for whoever wants the agent to
read it too, with the same brakes the family's other Stop hooks carry.

WHAT IT DOES
------------
1. Reads the Stop payload: `cwd`, `session_id`, `stop_hook_active`.
2. Works out what THIS TURN changed: the base is the commit the tree stood at
   when this hook last ran here (from its receipts), else `HEAD`. A clean tree
   with no earlier base means the turn changed nothing the diff can see, and
   the hook records `nothing-to-measure` instead of judging the whole branch.
3. Runs the gate as a subprocess, `HARNESS_ROOT` = the working directory, so
   thresholds (`.conduct/proportion.toml`), the allowlist and the `Scope:`
   valve are the working repository's. Exit 2 — no base, a malformed config —
   is recorded as `gate-failure` and stays silent, never "clean".
4. On findings: `notify` prints `{"systemMessage": "…"}`; `feedback` prints
   `{"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": "…"}}`
   and the agent continues once; `block` uses `decision: "block"`.
5. Appends one receipt per run to `PROPORTION_RECEIPTS` (default
   `~/.local/state/zen-harness/proportion-receipts.jsonl`; `off` disables):
   `{ts, session, cwd, head, base, verdict, checks, findings, ms}`.

THE BRAKES (feedback and block modes only)
------------------------------------------
`stop_hook_active` — the runtime already sent the agent round once: record,
stay silent. The same finding signature is never fed back twice — the human is
told instead. And the runtime caps continuations at eight.

WHAT IT DOES NOT SEE
--------------------
What the gate does not: the task itself, which is not in the diff. A
legitimate refactor and an unasked-for sweep look alike from here, which is why
the default is a note to a person and why `Scope: <reason>` in the commit
message waives the verdict without hiding the report.

Tests: tests/test_proportion_hook.py · mutants: tests/mutation_check_proportion_hook.py
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
GATE = pathlib.Path(os.environ.get("PROPORTION_GATE") or HERE.parent / "gate" / "proportion.py")


def _default_receipts() -> str:
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(state, "zen-harness", "proportion-receipts.jsonl")


RECEIPTS = os.environ.get("PROPORTION_RECEIPTS") or _default_receipts()
MODE = os.environ.get("PROPORTION_HOOK_MODE", "notify")          # notify | feedback | block
# mutation-anchor: MODE
RECEIPT_TAIL_BYTES = 262_144
MAX_SHOWN = 4


# --- receipts ----------------------------------------------------------------

def receipt(**row: object) -> None:
    """One JSON line per run. Paths, counts and a commit sha — never file contents."""
    if RECEIPTS == "off":
        return
    try:
        pathlib.Path(RECEIPTS).parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **row}
        with open(RECEIPTS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — a receipt never brings the hook down
        pass


def last_receipt(session: str, cwd: str) -> dict | None:
    if RECEIPTS == "off":
        return None
    try:
        with open(RECEIPTS, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - RECEIPT_TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for raw in reversed(tail.split("\n")):
        if session not in raw or cwd not in raw:
            continue
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if r.get("session") == session and r.get("cwd") == cwd and "head" in r:
            return r
    return None


# --- git ---------------------------------------------------------------------

def git(cwd: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def turn_base(cwd: str, previous: dict | None) -> str | None:
    """The commit the tree stood at when this hook last ran here, if it still exists;
    `HEAD` when the tree is dirty; None when there is nothing the diff can see."""
    prev = str((previous or {}).get("head") or "")
    if prev and subprocess.run(["git", "-C", cwd, "cat-file", "-e", f"{prev}^{{commit}}"],
                               capture_output=True, check=False).returncode == 0:
        return prev
    dirty = bool(git(cwd, "status", "--porcelain"))
    return "HEAD" if dirty else None
# mutation-anchor: turn_base


# --- the gate ----------------------------------------------------------------

def judge(cwd: str, base: str) -> tuple[int, str, str]:
    env = {**os.environ, "HARNESS_ROOT": cwd}
    r = subprocess.run([sys.executable, str(GATE), "--base", base],
                       capture_output=True, text=True, env=env, cwd=cwd, check=False, timeout=25)
    return r.returncode, r.stdout, r.stderr


def findings_of(stdout: str) -> list[str]:
    """The gate prints `WARN [check] where: message` per finding; `note [check]` when the
    change was declared with a Scope: line, which is a waived verdict and not a finding."""
    return [line.strip()[5:] for line in stdout.split("\n") if line.strip().startswith("WARN ")]


def signature(findings: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(findings)).encode("utf-8")).hexdigest()[:16]


def message(findings: list[str]) -> str:
    shown = findings[:MAX_SHOWN]
    more = f" (+{len(findings) - MAX_SHOWN} more)" if len(findings) > MAX_SHOWN else ""
    return ("proportion (advisory): this turn's change may be larger than the task it was sent "
            "for. " + " ".join(shown) + more
            + " Sōji: sweeping serves the task, never itself. Nothing is blocked; a `Scope: "
            "<reason>` line in the commit message waives the verdict without hiding the report.")


# --- main --------------------------------------------------------------------

def main() -> int:
    t0 = time.time()
    payload = json.loads(sys.stdin.read() or "{}")
    if payload.get("hook_event_name") not in (None, "Stop", "SubagentStop"):
        return 0
    cwd = str(payload.get("cwd") or os.getcwd())
    session = str(payload.get("session_id") or "")[:8]
    common = {"session": session, "cwd": cwd, "mode": MODE}

    head = git(cwd, "rev-parse", "HEAD")
    if not head:
        receipt(verdict="not-a-repo", **common)
        return 0
    previous = last_receipt(session, cwd)
    base = turn_base(cwd, previous)
    if base is None or (base == head and not git(cwd, "status", "--porcelain")):
        receipt(verdict="nothing-to-measure", head=head, **common)
        return 0
    code, out, err = judge(cwd, base)
    ms = int((time.time() - t0) * 1000)

    if code == 2:
        receipt(verdict="gate-failure", head=head, base=base, error=err.strip()[:200], ms=ms, **common)
        return 0
    findings = findings_of(out) if code == 1 else []
    if not findings:
        receipt(verdict="declared" if "waived" in out else "ok", head=head, base=base, ms=ms, **common)
        return 0

    sig = signature(findings)
    checks = sorted({f.split("]", 1)[0].lstrip("[") for f in findings if f.startswith("[")})
    already = bool(payload.get("stop_hook_active"))
    # mutation-anchor: stop_hook_active
    repeated = bool(previous) and previous.get("signature") == sig
    mode = MODE
    if mode != "notify" and (already or repeated):
        mode = "notify"
    receipt(verdict="finding", nudged=mode != "notify", head=head, base=base, checks=checks,
            findings=len(findings), signature=sig, ms=ms, **common)
    text = message(findings)
    if mode == "block":
        print(json.dumps({"decision": "block", "reason": text}, ensure_ascii=False))
    elif mode == "feedback":
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "Stop",
                                                 "additionalContext": text}}, ensure_ascii=False))
    else:
        print(json.dumps({"systemMessage": text}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail open on purpose: the hook never keeps a session from ending
        receipt(verdict="error", error=f"{type(exc).__name__}: {exc}"[:200])
        sys.exit(0)
