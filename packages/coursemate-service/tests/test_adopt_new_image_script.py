"""The adoption script must not report success on a partial adoption.

**This exists because it happened.** On 2026-08-16 an earlier command had already
moved `lms` onto the new image, so the script's preflight — which inspected
`tutor_local-lms-1` and nothing else — saw a match, printed "containers are
ALREADY on the latest image; nothing to do" and exited 0. `lms-worker` stayed on
the previous image and would have run stale code indefinitely. The per-container
verification that would have caught it sat in step 3, unreachable behind that
early exit.

`coursemate-beat` was a second hole in the same wall: it is built from the
openedx image (`deploy/tutor-plugin/coursemate.yml`) and was in neither the
recreate list nor the verification loop.

A source-level scan rather than an execution test, and the reason is worth
stating: running this script needs a Docker daemon, a built image and a live
Tutor stack, so an execution test would be skipped in CI and therefore prove
nothing — the failure mode this repository has already been bitten by twice
(`pypdf`, `make test-js`). What is asserted here is what actually went wrong:
which containers the script names, and whether its early exit can be reached
while any of them is behind.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tools" / "ops" / "adopt_new_image.sh"
PLUGIN = ROOT / "deploy" / "tutor-plugin" / "coursemate.yml"

#: Every container built from the openedx image.
EXPECTED = {"lms", "cms", "lms-worker", "cms-worker", "coursemate-beat"}


@pytest.fixture(scope="module")
def source() -> str:
    assert SCRIPT.exists(), f"{SCRIPT} is missing"
    return SCRIPT.read_text(encoding="utf-8")


def _services(source: str) -> set[str]:
    m = re.search(r'^SERVICES="([^"]+)"', source, re.MULTILINE)
    assert m, "SERVICES is not declared as a quoted string; the scan has rotted"
    return set(m.group(1).split())


def test_the_scan_finds_the_service_list(source):
    """A regex that matched nothing would make every test below vacuous."""
    assert _services(source), "no services parsed"


def test_every_openedx_container_is_adopted(source):
    """`coursemate-beat` was absent, so it was never recreated and never checked."""
    missing = EXPECTED - _services(source)
    assert not missing, f"these run the openedx image but are not adopted: {sorted(missing)}"


def test_no_container_is_adopted_that_does_not_run_the_image(source):
    """The reverse error: recreating something built from a different image."""
    unexpected = _services(source) - EXPECTED
    assert not unexpected, f"not built from the openedx image: {sorted(unexpected)}"


def test_coursemate_beat_really_does_use_the_openedx_image():
    """Pins the premise of the test above to the plugin, so the two cannot drift
    — if beat is ever moved to its own image, this fails rather than silently
    keeping it in the adopted set."""
    text = PLUGIN.read_text(encoding="utf-8")
    block = text.split("coursemate-beat:", 1)[1][:400]
    assert "overhangio/openedx" in block, "coursemate-beat no longer uses the openedx image"


def test_the_preflight_checks_every_container_not_just_lms(source):
    """**The defect itself.**

    The early exit must be governed by all of SERVICES. A preflight that inspects
    one container can short-circuit while another is behind, which is precisely
    what shipped a stale `lms-worker`.
    """
    assert "all_on_target" in source, (
        "the preflight no longer uses an all-container check; a single-container "
        "check can exit 0 while another container is stale"
    )
    # The early exit must be guarded by that helper.
    exit_guard = re.search(r"if\s+all_on_target[^\n]*\n(?:[^\n]*\n){0,3}?[^\n]*exit 0",
                           source)
    assert exit_guard, "the `exit 0` early return is not guarded by all_on_target"


def test_the_preflight_does_not_short_circuit_on_lms_alone(source):
    """Guards against a regression back to the original shape."""
    bad = re.search(r'OLD=\$\(docker inspect tutor_local-lms-1[^\n]*\)\s*\n'
                    r'(?:[^\n]*\n){0,4}?\s*if \[ "\$OLD" = "\$NEW" \]', source)
    assert not bad, "the preflight compares only tutor_local-lms-1 again"


def test_adoption_fails_when_a_container_did_not_move(source):
    """Step 3 must still be fatal. Reporting success with one container behind is
    how a worker runs last week's code unnoticed."""
    assert 'FATAL: not all containers moved.' in source
    assert re.search(r'\[ "\$FAILED" -eq 0 \] \|\| \{[^}]*exit 1', source), (
        "the per-container verification no longer exits non-zero on failure"
    )


def test_a_missing_container_is_a_failure_not_a_pass(source):
    """An absent container used to make `docker inspect` fail under `set -e`, or
    compare empty-to-empty. Either way "not running" must not read as "moved"."""
    assert "NOT RUNNING" in source, "an absent expected container is not reported"


def test_the_package_preflight_is_still_refused_when_absent(source):
    """Unchanged behaviour: an image without `coursemate_platform` must not be
    adopted, whatever else this script learns to check."""
    assert "Refusing to recreate" in source


def test_the_post_checks_are_still_run(source):
    """`check_install.sh` and `check_tasks.sh` are what prove the package
    survived the recreate and the tasks registered."""
    assert "check_install.sh" in source
    assert "check_tasks.sh" in source
