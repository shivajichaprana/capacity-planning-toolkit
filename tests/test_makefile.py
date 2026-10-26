"""The Makefile and the pipeline must run the same commands.

A Makefile that merely resembles the pipeline is worse than no Makefile: it
invites you to trust a local run that is checking something slightly different,
and the difference is invisible until a push fails. So the contract here is
equality of command strings, checked in both directions.

Forward
    Every tool invocation in a gate target appears verbatim in the pipeline. A
    target that drops a flag, points at fewer packages, or runs a different
    tool fails here.

Backward
    Every tool invocation in the pipeline appears verbatim in some target. A
    gate added to the pipeline with no target and no stated exception fails
    here, which is the direction that actually rots -- the pipeline is where a
    new check gets added, and nothing else would notice the Makefile falling
    behind.

The gate set is not written down twice either: it is read from the
prerequisites of ``check``, so adding a gate target to ``check`` is what brings
it under the contract.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = ROOT / "Makefile"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

#: What ``$(PYTHON)`` resolves to in the pipeline, where the interpreter is on
#: PATH as ``python``. Expanding it is the only normalisation applied to a
#: recipe before comparison; everything else is compared as written.
PYTHON = "python"

#: Required jobs whose gate is not a tool invocation, so command equality
#: cannot reach it. Each entry says what checks the job's parity instead.
PIPELINE_ONLY_JOBS: dict[str, str] = {
    "analysis-imports": (
        "its gate is a list of modules rather than a command, and the two "
        "lists are compared as sets by "
        "test_the_import_gate_checks_the_same_modules_as_the_pipeline"
    ),
}


def _commands(text: str) -> list[str]:
    """Shell command lines worth comparing, in order of appearance.

    A line qualifies when it invokes one of the project's tools directly. Echo
    lines, shell options and the bodies of inline scripts are not part of the
    contract: what matters is that the same tool is run against the same
    arguments, not that two files narrate it identically.
    """
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        # A workflow step writes a single command as `run: <command>` and a
        # script as `run: |` followed by bare lines. Both forms have to be
        # read, and forgetting the first is a checker that sees a sixth of the
        # pipeline and reports agreement with it.
        if line.startswith("run:"):
            line = line[len("run:"):].strip()
        if line in ("|", ">", "|-", ">-"):
            continue
        if line.startswith(("@", "-", "+")):
            line = line[1:].strip()
        if line.startswith(f"{PYTHON} -m ") or "py_compile" in line:
            out.append(line)
    return out


def _recipes() -> dict[str, list[str]]:
    """Each target's recipe, continuation lines joined, ``$(PYTHON)`` expanded."""
    recipes: dict[str, list[str]] = {}
    target: str | None = None
    pending: str | None = None
    for raw in MAKEFILE.read_text(encoding="utf-8").splitlines():
        if raw.startswith("\t"):
            if target is None:
                continue
            body = raw[1:]
            joined = body[:-1].rstrip() if body.endswith("\\") else body
            pending = joined if pending is None else f"{pending} {joined.strip()}"
            if not body.endswith("\\"):
                recipes.setdefault(target, []).append(
                    pending.replace("$(PYTHON)", PYTHON)
                )
                pending = None
            continue
        pending = None
        match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*)\s*:(?!=)", raw)
        target = match.group(1) if match else None
        if match:
            recipes.setdefault(target or "", [])
    return recipes


def _gate_targets() -> list[str]:
    """The targets ``check`` depends on, which is the gate set by definition."""
    text = MAKEFILE.read_text(encoding="utf-8")
    match = re.search(r"^check:([^\n]*)$", text, re.MULTILINE)
    assert match is not None, "the Makefile has no `check` target to read gates from"
    gates = match.group(1).split()
    assert gates, "`check` has no prerequisites, so no target is under the contract"
    return gates


def _required_jobs(workflow: str) -> list[str]:
    """Jobs the aggregate gate depends on, read from its ``needs`` list."""
    block = re.search(r"\n  ci:\n(.*?)(?=\n  [a-z]|\Z)", workflow, re.DOTALL)
    assert block is not None, "the pipeline has no aggregate `ci` job"
    needs = re.search(r"needs:\n((?:\s*-\s*[^\n]+\n)+)", block.group(1))
    assert needs is not None, "the aggregate job declares no `needs`"
    return [
        line.strip().lstrip("-").strip()
        for line in needs.group(1).splitlines()
        if line.strip()
    ]


def _job_body(workflow: str, job: str) -> str:
    """The text of one job definition."""
    block = re.search(
        rf"\n  {re.escape(job)}:\n(.*?)(?=\n  [a-z]|\Z)", workflow, re.DOTALL
    )
    assert block is not None, f"the pipeline has no job named {job}"
    return block.group(1)


def _module_list(text: str) -> set[str]:
    """Dotted module names inside the first bracketed list in ``text``."""
    return set(re.findall(r'"([a-z_]+\.[a-z_]+)"', text))


# ---------------------------------------------------------------------------
# The parity contract
# ---------------------------------------------------------------------------


def test_every_gate_command_appears_in_the_pipeline() -> None:
    pipeline = _commands(WORKFLOW.read_text(encoding="utf-8"))
    recipes = _recipes()
    for target in _gate_targets():
        assert target in recipes, f"`check` names {target}, which has no recipe"
        for command in _commands("\n".join(recipes[target])):
            assert command in pipeline, (
                f"the {target} target runs `{command}`, which the pipeline does "
                "not: a local pass would not mean what a green run means"
            )


def test_every_pipeline_command_appears_in_a_target() -> None:
    available = _commands(
        "\n".join(
            command for commands in _recipes().values() for command in commands
        )
    )
    pipeline = _commands(WORKFLOW.read_text(encoding="utf-8"))
    assert pipeline, "no pipeline command was read at all, so this proves nothing"
    for command in pipeline:
        assert command in available, (
            f"the pipeline runs `{command}` and no target does: there is no way "
            "to run that check before pushing"
        )


def test_every_required_job_has_a_local_equivalent() -> None:
    """Command equality is per command; this is per job.

    A new job added to the pipeline brings commands that the backward check
    would catch -- but only if it runs a tool directly. A job whose whole gate
    is an inline script would slip past it, which is how a required check ends
    up with no way to run it locally. So every job the aggregate gate depends
    on must either share a command with a gate target or say why it cannot.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    recipes = _recipes()
    gate_commands = {
        command
        for target in _gate_targets()
        for command in _commands("\n".join(recipes[target]))
    }
    required = _required_jobs(workflow)
    assert required, "the aggregate gate depends on nothing"
    for job in required:
        if job in PIPELINE_ONLY_JOBS:
            continue
        shared = gate_commands & set(_commands(_job_body(workflow, job)))
        assert shared, (
            f"the required job {job} shares no command with any gate target, so "
            "there is no way to run it before pushing. Add a target, or record "
            "the job in PIPELINE_ONLY_JOBS with what checks its parity instead."
        )


def test_each_excused_job_is_still_a_required_job() -> None:
    """An excuse for a job that no longer exists is stale."""
    required = _required_jobs(WORKFLOW.read_text(encoding="utf-8"))
    for job in PIPELINE_ONLY_JOBS:
        assert job in required, (
            f"PIPELINE_ONLY_JOBS excuses {job}, which the aggregate gate no "
            "longer requires: delete the entry rather than leaving it to "
            "excuse nothing"
        )


def test_the_import_gate_checks_the_same_modules_as_the_pipeline() -> None:
    """A set comparison, both ways.

    The import gate is the one place the two files hold a list rather than a
    command, so equality of the command string does not reach it. Asserting
    that each module merely *appears* in both files would pass while one of
    them silently stopped checking a module, so the lists are compared as sets.
    """
    recipe = "\n".join(_recipes()["analysis-imports"])
    workflow = WORKFLOW.read_text(encoding="utf-8")
    block = re.search(r"modules = \[(.*?)\]", workflow, re.DOTALL)
    assert block is not None, "the pipeline's import gate has no module list"

    in_makefile = _module_list(recipe)
    in_pipeline = _module_list(block.group(1))
    assert in_makefile, "the Makefile's import gate names no modules"
    assert in_makefile == in_pipeline, (
        "the import gate checks different modules in the two files: only in the "
        f"Makefile {sorted(in_makefile - in_pipeline)}, only in the pipeline "
        f"{sorted(in_pipeline - in_makefile)}"
    )


def test_the_import_gate_covers_every_analysis_module() -> None:
    """And the list is not merely consistent -- it is complete."""
    expected = {
        f"{package.name}.{module.stem}"
        for package in (
            ROOT / "ingest",
            ROOT / "models",
            ROOT / "recommend",
            ROOT / "reports",
        )
        for module in package.glob("*.py")
        if module.stem != "__init__"
    }
    covered = _module_list("\n".join(_recipes()["analysis-imports"]))
    assert expected - covered == set(), (
        f"these modules are never imported by the gate: {sorted(expected - covered)}"
    )


# ---------------------------------------------------------------------------
# Properties that make a target trustworthy at all
# ---------------------------------------------------------------------------


def test_the_shell_fails_on_an_error_and_inside_a_pipe() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    assert re.search(r"^SHELL := bash$", text, re.MULTILINE), "no shell is declared"
    flags = re.search(r"^\.SHELLFLAGS := ([^\n]+)$", text, re.MULTILINE)
    assert flags is not None, "the declared shell runs with default flags"
    assert "-o pipefail" in flags.group(1), (
        "the recipe shell does not fail inside a pipe, so `a | b` reports b's "
        "status and a silent failure in a reads as success"
    )
    # Short options cluster: `-eu` is `-e -u`, and a check for the string "-u"
    # does not see it. Split the clusters before asking.
    single: set[str] = set()
    for token in flags.group(1).split():
        if token.startswith("-") and not token.startswith("--") and token != "-o":
            single.update(token[1:])
    for flag, what in (("e", "stop at the first failing command"),
                       ("u", "refuse an unset variable")):
        assert flag in single, f"the recipe shell does not {what} (-{flag})"
    assert ".DELETE_ON_ERROR:" in text, (
        "without .DELETE_ON_ERROR a failed recipe leaves a half-written target "
        "that the next run treats as up to date"
    )


def test_no_recipe_line_can_fail_without_failing_the_target() -> None:
    for target, commands in _recipes().items():
        for command in commands:
            assert "|| true" not in command, (
                f"the {target} target swallows a failure: `{command}`"
            )
    # The prefix sits at the start of a LOGICAL line, so continuation lines --
    # which routinely begin with a long option -- must be joined first, or
    # every multi-line invocation reads as an ignored failure.
    for target, commands in _recipes().items():
        for command in commands:
            assert not command.lstrip().startswith("-") or command.lstrip().startswith("--"), (
                f"the {target} target ignores the exit status of `{command}`"
            )


def test_every_phony_target_is_documented_and_every_document_is_a_target() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    declaration = re.search(
        r"^\.PHONY:((?:[^\n]*\\\n)*[^\n]*)$", text, re.MULTILINE
    )
    assert declaration is not None, "the Makefile declares no .PHONY targets"
    phony = set(
        " ".join(
            line.rstrip("\\").strip() for line in declaration.group(1).splitlines()
        ).split()
    )
    documented = set(re.findall(r"^## ([a-z-]+):", text, re.MULTILINE))
    assert phony, "no target is declared phony, so a same-named file would shadow it"
    assert phony == documented, (
        f"undocumented targets {sorted(phony - documented)}; documented "
        f"non-targets {sorted(documented - phony)}"
    )
    recipes = _recipes()
    for target in phony:
        assert target in recipes, f"{target} is declared phony but has no rule"
