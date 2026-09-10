"""Mutation tests for the proportion gate.

Every check gets the same treatment: build a repo the gate PASSES, then plant
the one defect that check exists to catch, and require the gate to go red. Then
plant the DECLARED form of the same change and require it to go green — because
a gate that cannot be satisfied by doing the right thing is not a gate, it is an
obstacle, and obstacles get removed.

    python3 -m pytest tests/ -q

The gate is invoked as a subprocess rather than imported, because the exit code
is part of the contract the whole conduct-harness family shares (0 clean,
1 findings, 2 the gate itself broke). Importing would test the functions and
leave the contract untested.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parent.parent / "gate" / "proportion.py"

# A throwaway identity for the scratch repos. Not a real address: scripts/check.py
# scans every tracked file for anything that looks like a private one.
IDENT = ("-c", "user.email=gate@example.invalid", "-c", "user.name=Proportion Gate")


def sh(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, check=True)
    return r.stdout


def commit(repo: Path, message: str) -> None:
    sh(repo, "add", "-A")
    sh(repo, *IDENT, "commit", "-q", "-m", message)


def write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HARNESS_ROOT": str(repo)}
    return subprocess.run([sys.executable, str(GATE), *args],
                          capture_output=True, text=True, env=env, check=False)


# --- fixtures ----------------------------------------------------------------

def body(n: int, indent: str = "    ") -> str:
    """A function body of `n` distinct lines, so nothing pairs by accident."""
    return "".join(f"{indent}step_{i} = compute_{i}(payload, {i})\n" for i in range(n))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with one base commit. Every test adds the second commit itself.

    No `--base` is passed anywhere below, so the gate's own fallback chain runs:
    `origin/main` is absent, `main` resolves to HEAD and is refused for measuring
    nothing, and `HEAD~1` is what it lands on.
    """
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    write(tmp_path, "src/app.py", "def handle(payload):\n" + body(6) + "    return payload\n")
    write(tmp_path, "README.md", "# scratch\n")
    commit(tmp_path, "[Init] Base")
    return tmp_path


# --- the control -------------------------------------------------------------

def test_a_small_focused_change_passes(repo: Path) -> None:
    """Without this, every test below could pass because the gate always fails."""
    write(repo, "src/app.py",
          "def handle(payload):\n" + body(6) + "    return normalise(payload)\n")
    commit(repo, "[Fix] Normalizar el payload antes de devolverlo")
    r = run(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "the size the task asked for" in r.stdout


def test_an_untouched_tree_passes(repo: Path) -> None:
    """A second commit that changes nothing measurable is still exit 0."""
    write(repo, "src/app.py", (repo / "src/app.py").read_text(encoding="utf-8"))
    sh(repo, *IDENT, "commit", "-q", "--allow-empty", "-m", "[Chore] Nada")
    assert run(repo).returncode == 0


# --- CHECK 1: scope-spread ---------------------------------------------------

def spread(repo: Path, count: int) -> None:
    for i in range(count):
        write(repo, f"mod/feature_{i}.py", f"VALUE_{i} = {i}\nNAME_{i} = 'n{i}'\n")


def test_a_change_fanning_out_over_many_files_is_caught(repo: Path) -> None:
    spread(repo, 12)
    commit(repo, "[Fix] Ajuste puntual")
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "scope-spread" in r.stdout


def test_the_same_fan_out_declared_with_scope_passes(repo: Path) -> None:
    """The declared form. Soji rule 4: a fix that grows is split out and NAMED."""
    spread(repo, 12)
    commit(repo, "[Feat] Alta de doce modulos\n\nScope: el alta del paquete es la tarea, "
                 "no un barrido de paso.")
    r = run(repo)
    assert r.returncode == 0, r.stdout
    assert "declared by" in r.stdout


def test_alcance_works_as_the_spanish_form_of_the_valve(repo: Path) -> None:
    spread(repo, 12)
    commit(repo, "[Feat] Alta de doce modulos\n\nAlcance: el alta del paquete es la tarea.")
    assert run(repo).returncode == 0


def test_the_scope_flag_declares_a_change_with_no_commit_yet(repo: Path) -> None:
    """Pre-commit use: the work is staged and there is no message to carry a trailer.

    A dirty tree makes HEAD a legitimate base — the uncommitted work is exactly
    what there is to measure. `git add` matters: git cannot diff a file it has
    never been told about, and the gate says so in a note rather than reporting
    a confident count over a tree it only half saw.
    """
    spread(repo, 12)
    sh(repo, "add", "-A")
    assert run(repo).returncode == 1
    assert run(repo, "--scope", "alta del paquete completo").returncode == 0


def test_untracked_files_are_reported_as_unmeasured(repo: Path) -> None:
    spread(repo, 12)
    r = run(repo)
    assert "untracked file(s) are outside the diff" in r.stdout


# --- CHECK 2: cleanup-ratio --------------------------------------------------

def reindented(repo: Path) -> None:
    """Commit a thirty-line function, then reindent it end to end. Pure sweeping."""
    write(repo, "src/app.py", "def handle(payload):\n" + body(30) + "    return payload\n")
    commit(repo, "[Init] app.py con treinta pasos")
    write(repo, "src/app.py",
          "def handle(payload):\n" + body(30, indent="        ") + "        return payload\n")


def test_a_diff_that_is_mostly_reformatting_is_caught(repo: Path) -> None:
    reindented(repo)
    commit(repo, "[Fix] app.py devuelve el payload")
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "cleanup-ratio" in r.stdout


def test_the_same_reformat_declared_passes(repo: Path) -> None:
    reindented(repo)
    commit(repo, "[Style] Reindentar app.py\n\nScope: la reindentacion ES la tarea.")
    assert run(repo).returncode == 0


def test_reordering_imports_counts_as_cleanup_not_as_work(repo: Path) -> None:
    """The same lines in a different order is a move, and a move is sweeping."""
    imports = [f"import package_{i}\n" for i in range(24)]
    write(repo, "src/app.py", "".join(imports) + "\ndef handle(payload):\n    return payload\n")
    commit(repo, "[Chore] Imports en app.py")
    write(repo, "src/app.py",
          "".join(reversed(imports)) + "\ndef handle(payload):\n    return payload\n")
    commit(repo, "[Chore] Otro toque")
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "cleanup-ratio" in r.stdout


def test_a_real_rewrite_is_not_mistaken_for_cleanup(repo: Path) -> None:
    """The false positive that would sink this gate: work read as formatting.

    Sixty-two changed lines, every one of them a different statement doing a
    different thing. If this reads as cleanup the gate is useless, because the
    biggest genuine changes are the ones it would flag hardest.
    """
    write(repo, "src/app.py", "def handle(payload):\n" + body(30) + "    return payload\n")
    commit(repo, "[Init] app.py con treinta pasos")
    write(repo, "src/app.py",
          "def handle(payload):\n"
          + "".join(f"    checked_{i} = validate(payload['k{i}'], strict=True) or FALLBACK\n"
                    for i in range(30))
          + "    return checked_0\n")
    commit(repo, "[Feat] Validacion estricta en app.py")
    r = run(repo)
    assert r.returncode == 0, f"the gate called real work cleanup:\n{r.stdout}"


def test_a_one_line_fix_is_not_read_as_a_cosmetic_rename(repo: Path) -> None:
    """`foo(a)` -> `foo(b)` has a rename's shape and is an edit.

    Without the repetition guard the classifier marks the most ordinary change
    there is as cleanup — and a gate that fires on every bugfix is deleted in a
    week, deservedly.
    """
    src = (repo / "src/app.py").read_text(encoding="utf-8")
    write(repo, "src/app.py", src.replace("step_3 = compute_3(payload, 3)",
                                          "step_3 = compute_3(payload, 4)"))
    commit(repo, "[Fix] El tercer paso usaba el indice equivocado")
    assert run(repo).returncode == 0


# --- CHECK 3: undeclared-refactor --------------------------------------------

def test_heavy_churn_in_a_file_the_message_never_names_is_caught(repo: Path) -> None:
    write(repo, "src/helper.py", "def helper(payload):\n" + body(150) + "    return payload\n")
    commit(repo, "[Fix] Ajustar el saludo")
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "undeclared-refactor" in r.stdout


def test_naming_the_file_in_the_message_passes(repo: Path) -> None:
    """The declared form for CHECK 3 is not the valve — it is simply saying so."""
    write(repo, "src/helper.py", "def helper(payload):\n" + body(150) + "    return payload\n")
    commit(repo, "[Feat] Nuevo src/helper.py con la ruta de calculo")
    r = run(repo)
    assert r.returncode == 0, r.stdout


def test_check_three_reports_itself_unmeasured_with_no_commit_in_range(repo: Path) -> None:
    """An honest gap beats a confident guess: it says so instead of passing quietly.

    Staged work with no commit yet has no message to compare a filename against.
    The check does not run — and does not pretend it did.
    """
    write(repo, "src/helper.py", "def helper(payload):\n" + body(150) + "    return payload\n")
    sh(repo, "add", "-A")
    r = run(repo)
    assert "CHECK 3 (undeclared-refactor) could not run" in r.stdout
    assert "WARN [undeclared-refactor]" not in r.stdout


# --- CHECK 4: mixed-commit ---------------------------------------------------

def mixed(repo: Path) -> None:
    for name in ("alpha", "beta", "gamma"):
        write(repo, f"tools/{name}.py", "def run():\n" + body(4) + "    return None\n")
    commit(repo, "[Init] tools")
    # The fix.
    write(repo, "src/app.py",
          "def handle(payload):\n" + body(6) + "    return normalise(payload)\n")
    # The unrelated sweep: same lines, deeper indent.
    for name in ("alpha", "beta", "gamma"):
        write(repo, f"tools/{name}.py", "def run():\n" + body(4, indent="        ")
              + "        return None\n")


def test_a_fix_plus_a_sweep_of_unrelated_files_is_caught(repo: Path) -> None:
    mixed(repo)
    commit(repo, "[Fix] Normalizar el payload")
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "mixed-commit" in r.stdout


def test_the_same_mixed_change_declared_passes(repo: Path) -> None:
    mixed(repo)
    commit(repo, "[Fix] Normalizar el payload\n\nScope: la reindentacion de tools/ va con "
                 "el fix a proposito, es un solo cambio de estilo acordado.")
    assert run(repo).returncode == 0


def test_reformatting_a_sibling_in_the_same_directory_is_not_mixed(repo: Path) -> None:
    """Heal in passing is Soji working, not Soji failing. It must not fire."""
    for name in ("alpha", "beta", "gamma"):
        write(repo, f"src/{name}.py", "def run():\n" + body(4) + "    return None\n")
    commit(repo, "[Init] src helpers")
    write(repo, "src/app.py",
          "def handle(payload):\n" + body(6) + "    return normalise(payload)\n")
    for name in ("alpha", "beta", "gamma"):
        write(repo, f"src/{name}.py", "def run():\n" + body(4, indent="        ")
              + "        return None\n")
    commit(repo, "[Fix] Normalizar el payload en src/app.py")
    r = run(repo)
    assert "mixed-commit" not in r.stdout, r.stdout


# --- exemptions: the paths whose size a tool decides, not a person -----------

def test_a_five_thousand_line_lockfile_does_not_trip_the_gate(repo: Path) -> None:
    write(repo, "package-lock.json",
          "\n".join(f'  "package-{i}": {{ "version": "1.0.{i}" }},' for i in range(5000)))
    commit(repo, "[Chore] Actualizar una dependencia")
    r = run(repo)
    assert r.returncode == 0, r.stdout
    assert "1 exempt" in r.stdout


def test_generated_output_and_snapshots_are_exempt(repo: Path) -> None:
    for i in range(6):
        write(repo, f"dist/bundle-{i}.js", "\n".join(f"var x{j}={j};" for j in range(200)))
        write(repo, f"tests/__snapshots__/case-{i}.snap", "\n".join(f"line {j}" for j in range(200)))
        write(repo, f"src/generated/model_{i}.py", "\n".join(f"F_{j} = {j}" for j in range(200)))
    commit(repo, "[Chore] Regenerar artefactos")
    r = run(repo)
    assert r.returncode == 0, r.stdout
    assert "0 file(s)" in r.stdout


def test_an_allowlist_entry_in_conduct_exempts_a_path(repo: Path) -> None:
    for i in range(12):
        write(repo, f"fixtures/case_{i}.py", f"CASE_{i} = {i}\n")
    commit(repo, "[Init] fixtures")
    for i in range(12):
        write(repo, f"fixtures/case_{i}.py", f"CASE_{i} = {i + 1}\n")
    commit(repo, "[Chore] Regenerar fixtures")
    assert run(repo).returncode == 1
    write(repo, ".conduct/proportion-allow.txt", "# generated by the fixture builder\nfixtures/**\n")
    commit(repo, "[Chore] Eximir fixtures/ del gate de proporcion")
    assert run(repo).returncode == 0


# --- configuration: the thresholds are the repo's, not the gate's ------------

def test_a_raised_threshold_in_toml_lets_the_wide_diff_through(repo: Path) -> None:
    spread(repo, 12)
    write(repo, ".conduct/proportion.toml", "max_files = 50\n")
    commit(repo, "[Feat] Alta de doce modulos")
    r = run(repo)
    assert r.returncode == 0, r.stdout


def test_the_json_config_works_the_same_way(repo: Path) -> None:
    spread(repo, 12)
    write(repo, ".conduct/proportion.json", json.dumps({"max_files": 50}))
    commit(repo, "[Feat] Alta de doce modulos")
    assert run(repo).returncode == 0


def test_a_lowered_threshold_makes_an_ordinary_diff_fail(repo: Path) -> None:
    """The config is load-bearing in both directions, or it is decoration."""
    write(repo, "src/app.py",
          "def handle(payload):\n" + body(6) + "    return normalise(payload)\n")
    write(repo, "docs.md", "# doc\n")
    write(repo, ".conduct/proportion.toml", "max_files = 1\n")
    commit(repo, "[Fix] Normalizar el payload")
    r = run(repo)
    assert r.returncode == 1
    assert "scope-spread" in r.stdout


# --- the contract: exit 2 is the gate's own failure --------------------------

def test_a_broken_config_is_exit_two_not_a_shrug_to_the_defaults(repo: Path) -> None:
    """Measuring with thresholds the author did not choose is the gate lying."""
    spread(repo, 12)
    write(repo, ".conduct/proportion.toml", "max_files = = 3\n")
    commit(repo, "[Chore] Config rota")
    r = run(repo)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "gate failure" in r.stderr


def test_an_unknown_config_key_is_exit_two(repo: Path) -> None:
    """A typo'd key that silently does nothing is a threshold nobody is enforcing."""
    write(repo, ".conduct/proportion.toml", "max_fils = 50\n")
    commit(repo, "[Chore] Config con una clave mal escrita")
    r = run(repo)
    assert r.returncode == 2
    assert "unknown key" in r.stderr


def test_no_resolvable_base_is_exit_two_not_a_clean_pass(tmp_path: Path) -> None:
    """When the input is missing the answer is never 'all good'."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    write(tmp_path, "a.py", "A = 1\n")
    commit(tmp_path, "[Init] Solo commit")
    r = run(tmp_path)
    assert r.returncode == 2, r.stdout
    assert "no usable base" in r.stderr


def test_an_explicit_base_is_honoured(repo: Path) -> None:
    spread(repo, 12)
    commit(repo, "[Feat] Doce modulos")
    assert run(repo, "--base", "HEAD~1").returncode == 1
    assert run(repo, "--base", "HEAD").returncode == 1     # falls back, does not pass blindly


# --- SARIF output ------------------------------------------------------------

def test_sarif_is_written_and_well_formed(repo: Path, tmp_path: Path) -> None:
    spread(repo, 12)
    commit(repo, "[Feat] Doce modulos")
    out = tmp_path / "out.sarif"
    r = run(repo, "--sarif", str(out))
    assert r.returncode == 1
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["version"] == "2.1.0"
    assert doc["runs"][0]["results"], "SARIF carries no results for a failing run"
    assert doc["runs"][0]["results"][0]["ruleId"] == "scope-spread"
    # Advisory by design: this gate reasons about intent it can only infer.
    assert doc["runs"][0]["results"][0]["level"] == "warning"


def test_a_declared_change_writes_an_empty_sarif(repo: Path, tmp_path: Path) -> None:
    spread(repo, 12)
    commit(repo, "[Feat] Doce modulos\n\nScope: el alta del paquete es la tarea.")
    out = tmp_path / "out.sarif"
    assert run(repo, "--sarif", str(out)).returncode == 0
    assert json.loads(out.read_text(encoding="utf-8"))["runs"][0]["results"] == []


def test_the_waived_findings_are_still_printed(repo: Path) -> None:
    """The valve waives the verdict, never the report. The mirror is not traded."""
    spread(repo, 12)
    commit(repo, "[Feat] Doce modulos\n\nScope: el alta del paquete es la tarea.")
    r = run(repo)
    assert r.returncode == 0
    assert "scope-spread" in r.stdout
    assert "waived" in r.stdout
