# Hooks — keeping the practices present

The Codex only works if it's *in context* when the agent acts. A one-time paste into
`AGENTS.md` works; a hook makes it automatic, every session, and opens each run with the
first word.

## `session-start.sh`

Emits, to stdout:

1. The **first word** — a fixed line, then a rotating line of the day (`bin/precept`, drawn
   from `precepts.txt`).
2. The **conduct block** — the four practices, precedence, and the hard limit
   (`codex-block.md`).

It's harness-agnostic: any harness that can run a command at session start can use it, and
its stdout is plain readable text.

## Wiring it into Claude Code

Claude Code injects a `SessionStart` hook's stdout into the session context. Add to your
`settings.json` (use the **absolute** path, and check your Claude Code version's hook docs —
the schema evolves):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          { "type": "command", "command": "/abs/path/to/zen-harness/hooks/session-start.sh" }
        ]
      }
    ]
  }
}
```

## Wiring it into any other harness

Run `hooks/session-start.sh` as the first step of your session bootstrap and prepend its
output to the system prompt. The first word goes first, the practices stay present.

## `proportion-at-stop.py` — proportion, weighed when the agent stops

A Claude Code `Stop` hook. When the agent finishes its turn, it runs
[`gate/proportion.py`](../gate/proportion.py) over what the turn changed and, if the change is
larger than the task it was sent for — nine files for a one-line fix, a diff that is mostly
reformatting, a file rewritten that nothing names — tells the **human**. Not the agent, by
default:

> proportion (advisory): this turn's change may be larger than the task it was sent for.
> [scope-spread] src/mod0.py: 10 files in one change, above the threshold of 8 (src/mod0.py,
> src/mod1.py, src/mod2.py, src/mod3.py, src/mod4.py, …). If the breadth is the task, say so
> with a `Scope:` line in the commit message; if it is not, split it. Sōji: sweeping serves the
> task, never itself. Nothing is blocked; a `Scope: <reason>` line in the commit message waives
> the verdict without hiding the report.

Why at Stop: proportion is a property of the whole change, so no single edit can be judged;
the end of the turn is the first moment the diff is a diff. Why to the human: the gate's own
README calls it the noisiest falsifier in the family and keeps it advisory on purpose — it
reasons about intent it can only infer. A Stop hook that sent the agent round again on every
"nine files" would turn advice into a leash. So the default is `notify`: `systemMessage`,
shown to the person, never steering the agent.

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          { "type": "command",
            "command": "python3 /abs/path/to/zen-harness/hooks/proportion-at-stop.py",
            "timeout": 30 }
        ]
      }
    ]
  }
}
```

It judges **the turn**, not the working tree: the base is the commit the tree stood at when
the hook last ran for this session and directory (kept in its receipts), so a fan-out committed
during the turn is still measured. A clean tree with no earlier base is `nothing-to-measure`,
not a verdict over the whole branch. The gate runs with the working directory as its root, so
the thresholds in `.conduct/proportion.toml`, the allowlist and the `Scope:` valve are the
working repository's; a change declared with `Scope:` is recorded as `declared`, not reported.
The gate's exit 2 — no base, a malformed config — is a `gate-failure` receipt and silence,
never "clean".

Modes: `PROPORTION_HOOK_MODE=notify` (default — `systemMessage`, the human sees it),
`feedback` (`additionalContext`, the agent reads it and continues once), `block` (the runtime's
`decision: "block"`). The two that steer carry the family's brakes: `stop_hook_active` (a stop
hook already sent the agent round — record, stay silent), the same finding is never fed back
twice (the human is told instead), and the runtime's own cap of eight continuations. Receipts
in `~/.local/state/zen-harness/proportion-receipts.jsonl` (`PROPORTION_RECEIPTS=…` to move,
`off` to disable) — paths, counts and a commit sha, never file contents. Any error of its own
is a receipt and exit 0 — the hook is never the reason a session cannot end.

Tests: [`tests/test_proportion_hook.py`](../tests/test_proportion_hook.py) ·
mutants: [`tests/mutation_check_proportion_hook.py`](../tests/mutation_check_proportion_hook.py).

## Just want to see it?

```sh
./hooks/session-start.sh
```
