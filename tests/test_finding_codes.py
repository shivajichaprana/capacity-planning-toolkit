"""A cross-artifact audit of the finding vocabulary.

Findings are the toolkit's only channel for saying "this number is weaker than
it looks", and the vocabulary they are drawn from lives in five places that can
each be edited alone: the modules that emit the codes, the registries that
describe them, the README table a reader resolves them against, and the remedy
map the review renderer closes a report with. Nothing fails when those drift
apart. A code emitted but not registered renders as a bare identifier; a code
registered but no longer emitted documents a check that has quietly gone away;
a remedy keyed on a code nobody emits is dead text nobody will notice. All
three are failures here.

The namespace check is the one that matters most over time. The prefixes are
reserved per concern precisely so that a later module cannot take a range that
is already in use, and a reservation that is only written in a docstring is a
reservation that will eventually be ignored.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from models.saturation import FINDING_CODES as SATURATION_CODES
from recommend.headroom import FINDING_CODES as HEADROOM_CODES
from recommend.whatif import FINDING_CODES as WHATIF_CODES
from reports.review import REMEDIES

ROOT = Path(__file__).resolve().parents[1]

#: Which prefixes each module is allowed to emit. The reservations are stated
#: in the package docstrings; this is what makes them enforceable.
NAMESPACES = {
    "models/seasonality.py": ("F1",),
    "models/forecast.py": ("F2", "F3", "F4"),
    "models/saturation.py": ("F5", "F6"),
    "recommend/headroom.py": ("F7",),
    "recommend/whatif.py": ("F8",),
}

CODE_LITERAL = re.compile(r'"(F\d{3})"')
CONSTRUCTED = re.compile(r'Finding\(\s*"(F\d{3})",\s*"(error|warning|note)"', re.S)
README_ROW = re.compile(r"^\| (F\d{3}) \| (error|warning|note) \|", re.M)


def source(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def emitted(name: str) -> set[str]:
    """Every code the module names, however it reaches a result.

    Codes travel two ways: constructed inline as a ``Finding``, or passed to a
    refusal helper that builds one. Matching only the first would miss every
    error code in the forecast module.
    """
    return set(CODE_LITERAL.findall(source(name)))


def constructed(name: str) -> set[tuple[str, str]]:
    return set(CONSTRUCTED.findall(source(name)))


@pytest.mark.parametrize(("name", "prefixes"), sorted(NAMESPACES.items()))
def test_each_module_stays_inside_its_reserved_namespace(
    name: str, prefixes: tuple[str, ...]
) -> None:
    for code in sorted(emitted(name)):
        assert code.startswith(prefixes), f"{name} emits {code}, outside {prefixes}"


def test_no_two_modules_claim_the_same_code() -> None:
    seen: dict[str, str] = {}
    for name in NAMESPACES:
        for code in emitted(name):
            assert code not in seen, f"{code} is emitted by both {seen.get(code)} and {name}"
            seen[code] = name
    assert len(seen) >= 95


# ---------------------------------------------------------------------------
# The model codes against the README table
# ---------------------------------------------------------------------------


def test_the_readme_table_and_the_model_codes_agree_in_both_directions() -> None:
    documented = {code for code, _ in README_ROW.findall(source("README.md"))}
    produced = emitted("models/seasonality.py") | emitted("models/forecast.py")
    assert documented - produced == set(), "documented but never emitted"
    assert produced - documented == set(), "emitted but not documented"


def test_the_readme_table_records_the_severity_each_code_is_emitted_with() -> None:
    documented = dict(README_ROW.findall(source("README.md")))
    pairs = constructed("models/seasonality.py") | constructed("models/forecast.py")
    for code, severity in sorted(pairs):
        assert documented[code] == severity, f"{code} is {severity}, documented as " \
                                             f"{documented[code]}"


def test_the_readme_table_has_no_duplicate_rows() -> None:
    rows = README_ROW.findall(source("README.md"))
    assert len(rows) == len({code for code, _ in rows})


# ---------------------------------------------------------------------------
# The registries against the modules that own them
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "registry"),
    (
        ("models/saturation.py", SATURATION_CODES),
        ("recommend/headroom.py", HEADROOM_CODES),
        ("recommend/whatif.py", WHATIF_CODES),
    ),
)
def test_a_registry_describes_exactly_what_its_module_emits(
    name: str, registry: dict[str, str]
) -> None:
    produced = emitted(name)
    assert set(registry) - produced == set(), f"{name}: registered but never emitted"
    assert produced - set(registry) == set(), f"{name}: emitted but not registered"


@pytest.mark.parametrize(
    "registry", (SATURATION_CODES, HEADROOM_CODES, WHATIF_CODES)
)
def test_every_registered_code_has_a_description_worth_reading(
    registry: dict[str, str]
) -> None:
    for code, description in registry.items():
        assert re.fullmatch(r"F\d{3}", code)
        assert isinstance(description, str) and len(description) > 15
        assert not description.endswith(".")


def test_the_registries_do_not_overlap() -> None:
    assert not set(SATURATION_CODES) & set(HEADROOM_CODES)
    assert not set(SATURATION_CODES) & set(WHATIF_CODES)
    assert not set(HEADROOM_CODES) & set(WHATIF_CODES)


# ---------------------------------------------------------------------------
# The remedy map the renderer closes a review with
# ---------------------------------------------------------------------------


def test_every_remedy_is_keyed_on_a_code_something_actually_emits() -> None:
    known: set[str] = set()
    for name in NAMESPACES:
        known |= emitted(name)
    unknown = sorted(set(REMEDIES) - known)
    assert unknown == [], f"remedies for codes nobody emits: {unknown}"


def test_every_remedy_reads_as_an_instruction() -> None:
    assert REMEDIES
    for code, remedy in REMEDIES.items():
        assert re.fullmatch(r"F\d{3}", code)
        assert len(remedy) > 20
