"""The OpenAPI drift check — the checker, not just the spec.

`make openapi` has always existed, and the spec still went stale: Phase 2 added
`content_sha256` and `clo_confidence` to the contracts, nobody re-ran it, and the
committed document described an API that no longer existed. Nothing said so,
because generating the spec was a step someone had to remember.

So the interesting thing to test is not "the spec is current" — `make check` runs
that on every build now. It is that **the checker actually fails when the spec is
stale**. A drift check that passes unconditionally is worse than none: it is a
green light that means nothing, and this repo has shipped two of those.

The drift check runs `docs/openapi.json` against the live route table, so these
tests reach outside the package into `tools/`. That is why they load the script by
path rather than importing it — `tools/` is deliberately not on `sys.path` (it is
operator tooling, not shipped code), and putting it there to make a test tidy
would ship it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tools" / "ops" / "dump_openapi.py"


@pytest.fixture(scope="module")
def dump():
    """The generator script, loaded from its real path."""
    spec = importlib.util.spec_from_file_location("cm_dump_openapi", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


@pytest.fixture
def spec_file(dump, monkeypatch, tmp_path):
    """Point the checker at a scratch copy, so no test can rewrite the real one.

    A test that regenerates `docs/openapi.json` as a side effect would make the
    check it is testing pass by construction — and would quietly commit a spec
    nobody reviewed.
    """
    scratch = tmp_path / "openapi.json"
    scratch.write_text(dump.render(dump.current_spec()), encoding="utf-8")
    monkeypatch.setattr(dump, "OUT", scratch)
    return scratch


# --- the positive case -----------------------------------------------------


def test_the_committed_spec_is_current(dump):
    """Against the REAL `docs/openapi.json`. If this fails, run `make openapi`."""
    assert dump.OUT.exists(), "docs/openapi.json is missing"
    assert dump.OUT.read_text(encoding="utf-8") == dump.render(dump.current_spec()), (
        "docs/openapi.json is stale — run `make openapi`"
    )


def test_check_passes_on_a_matching_spec(dump, spec_file):
    assert dump.check() == 0


def test_check_is_not_confused_by_key_order(dump, spec_file):
    """FastAPI does not promise stable key order, so the checker sorts before
    comparing. Without that it would fail on a reordering that changed nothing,
    and a check that cries wolf gets switched off."""
    payload = json.loads(spec_file.read_text(encoding="utf-8"))
    shuffled = dict(reversed(list(payload.items())))
    spec_file.write_text(json.dumps(shuffled, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")

    assert dump.check() == 0


# --- the negative case, which is the point ---------------------------------


def test_check_fails_when_a_field_is_missing(dump, spec_file, capsys):
    """The exact Phase 2 failure, reproduced: a contract gained a field and the
    spec did not."""
    payload = json.loads(spec_file.read_text(encoding="utf-8"))
    del payload["components"]["schemas"]["QuestionRecord"]["properties"]["clo_confidence"]
    spec_file.write_text(dump.render(payload), encoding="utf-8")

    assert dump.check() == 1
    assert "STALE" in capsys.readouterr().err


def test_check_fails_when_a_route_is_missing(dump, spec_file, capsys):
    """The Phase 4A failure shape: a new endpoint that the spec does not
    describe."""
    payload = json.loads(spec_file.read_text(encoding="utf-8"))
    del payload["paths"]["/coursemate/api/examprep/study-plan"]
    spec_file.write_text(dump.render(payload), encoding="utf-8")

    assert dump.check() == 1


def test_check_fails_on_a_harmless_looking_edit(dump, spec_file):
    """Any difference, not just a structural one. A spec edited by hand is a
    second source of truth however small the edit."""
    payload = json.loads(spec_file.read_text(encoding="utf-8"))
    payload["info"]["version"] = "9.9.9-hand-edited"
    spec_file.write_text(dump.render(payload), encoding="utf-8")

    assert dump.check() == 1


def test_a_missing_spec_file_fails_rather_than_passing_vacuously(
    dump, spec_file, capsys
):
    """"There is nothing to compare" must not read as "nothing has drifted"."""
    spec_file.unlink()

    assert dump.check() == 1
    assert "MISSING" in capsys.readouterr().err


def test_the_failure_says_how_to_fix_it(dump, spec_file, capsys):
    """A check whose message leaves the reader guessing gets disabled the first
    time it fires on someone else's change."""
    payload = json.loads(spec_file.read_text(encoding="utf-8"))
    payload["info"]["title"] = "not CourseMate"
    spec_file.write_text(dump.render(payload), encoding="utf-8")
    dump.check()

    err = capsys.readouterr().err
    assert "make openapi" in err
    assert "not CourseMate" in err, "the diff should show what actually differs"


def test_the_diff_is_truncated(dump, spec_file, capsys):
    """A contract rename can move thousands of lines, and a wall of diff buries
    the one-line answer to "what changed"."""
    spec_file.write_text(dump.render({"openapi": "3.1.0", "paths": {}}), encoding="utf-8")
    dump.check()

    err = capsys.readouterr().err
    assert "more diff lines" in err
    assert len(err.splitlines()) < 100


# --- the check does not have side effects ---------------------------------


def test_check_never_writes_the_file(dump, spec_file):
    """It reports drift; it does not silently fix it. A checker that rewrote the
    spec would turn a build failure into an unreviewed commit."""
    payload = json.loads(spec_file.read_text(encoding="utf-8"))
    payload["info"]["version"] = "0.0.0-drifted"
    stale = dump.render(payload)
    spec_file.write_text(stale, encoding="utf-8")

    dump.check()
    assert spec_file.read_text(encoding="utf-8") == stale


def test_the_writer_and_the_checker_agree(dump, tmp_path, monkeypatch):
    """One serialiser, so they cannot disagree about formatting and report a
    difference that is only whitespace."""
    scratch = tmp_path / "written.json"
    monkeypatch.setattr(dump, "OUT", scratch)

    assert dump.main([]) == 0        # write
    assert dump.check() == 0         # check what was written


def test_generating_twice_is_stable(dump):
    """A spec that differs from itself would make the check fail at random —
    which is how a real check gets removed for being flaky."""
    assert dump.render(dump.current_spec()) == dump.render(dump.current_spec())


# --- what the spec must contain -------------------------------------------


def test_the_study_plan_route_is_described(dump):
    spec = dump.current_spec()
    route = spec["paths"]["/coursemate/api/examprep/study-plan"]["post"]

    body = route["requestBody"]["content"]["application/json"]["schema"]
    assert body["$ref"].endswith("/StudyPlanRequest")

    ok = route["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok["$ref"].endswith("/StudyPlan")


def test_the_study_plan_request_publishes_its_budget_bounds(dump):
    """An integrator reading the spec should learn the budget is bounded from the
    spec, not from a 422."""
    props = dump.current_spec()["components"]["schemas"]["StudyPlanRequest"]["properties"]

    assert props["marks_budget"]["minimum"] == 1
    assert props["marks_budget"]["maximum"] == 500
    assert set(props) == {"marks_budget", "mastery"}, (
        "no identity field may appear in a request body"
    )


def test_the_phase_2_fields_are_described(dump):
    """The two that went missing and started this."""
    schemas = dump.current_spec()["components"]["schemas"]

    assert "content_sha256" in schemas["ExamPrepPack"]["properties"]
    assert "clo_confidence" in schemas["QuestionRecord"]["properties"]


def test_every_route_on_the_app_is_in_the_spec(dump):
    """Not a tautology: a route excluded with `include_in_schema=False` would
    vanish from the spec while still being callable, and an undocumented live
    endpoint is exactly what §3.4 keeps the credential classes apart to avoid."""
    from coursemate_service.main import app

    described = set(dump.current_spec()["paths"])
    live = {
        r.path for r in app.routes
        if getattr(r, "methods", None) and getattr(r, "include_in_schema", True)
    }
    assert live - described == set()
