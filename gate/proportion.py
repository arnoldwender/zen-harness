#!/usr/bin/env python3
"""The Zen Harness gate: the change is the size the task asked for, and no larger.

    python3 gate/proportion.py                     # diff against origin/main
    python3 gate/proportion.py --base HEAD~1
    python3 gate/proportion.py --sarif out.json

Exit codes are part of the contract shared by the conduct-harness family:

    0   no findings (or the change is declared — see THE ESCAPE VALVE)
    1   findings
    2   the gate itself failed

The third one is not decoration. A checker that returns 1 when it crashed reads
as "I found something"; one that returns 0 reads as "clean" and fails OPEN.
This gate distinguishes its own failure from its verdict.

WHAT THIS GATE IS FOR
---------------------
SOJI's falsifier, from the codex this repo carries:

    the diff carries churn the task never asked for

That is the one failure `scripts/check.py` cannot see, because it reads the
repo's files and this reads the repo's *diff*. Sweeping serves the task and
never itself: the dead import in a file you already had open is Soji; the
reformat of forty files you were never sent to is the mission being replaced by
the broom. Both look like diligence in a report. Only the second shows up as
churn in a diff nobody asked for.

THE HONEST LIMIT OF THIS GATE
-----------------------------
It is the noisiest falsifier in the family, and the reason is structural: to
judge whether a change is proportionate you must know what the task asked for,
and THE TASK IS NOT IN THE DIFF. This gate infers intent from the commit
messages in the range, which is a real signal and a partial one. A legitimate
refactor, a scaffold, a generated-code bump and a genuine sweep all look alike
from here.

So it is built to be kept rather than to be right:

  * every threshold is configurable in `.conduct/proportion.toml`;
  * every finding is ADVICE — SARIF level `warning`, not `error`, and the CI
    job is not a required check;
  * and there is an explicit way to say "yes, this one is big on purpose".

THE ESCAPE VALVE
----------------
A commit message in the range carrying a line

    Scope: <reason>          (or)          Alcance: <razon>

declares the change and the gate passes. The findings are still printed, under
`note`, so the author sees what was waived — the Clear Mirror is not traded for
a green light. `--scope "<reason>"` does the same before a commit exists, for
pre-commit use.

A gate with no valve punishes the legitimate refactor, which is the exact
opposite of the codex: Soji rule 4 says a growing fix is "split out and NAMED",
not forbidden. Naming it is what the valve is.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The root is overridable so the tests can point the gate at a scratch repo. A
# checker that can only ever run on itself cannot be shown to work: the only way
# to prove a check has teeth is to hand it a repo with the defect planted and
# watch it go red.
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).resolve().parent.parent)
CONDUCT = ROOT / ".conduct"

# Defaults chosen to sit ABOVE ordinary work rather than at it. A gate that
# fires on a normal Tuesday gets uninstalled, and an uninstalled gate catches
# nothing at all.
DEFAULTS: dict[str, Any] = {
    # CHECK 1 — how many files a single undeclared change may touch.
    "max_files": 8,
    # CHECK 2 — the share of churn that may be pure cleanup before it needs saying.
    "cleanup_ratio": 0.5,
    # CHECK 2 — below this much churn the ratio is noise, not a signal.
    "min_lines_for_ratio": 40,
    # CHECK 3 — churn in one file that the commit message never mentions.
    "max_lines_unnamed_file": 120,
    # CHECK 4 — how many unrelated cosmetic-only files make a commit "mixed".
    "min_cosmetic_files": 3,
    # classification — a rename is cosmetic when it repeats; once is an edit.
    "rename_min_occurrences": 3,
}

# Paths whose size says nothing about the author's restraint. A lockfile moves
# five thousand lines because one dependency moved; counting that as churn would
# make the gate fire on `npm install` forever.
DEFAULT_ALLOW = (
    "*.lock", "*-lock.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "Cargo.lock", "poetry.lock", "composer.lock", "Gemfile.lock", "uv.lock",
    "go.sum", "flake.lock",
    "dist/**", "build/**", "out/**", "vendor/**", "node_modules/**",
    ".next/**", "target/**", "coverage/**",
    "**/__snapshots__/**", "*.snap",
    "**/generated/**", "*.generated.*", "*_pb2.py", "*.pb.go",
    "*.min.js", "*.min.css", "*.map",
)

# Files where `#` is a heading and `- ` is a bullet, not a comment. Classifying
# prose by comment syntax would mark half a README as cosmetic.
PROSE_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".adoc"}

COMMENT_RE = re.compile(r"^\s*(?://|/\*|\*/|\*\s|#|--\s|;;)")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SCOPE_RE = re.compile(r"^[ \t>*-]*(?:scope|alcance)[ \t]*:[ \t]*\S", re.IGNORECASE | re.MULTILINE)


class GateError(RuntimeError):
    """The gate could not run. Always exit 2, never 1 and never 0."""


@dataclass
class Finding:
    check: str
    message: str
    path: str = ""
    line: int = 0


@dataclass
class FileDiff:
    path: str
    binary: bool = False
    exempt: bool = False
    # Each hunk is (removed lines, added lines). `--unified=0` means no context.
    hunks: list[tuple[list[str], list[str]]] = field(default_factory=list)

    @property
    def changed(self) -> int:
        """Churn: an edited line counts twice, once as `-` and once as `+`."""
        return sum(len(r) + len(a) for r, a in self.hunks)


@dataclass
class Stats:
    total: dict[str, int] = field(default_factory=dict)
    cosmetic: dict[str, int] = field(default_factory=dict)
    functional: dict[str, int] = field(default_factory=dict)


# --- git ---------------------------------------------------------------------

def git(*args: str) -> str:
    r = subprocess.run(["git", "-C", str(ROOT), "-c", "core.quotepath=false", *args],
                       capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise GateError(f"git {' '.join(args)} failed: {r.stderr.strip()[:200]}")
    return r.stdout


def _rev(ref: str) -> str | None:
    r = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", "--quiet",
                        f"{ref}^{{commit}}"], capture_output=True, text=True, check=False)
    return r.stdout.strip() or None


def resolve_base(requested: str) -> tuple[str, str, str]:
    """Return (ref name used, merge-base sha, note about any substitution).

    The requested ref is tried first, then the family fallbacks. A ref that
    resolves to HEAD itself is skipped WHEN THE TREE IS CLEAN: comparing a
    commit with itself measures nothing and would report "0 files changed" with
    total confidence — the failure mode this family calls failing open. With a
    dirty tree, HEAD is the right base: the uncommitted work is exactly what
    there is to measure, which is how the gate runs from a pre-commit hook.
    """
    head = _rev("HEAD")
    if head is None:
        raise GateError("HEAD does not resolve — no git repository, or no commits yet")
    dirty = bool(git("status", "--porcelain").strip())

    tried: list[str] = []
    for ref in dict.fromkeys([requested, "origin/main", "main", "HEAD~1"]):
        tried.append(ref)
        sha = _rev(ref)
        if sha is None or (sha == head and not dirty):
            continue
        mb = subprocess.run(["git", "-C", str(ROOT), "merge-base", sha, head],
                            capture_output=True, text=True, check=False)
        base = mb.stdout.strip() or sha
        note = "" if ref == requested else (
            f"`{requested}` gave no usable base; compared against `{ref}` instead")
        return ref, base, note

    raise GateError(
        "no usable base to compare against — tried " + ", ".join(f"`{t}`" for t in tried)
        + ". Pass one with --base. The gate does not guess, because a guessed base "
          "reports a clean diff for the wrong reason.")


# --- config ------------------------------------------------------------------

def glob_to_re(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def load_config() -> dict[str, Any]:
    """`.conduct/proportion.toml`, else `.conduct/proportion.json`, else defaults.

    A malformed config is exit 2, not a shrug back to the defaults. Running with
    thresholds the author did not choose, while reporting as though they did, is
    the gate lying about what it measured.
    """
    cfg = dict(DEFAULTS)
    toml_path, json_path = CONDUCT / "proportion.toml", CONDUCT / "proportion.json"

    raw: dict[str, Any] = {}
    if toml_path.is_file():
        import tomllib
        try:
            raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        except Exception as exc:                       # noqa: BLE001 - reported, not swallowed
            raise GateError(f".conduct/proportion.toml is unreadable: {exc}") from exc
    elif json_path.is_file():
        try:
            raw = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as exc:                       # noqa: BLE001
            raise GateError(f".conduct/proportion.json is unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise GateError("the proportion config's top level is not a mapping")

    for key, value in raw.items():
        if key not in DEFAULTS:
            raise GateError(f"unknown key `{key}` in the proportion config — "
                            f"known keys: {', '.join(sorted(DEFAULTS))}")
        if isinstance(DEFAULTS[key], int) and not isinstance(value, int):
            raise GateError(f"`{key}` must be a whole number, got {value!r}")
        if isinstance(DEFAULTS[key], float) and not isinstance(value, (int, float)):
            raise GateError(f"`{key}` must be a number, got {value!r}")
        cfg[key] = value
    return cfg


def load_allowlist() -> list[re.Pattern[str]]:
    patterns = list(DEFAULT_ALLOW)
    extra = CONDUCT / "proportion-allow.txt"
    if extra.is_file():
        for raw in extra.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if line:
                patterns.append(line)
    return [glob_to_re(p) for p in patterns]


def is_exempt(path: str, allow: list[re.Pattern[str]]) -> bool:
    base = path.rsplit("/", 1)[-1]
    return any(rx.match(path) or rx.match(base) for rx in allow)


# --- diff parsing ------------------------------------------------------------

DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")


def parse_diff(text: str, allow: list[re.Pattern[str]]) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: tuple[list[str], list[str]] | None = None

    for line in text.splitlines():
        m = DIFF_HEADER_RE.match(line)
        if m:
            cur = FileDiff(path=m.group(2))
            cur.exempt = is_exempt(cur.path, allow)
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target != "/dev/null":
                cur.path = target[2:] if target.startswith("b/") else target
                cur.exempt = is_exempt(cur.path, allow)
            continue
        if line.startswith("--- "):
            continue
        if line.startswith("Binary files") or line.startswith("GIT binary patch"):
            cur.binary = True
            continue
        if line.startswith("@@"):
            hunk = ([], [])
            cur.hunks.append(hunk)
            continue
        if hunk is None:
            continue
        if line.startswith("+"):
            hunk[1].append(line[1:])
        elif line.startswith("-"):
            hunk[0].append(line[1:])
    return files


# --- classification: which churn is cleanup and which is the task -------------

def ws(s: str) -> str:
    """Collapse a line to its content: whitespace and trailing punctuation gone."""
    return " ".join(s.split()).rstrip(";,")


def skeleton(s: str) -> str:
    """The line with every identifier blanked — what a pure rename leaves alone."""
    return IDENT_RE.sub("\x00", ws(s))


def is_comment(path: str, line: str) -> bool:
    if Path(path).suffix.lower() in PROSE_SUFFIXES:
        return False
    return bool(COMMENT_RE.match(line))


def analyse(files: list[FileDiff], rename_min_occurrences: int) -> Stats:
    """Split every changed line into cleanup and task.

    Four signals count as cleanup, in this order:

      1. a blank-line change — pure whitespace, by definition;
      2. a comment-only line in a non-prose file;
      3. a MOVE or REFORMAT: a removed line whose content reappears, whitespace
         aside, among the added lines of the same file. Reordered imports and
         reindented blocks are exactly this, matched file-wide so a move across
         hunks still counts;
      4. a COSMETIC RENAME: a paired removed/added line whose skeleton is
         identical once identifiers are blanked, AND whose identifier
         substitutions repeat at least `rename_min_occurrences` times across the
         diff.

    The repetition requirement in (4) is what keeps a real fix out of the
    cleanup bucket. `-  foo(a)` / `+  foo(b)` has the same skeleton as a rename
    and is not one; a rename touches many lines with the SAME substitution, a
    one-line fix does not. Without that guard this classifier would call every
    small bugfix "cosmetic" — a false positive on the most ordinary change there
    is, which is how a gate earns its way into the bin.
    """
    stats = Stats()
    remaining: dict[str, tuple[list[list[str]], list[list[str]]]] = {}

    for fd in files:
        stats.total[fd.path] = fd.changed
        cosmetic = 0
        rem: list[list[str]] = []
        add: list[list[str]] = []

        for removed, added in fd.hunks:
            keep_r, keep_a = [], []
            for line in removed:
                if ws(line) == "" or is_comment(fd.path, line):
                    cosmetic += 1
                else:
                    keep_r.append(line)
            for line in added:
                if ws(line) == "" or is_comment(fd.path, line):
                    cosmetic += 1
                else:
                    keep_a.append(line)
            rem.append(keep_r)
            add.append(keep_a)

        # (3) move / reformat, matched file-wide as a multiset so a line that
        # moved between hunks still pairs with itself.
        available = Counter(ws(line) for hunk in rem for line in hunk)
        matched: Counter[str] = Counter()
        add_left: list[list[str]] = []
        for hunk in add:
            keep = []
            for line in hunk:
                key = ws(line)
                if matched[key] < available[key]:
                    matched[key] += 1
                    cosmetic += 1
                else:
                    keep.append(line)
            add_left.append(keep)

        consumed: Counter[str] = Counter()
        rem_left: list[list[str]] = []
        for hunk in rem:
            keep = []
            for line in hunk:
                key = ws(line)
                if consumed[key] < matched[key]:
                    consumed[key] += 1
                    cosmetic += 1
                else:
                    keep.append(line)
            rem_left.append(keep)

        stats.cosmetic[fd.path] = cosmetic
        remaining[fd.path] = (rem_left, add_left)

    # (4) cosmetic renames need a diff-wide census before any pair can be judged.
    candidates: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    for fd in files:
        rem_left, add_left = remaining[fd.path]
        for removed, added in zip(rem_left, add_left):
            for old, new in zip(removed, added):
                if ws(old) == ws(new) or skeleton(old) != skeleton(new):
                    continue
                old_ids, new_ids = IDENT_RE.findall(old), IDENT_RE.findall(new)
                if len(old_ids) != len(new_ids):
                    continue
                subs = tuple(sorted({(a, b) for a, b in zip(old_ids, new_ids) if a != b}))
                if subs:
                    candidates.append((fd.path, subs))

    census: Counter[tuple[str, str]] = Counter()
    for _, subs in candidates:
        census.update(subs)
    for path, subs in candidates:
        if all(census[s] >= rename_min_occurrences for s in subs):
            stats.cosmetic[path] += 2          # the removed half and the added half

    for path, total in stats.total.items():
        stats.cosmetic[path] = min(stats.cosmetic[path], total)
        stats.functional[path] = total - stats.cosmetic[path]
    return stats


# --- checks ------------------------------------------------------------------

def check_scope_spread(files: list[FileDiff], cfg: dict[str, Any],
                       findings: list[Finding]) -> None:
    """CHECK 1 — one task, one circle. A change that fans out says so or narrows.

    Soji rule 2: "cleanup serves the task, never itself — no tidying untouched
    ground." Breadth is the cheapest observable proxy for that, and the only one
    available without knowing the task.
    """
    live = [f for f in files if not f.exempt]
    limit = cfg["max_files"]
    if len(live) > limit:
        sample = ", ".join(f.path for f in live[:5])
        findings.append(Finding(
            "scope-spread",
            f"{len(live)} files in one change, above the threshold of {limit} "
            f"({sample}{', …' if len(live) > 5 else ''}). If the breadth is the task, "
            f"say so with a `Scope:` line in the commit message; if it is not, split it.",
            live[0].path))


def check_cleanup_ratio(files: list[FileDiff], stats: Stats, cfg: dict[str, Any],
                        findings: list[Finding]) -> None:
    """CHECK 2 — the broom did not become the mission.

    Soji: sweeping serves the task. When most of the churn is reindentation,
    reordering and renaming, the task has become the sweeping — which is fine,
    and worth one sentence in the message so a reviewer reads the diff correctly.
    """
    live = [f for f in files if not f.exempt]
    total = sum(stats.total[f.path] for f in live)
    if total < cfg["min_lines_for_ratio"]:
        return
    cosmetic = sum(stats.cosmetic[f.path] for f in live)
    ratio = cosmetic / total
    if ratio >= cfg["cleanup_ratio"]:
        findings.append(Finding(
            "cleanup-ratio",
            f"{cosmetic} of {total} changed lines ({ratio:.0%}) are formatting, "
            f"reordering or repeated renaming rather than behaviour, above the "
            f"threshold of {cfg['cleanup_ratio']:.0%}. Cleanup this size is a change "
            f"of its own: name it with a `Scope:` line, or split it from the fix.",
            live[0].path if live else ""))


def check_undeclared_refactor(files: list[FileDiff], stats: Stats, messages: str,
                              cfg: dict[str, Any], findings: list[Finding],
                              notes: list[str]) -> None:
    """CHECK 3 — a file rewritten inside a change that never mentions it.

    Soji rule 4: a fix that grows is "split out and named". This looks for the
    unnamed half — heavy churn in a file the commit message does not so much as
    reference. With no commit messages in the range there is nothing to compare
    against, and the check says so rather than passing quietly: naming what you
    could not verify is the Clear Mirror's third rule.
    """
    if not messages.strip():
        notes.append("no commit messages in the range — CHECK 3 (undeclared-refactor) "
                     "could not run, and is reported as unmeasured rather than clean")
        return
    haystack = messages.lower()
    limit = cfg["max_lines_unnamed_file"]
    for fd in files:
        if fd.exempt or stats.total[fd.path] <= limit:
            continue
        p = Path(fd.path)
        names = {p.name.lower()}
        if len(p.stem) >= 3:
            names.add(p.stem.lower())
        if p.parent.name and len(p.parent.name) >= 4:
            names.add(p.parent.name.lower())
        if any(n in haystack for n in names):
            continue
        findings.append(Finding(
            "undeclared-refactor",
            f"{stats.total[fd.path]} changed lines in a file the commit message never "
            f"names (threshold {limit}). Name the file in the message, or carve the "
            f"work out into its own change.",
            fd.path))


def check_mixed_commit(files: list[FileDiff], stats: Stats, cfg: dict[str, Any],
                       findings: list[Finding]) -> None:
    """CHECK 4 — a fix and a style sweep of unrelated ground, in one change.

    The distinctive shape: one or more files carry real behaviour change, and
    several OTHER files, in other directories, carry nothing but formatting.
    That is the fix riding along with a tidy of ground the task never touched —
    reviewable as two changes, not as one.

    "Unrelated" is directory-level on purpose. Reformatting a sibling in the
    same directory as the fix is Soji working correctly: heal in passing.
    """
    live = [f for f in files if not f.exempt and stats.total[f.path] > 0]
    functional = [f for f in live if stats.functional[f.path] > 0]
    if not functional:
        return
    touched_dirs = {str(Path(f.path).parent) for f in functional}
    stray = [f for f in live
             if stats.functional[f.path] == 0
             and str(Path(f.path).parent) not in touched_dirs]
    if len(stray) >= cfg["min_cosmetic_files"]:
        findings.append(Finding(
            "mixed-commit",
            f"{len(functional)} file(s) carry behaviour change while {len(stray)} "
            f"file(s) in unrelated directories carry only formatting "
            f"({', '.join(f.path for f in stray[:4])}). That is two changes in one "
            f"diff: split the sweep out, or declare it with a `Scope:` line.",
            stray[0].path))


# --- output ------------------------------------------------------------------

def to_sarif(findings: list[Finding]) -> dict[str, Any]:
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "zen-harness-proportion",
                "informationUri": "https://github.com/arnoldwender/zen-harness",
                "rules": [{"id": r} for r in sorted({f.check for f in findings})],
            }},
            "results": [{
                "ruleId": f.check,
                # `warning`, never `error`: this gate reasons about intent it can
                # only infer, so its output is advice a human weighs — not a verdict.
                "level": "warning",
                "message": {"text": f.message},
                "locations": [{"physicalLocation": {
                    "artifactLocation": {"uri": f.path or "."},
                    "region": {"startLine": max(f.line, 1)},
                }}],
            } for f in findings],
        }],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default="origin/main", metavar="REF",
                    help="compare against this ref (default: origin/main)")
    ap.add_argument("--sarif", metavar="PATH", help="write SARIF 2.1.0 to PATH")
    ap.add_argument("--scope", metavar="REASON",
                    help="declare the change deliberately large, for use before the "
                         "commit exists; the durable form is a `Scope:` line in the "
                         "commit message")
    args = ap.parse_args(argv)

    findings: list[Finding] = []
    notes: list[str] = []
    try:
        cfg = load_config()
        allow = load_allowlist()
        ref, base, base_note = resolve_base(args.base)
        if base_note:
            notes.append(base_note)
        files = parse_diff(git("diff", "-M", "--unified=0", base), allow)
        messages = git("log", "--format=%B", f"{base}..HEAD")
        # `git diff` cannot see a file git has never been told about. Saying so
        # is cheaper than the alternative: a confident "3 files changed" over a
        # working tree that holds thirty.
        untracked = [p for p in git("ls-files", "--others", "--exclude-standard").splitlines()
                     if p.strip()]
        if untracked:
            notes.append(f"{len(untracked)} untracked file(s) are outside the diff and were "
                         f"not measured — `git add` them to bring them in")
        stats = analyse(files, cfg["rename_min_occurrences"])
        check_scope_spread(files, cfg, findings)
        check_cleanup_ratio(files, stats, cfg, findings)
        check_undeclared_refactor(files, stats, messages, cfg, findings, notes)
        check_mixed_commit(files, stats, cfg, findings)
    except Exception as exc:                           # noqa: BLE001
        # Exit 2, never 1 and never 0: the gate broke, it did not judge.
        print(f"gate failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    declared = bool(args.scope) or bool(SCOPE_RE.search(messages))

    if args.sarif:
        payload = to_sarif([] if declared else findings)
        Path(args.sarif).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    live = [f for f in files if not f.exempt]
    total = sum(stats.total[f.path] for f in live)
    cosmetic = sum(stats.cosmetic[f.path] for f in live)
    share = f"{cosmetic / total:.0%}" if total else "0%"
    print(f"proportion: base {ref} ({base[:7]}), {len(live)} file(s), "
          f"{total} changed line(s), {share} cleanup, {len(files) - len(live)} exempt")
    for note in notes:
        print(f"  note: {note}")

    label = "note" if declared else "WARN"
    for f in findings:
        where = f"{f.path}:{f.line}" if f.line else (f.path or ".")
        print(f"  {label} [{f.check}] {where}: {f.message}")

    if declared:
        # Printed above, waived here — deliberately in that order. A valve that
        # hides what it waived would make the gate a formality.
        source = "--scope" if args.scope else "a `Scope:` line in the commit message"
        print(f"\ndeclared by {source}: {len(findings)} finding(s) waived")
        return 0
    if findings:
        print(f"\n{len(findings)} finding(s) — advisory. Declare the change with a "
              f"`Scope: <reason>` line in the commit message, adjust the thresholds in "
              f"`.conduct/proportion.toml`, or split the work.")
        return 1
    print("  the change is the size the task asked for")
    return 0


if __name__ == "__main__":
    sys.exit(main())
