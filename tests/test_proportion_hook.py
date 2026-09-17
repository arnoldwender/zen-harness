"""Tests for the live hook, hooks/proportion-at-stop.py.

A turn that fans out past the threshold must be brought to the HUMAN's
attention at Stop — `systemMessage`, not a steer — and a turn the size of its
task must end in silence. The hook is run as a Stop subprocess with the payload
on stdin inside a small git repo; the exit code, the stdout JSON and the
receipt are what is asserted.

    python3 -m pytest tests/test_proportion_hook.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# The mutation runner points this at a mutated COPY; the real hook is never rewritten.
HOOK = Path(os.environ.get("PROPORTION_HOOK_UNDER_TEST")
            or Path(__file__).resolve().parent.parent / "hooks" / "proportion-at-stop.py")


def sh(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Twelve small source files under one directory, committed once."""
    root = tmp_path / "repo"
    write(root, "README.md", "# A project\n")
    for i in range(12):
        write(root, f"src/mod{i}.py", f"VALUE_{i} = {i}\n\n\ndef f{i}():\n    return VALUE_{i}\n")
    hooks = tmp_path / "nohooks"
    hooks.mkdir()
    sh(root, "init", "-q", "-b", "main")
    sh(root, "config", "core.hooksPath", str(hooks))
    sh(root, "config", "user.email", "gate@example.invalid")
    sh(root, "config", "user.name", "Proportion Hook Test")
    sh(root, "config", "commit.gpgsign", "false")
    sh(root, "add", "-A")
    sh(root, "commit", "-q", "-m", "base")
    return root


def fan_out(root: Path, n: int) -> None:
    """Touch n files with a one-line behaviour change each — a change wider than any task."""
    for i in range(n):
        write(root, f"src/mod{i}.py", f"VALUE_{i} = {i + 100}\n\n\ndef f{i}():\n    return VALUE_{i}\n")


def stop(root: Path, receipts: Path, active: bool = False, env: dict[str, str] | None = None,
         session: str = "test-session") -> tuple[int, dict | None, str, dict | None]:
    payload = {"session_id": session, "cwd": str(root), "hook_event_name": "Stop",
               "stop_hook_active": active, "last_assistant_message": "done"}
    run_env = {**os.environ, "PROPORTION_RECEIPTS": str(receipts)}
    run_env.pop("PROPORTION_HOOK_MODE", None)
    run_env.update(env or {})
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                       capture_output=True, text=True, env=run_env, cwd=str(root),
                       check=False, timeout=90)
    out = json.loads(r.stdout) if r.stdout.strip() else None
    rec = None
    if receipts.is_file():
        rec = json.loads(receipts.read_text(encoding="utf-8").strip().split("\n")[-1])
    return r.returncode, out, r.stderr, rec


def feedback(out: dict | None) -> str:
    return ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext") or ""


# --- the control -------------------------------------------------------------

def test_a_turn_the_size_of_its_task_ends_in_silence(repo: Path, tmp_path: Path) -> None:
    """Without this, every test below could pass because the hook always speaks."""
    fan_out(repo, 3)
    rc, out, _, rec = stop(repo, tmp_path / "r.jsonl")
    assert rc == 0 and out is None, (rc, out)
    assert rec["verdict"] == "ok"


def test_a_clean_tree_with_no_earlier_base_measures_nothing(repo: Path, tmp_path: Path) -> None:
    """The first stop of a session over a clean tree: the turn changed nothing the diff can
    see, and the whole branch is not the turn."""
    rc, out, _, rec = stop(repo, tmp_path / "r.jsonl")
    assert rc == 0 and out is None and rec["verdict"] == "nothing-to-measure"


# --- the finding, to the human -----------------------------------------------

def test_a_fan_out_past_the_threshold_is_a_note_to_the_human(repo: Path, tmp_path: Path) -> None:
    fan_out(repo, 10)
    rc, out, _, rec = stop(repo, tmp_path / "r.jsonl")
    assert rc == 0
    assert "systemMessage" in out and "scope-spread" in out["systemMessage"]
    assert "Scope:" in out["systemMessage"]
    assert "hookSpecificOutput" not in out and "decision" not in out, "advisory never steers"
    assert rec["verdict"] == "finding" and rec["nudged"] is False and rec["checks"] == ["scope-spread"]


def test_a_declared_change_is_waived_not_reported(repo: Path, tmp_path: Path) -> None:
    """`Scope:` in a commit message in the range waives the verdict."""
    receipts = tmp_path / "r.jsonl"
    stop(repo, receipts)                                   # records the base
    fan_out(repo, 10)
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "Widen every module\n\nScope: the task was the sweep")
    _, out, _, rec = stop(repo, receipts)
    assert out is None and rec["verdict"] == "declared"


# --- the turn, not the working tree ------------------------------------------

def test_a_fan_out_committed_during_the_turn_is_still_measured(repo: Path, tmp_path: Path) -> None:
    receipts = tmp_path / "r.jsonl"
    _, _, _, rec = stop(repo, receipts)
    assert rec["verdict"] == "nothing-to-measure" and len(rec["head"]) == 40
    fan_out(repo, 10)
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "widen")
    _, out, _, rec = stop(repo, receipts)
    assert out is not None and "scope-spread" in out["systemMessage"], (out, rec)
    assert rec["base"] != "HEAD" and len(rec["base"]) == 40


def test_a_base_that_no_longer_exists_falls_back_to_the_dirty_tree(repo: Path, tmp_path: Path) -> None:
    receipts = tmp_path / "r.jsonl"
    receipts.write_text(json.dumps({"ts": "2026-01-01T00:00:00Z", "session": "test-ses",
                                    "cwd": str(repo), "head": "0" * 40, "verdict": "ok"}) + "\n",
                        encoding="utf-8")
    fan_out(repo, 10)
    _, out, _, rec = stop(repo, receipts)
    assert out is not None and rec["base"] == "HEAD"


# --- feedback mode and its brakes --------------------------------------------

def test_feedback_mode_lets_the_agent_read_it_once(repo: Path, tmp_path: Path) -> None:
    fan_out(repo, 10)
    _, out, _, rec = stop(repo, tmp_path / "r.jsonl", env={"PROPORTION_HOOK_MODE": "feedback"})
    assert "scope-spread" in feedback(out) and rec["nudged"] is True


def test_feedback_mode_stays_silent_when_the_turn_is_already_a_continuation(repo: Path, tmp_path: Path) -> None:
    fan_out(repo, 10)
    _, out, _, rec = stop(repo, tmp_path / "r.jsonl", active=True, env={"PROPORTION_HOOK_MODE": "feedback"})
    assert not feedback(out) and "systemMessage" in out and rec["nudged"] is False


def test_feedback_mode_does_not_feed_the_same_finding_back_twice(repo: Path, tmp_path: Path) -> None:
    receipts = tmp_path / "r.jsonl"
    fan_out(repo, 10)
    _, out1, _, rec1 = stop(repo, receipts, env={"PROPORTION_HOOK_MODE": "feedback"})
    assert feedback(out1) and rec1["nudged"] is True
    _, out2, _, rec2 = stop(repo, receipts, env={"PROPORTION_HOOK_MODE": "feedback"})
    assert not feedback(out2) and "systemMessage" in out2 and rec2["nudged"] is False


def test_block_mode_uses_the_runtime_s_block_decision(repo: Path, tmp_path: Path) -> None:
    fan_out(repo, 10)
    _, out, _, _ = stop(repo, tmp_path / "r.jsonl", env={"PROPORTION_HOOK_MODE": "block"})
    assert out["decision"] == "block" and "scope-spread" in out["reason"]


# --- fail-open ---------------------------------------------------------------

def test_outside_a_git_repository_the_hook_records_and_ends(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    rc, out, _, rec = stop(plain, tmp_path / "r.jsonl")
    assert rc == 0 and out is None and rec["verdict"] == "not-a-repo"


def test_a_malformed_config_is_a_gate_failure_not_a_pass(repo: Path, tmp_path: Path) -> None:
    write(repo, ".conduct/proportion.toml", "max_files = 'nine'\n")
    fan_out(repo, 10)
    rc, out, _, rec = stop(repo, tmp_path / "r.jsonl")
    assert rc == 0 and out is None and rec["verdict"] == "gate-failure"


def test_a_missing_gate_fails_open_with_a_receipt(repo: Path, tmp_path: Path) -> None:
    fan_out(repo, 10)
    rc, out, _, rec = stop(repo, tmp_path / "r.jsonl", env={"PROPORTION_GATE": str(tmp_path / "none.py")})
    assert rc == 0 and out is None and rec["verdict"] in ("error", "gate-failure")


def test_receipts_can_be_switched_off(repo: Path, tmp_path: Path) -> None:
    fan_out(repo, 10)
    rc, out, _, _ = stop(repo, tmp_path / "r.jsonl", env={"PROPORTION_RECEIPTS": "off"})
    assert rc == 0 and out is not None and "scope-spread" in out["systemMessage"]
    assert not (tmp_path / "r.jsonl").exists()


def test_the_working_repository_s_thresholds_apply(repo: Path, tmp_path: Path) -> None:
    write(repo, ".conduct/proportion.toml", "max_files = 20\n")
    fan_out(repo, 10)
    _, out, _, rec = stop(repo, tmp_path / "r.jsonl")
    assert out is None and rec["verdict"] == "ok"


def test_other_events_are_ignored(repo: Path, tmp_path: Path) -> None:
    receipts = tmp_path / "r.jsonl"
    payload = {"session_id": "s", "cwd": str(repo), "hook_event_name": "PreToolUse", "tool_name": "Bash"}
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True,
                       text=True, env={**os.environ, "PROPORTION_RECEIPTS": str(receipts)}, check=False)
    assert r.returncode == 0 and not r.stdout.strip() and not receipts.exists()


def test_the_note_stays_far_below_the_runtime_cap(repo: Path, tmp_path: Path) -> None:
    fan_out(repo, 12)
    _, out, _, _ = stop(repo, tmp_path / "r.jsonl")
    assert 0 < len(out["systemMessage"]) < 3000
