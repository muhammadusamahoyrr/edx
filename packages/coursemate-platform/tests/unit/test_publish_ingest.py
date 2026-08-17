"""`XBLOCK_PUBLISHED` must actually publish — the run-lifecycle flags.

**The defect these pin.** `ingest_published_block` called `send_leaves` with no
`run_id`, no `is_final` and no `topup`. Every row `write_chunks` writes is
INACTIVE, and only `topup=True` (activate these blocks) or `is_final=True`
(verify and swap the whole course) makes anything active. Passing neither meant a
publish wrote chunks nobody could ever retrieve, while the task logged success
and the `swapped=False` in the reply went unread. The content stayed invisible
until the nightly sweep found it missing and repaired it — up to a day, from the
one path that exists so an instructor's Publish reaches the tutor promptly.

**Why `is_final` is not the fix, and is tested against.** `swap()` opens with
`UPDATE chunks SET active=0 WHERE offering_id=? AND version<>?`, so finalising a
run holding only the published subtree would deactivate the whole rest of the
course. The bug served stale content; that "fix" would serve almost none. The
test below asserts `is_final` is never true here, because the failure it prevents
is silent and total.
"""

from __future__ import annotations

import sys
import types

import pytest

pytest.importorskip("django")

# --- the task layer's imports, stubbed rather than skipped -------------------
#
# `coursemate_platform/tasks/__init__.py` imports every task module, and
# `tasks/ingest.py` imports `celery` and `opaque_keys` at module scope. Neither
# is in `requirements-dev.txt`, so importing the task raises there — and
# `importorskip` is what that file explicitly argues against: *"a suite that
# quietly does not run is worse than one that fails."* Skipping would leave the
# publish lifecycle untested in exactly the environment meant to test it.
#
# **Installed unconditionally, and that is the point.** The first version of this
# file stubbed only celery and only when absent, so a machine that happened to
# have `opaque_keys` (it arrives transitively with some XBlock resolutions) ran
# against the real package while CI, which does not, failed at collection. The
# suite was green locally for a reason that did not hold anywhere else. Stubbing
# both, always, makes local and CI exercise the same thing — which is the only
# property that makes a green local run mean anything.
#
# `shared_task` is scheduling plumbing and nothing under test depends on it. The
# key stub IS faithful, so `offering_id = str(key.course_key)` — real derivation
# in the code under test — is still genuinely asserted rather than mocked away.

_celery = types.ModuleType("celery")


def _shared_task(*args, **kwargs):
    if args and callable(args[0]) and not kwargs:
        return args[0]                        # bare @shared_task

    def _decorate(fn):
        return fn                              # @shared_task(bind=True, ...)

    return _decorate


_celery.shared_task = _shared_task
sys.modules["celery"] = _celery


class _CourseKey:
    """`course-v1:ORG+COURSE+RUN`, which is what the code stringifies."""

    def __init__(self, org: str, course: str, run: str) -> None:
        self._value = f"course-v1:{org}+{course}+{run}"

    def __str__(self) -> str:
        return self._value

    def __eq__(self, other) -> bool:
        return str(self) == str(other)


class _UsageKey:
    """Parses `block-v1:ORG+COURSE+RUN+type@T+block@B` down to its course key.

    Real parsing, not a canned answer: the task derives `offering_id` from this,
    and a stub returning a constant would assert nothing about that derivation.
    """

    def __init__(self, course_key: _CourseKey) -> None:
        self.course_key = course_key

    @classmethod
    def from_string(cls, raw: str) -> _UsageKey:
        org, course, run = raw.split(":", 1)[1].split("+")[:3]
        return cls(_CourseKey(org, course, run))


_keys = types.ModuleType("opaque_keys.edx.keys")
_keys.UsageKey = _UsageKey
_keys.CourseKey = _CourseKey
_edx = types.ModuleType("opaque_keys.edx")
_edx.keys = _keys
_opaque = types.ModuleType("opaque_keys")
_opaque.edx = _edx
sys.modules["opaque_keys"] = _opaque
sys.modules["opaque_keys.edx"] = _edx
sys.modules["opaque_keys.edx.keys"] = _keys

from django.conf import settings as django_settings

COURSE = "course-v1:OpenedX+OEX101+2023"
SUBTREE = "block-v1:OpenedX+OEX101+2023+type@sequential+block@seq1"
ACTIVE = "cdadbc7f-143d-4585-929b-736366736f1b"


class _Leaf:
    """The subset of `LeafContent` this task touches."""

    def __init__(self, n):
        self.usage_key = f"block-v1:OpenedX+OEX101+2023+type@html+block@{n}"
        self.block_id = f"b{n}"
        self.block_type = "html"
        self.version = "v-block"
        self.display_name = f"Block {n}"
        self.text = f"content {n}"
        self.group_tokens = ()


class _Accepted:
    def __init__(self, n):
        self.blocks_written = n
        self.failed = []


@pytest.fixture
def wired(monkeypatch):
    """Stub the adapter and the service client; record every outbound call."""
    from coursemate_platform.client import endpoints
    from coursemate_platform.tasks import ingest as task_mod

    calls: dict[str, list] = {"send": [], "prune": [], "version": []}

    monkeypatch.setattr(django_settings, "COURSEMATE_INGEST_BATCH_SIZE", 2,
                        raising=False)
    monkeypatch.setattr(django_settings, "COURSEMATE_TENANT", "default", raising=False)

    def _install(*, leaves, active=ACTIVE):
        # A stub MODULE set on the package, not `monkeypatch.setattr` with a
        # string target: the string form imports the real `content_adapter`,
        # which imports `xmodule` — the Open edX runtime this suite exists
        # without. `from ..adapters import content_adapter` resolves the package
        # attribute first, so this substitutes cleanly and the real module is
        # never touched.
        import coursemate_platform.adapters as adapters_pkg

        stub = types.ModuleType("content_adapter_stub")
        stub.iter_leaves = lambda _key: iter(leaves)
        stub.get_course_meta = lambda _key: {"course_version": "cv1"}
        monkeypatch.setattr(adapters_pkg, "content_adapter", stub, raising=False)

        def _active_version(offering_id):
            calls["version"].append(offering_id)
            return active

        def _prune(*, offering_id, usage_keys):
            calls["prune"].append((offering_id, list(usage_keys)))
            return {"deleted_chunks": 0, "blocks": len(usage_keys)}

        def _send(**kw):
            calls["send"].append(kw)
            return _Accepted(len(kw["leaves"]))

        monkeypatch.setattr(endpoints, "active_version", _active_version)
        monkeypatch.setattr(endpoints, "prune_blocks", _prune)
        monkeypatch.setattr(endpoints, "send_leaves", _send)
        return calls

    _install.module = task_mod
    return _install


def _run(leaves, wired, **kw):
    from coursemate_platform.tasks.ingest import ingest_published_block

    calls = wired(leaves=leaves, **kw)
    # `bind=True` on the real decorator makes `self` the first argument; the stub
    # leaves the function undecorated, so it is passed explicitly.
    result = ingest_published_block(None, SUBTREE, "v-block", "trace-1")
    return calls, result


# --- the published subtree becomes active -----------------------------------


def test_a_publish_activates_the_blocks_it_wrote(wired):
    """`topup=True` is what turns an INACTIVE write into served content."""
    calls, result = _run([_Leaf(1), _Leaf(2)], wired)

    assert calls["send"], "nothing was sent to the service"
    for kw in calls["send"]:
        assert kw["topup"] is True, "without topup the rows stay inactive forever"
    assert result["blocks"] == 2


def test_is_final_is_never_used_on_the_publish_path(wired):
    """The guard against the catastrophic fix.

    `swap()` deactivates every row whose version differs, so finalising a run
    containing only this subtree would empty the rest of the course."""
    calls, _ = _run([_Leaf(1), _Leaf(2), _Leaf(3)], wired)

    assert calls["send"], "no calls to inspect"
    for kw in calls["send"]:
        assert kw["is_final"] is False


def test_the_active_version_is_targeted_not_a_new_one(wired):
    """A top-up must land inside the version the swap pointer already names —
    that is what `activate_usage_keys` matches on. A fresh run_id would write
    rows nothing points at, which is precisely the old bug."""
    calls, _ = _run([_Leaf(1)], wired)

    assert calls["version"] == [COURSE], "the active version was never read"
    for kw in calls["send"]:
        assert kw["run_id"] == ACTIVE
        assert kw["run_id"] != "trace-1", "run_id must not default to the trace id"


def test_unrelated_course_content_is_not_touched(wired):
    """Only the published subtree's own keys are pruned, and nothing else is
    named in any outbound call — so no unrelated chunk can be deactivated."""
    leaves = [_Leaf(1), _Leaf(2)]
    calls, _ = _run(leaves, wired)

    mine = {leaf.usage_key for leaf in leaves}
    for offering_id, keys in calls["prune"]:
        assert offering_id == COURSE
        assert set(keys) == mine, "pruned something outside the published subtree"
    sent = {leaf.usage_key for kw in calls["send"] for leaf in kw["leaves"]}
    assert sent == mine


def test_the_correct_usage_keys_are_passed_for_activation(wired):
    """The service activates exactly the keys in the batch, so the batch is the
    activation list. Batch size is 2 here, so this also pins that every window
    carries its own keys rather than only the first."""
    leaves = [_Leaf(n) for n in range(1, 6)]
    calls, _ = _run(leaves, wired)

    assert len(calls["send"]) == 3, "expected 5 leaves in batches of 2"
    sent = [leaf.usage_key for kw in calls["send"] for leaf in kw["leaves"]]
    assert sent == [leaf.usage_key for leaf in leaves]


def test_republishing_prunes_before_writing(wired):
    """`write_chunks` does not deduplicate. Without a prune, a block published
    twice is written twice under the active version, activated twice, and cited
    twice — the same reasoning the sweep already applies."""
    calls, _ = _run([_Leaf(1)], wired)

    assert len(calls["prune"]) == 1
    assert calls["prune"][0][1] == [_Leaf(1).usage_key]


# --- refusals ---------------------------------------------------------------


def test_a_course_with_no_active_version_is_skipped_not_bootstrapped(wired):
    """Topping up an unindexed course writes rows under a pointer that does not
    exist. Bootstrapping is a deliberate operator action — the same rule the
    sweep applies to itself."""
    calls, result = _run([_Leaf(1)], wired, active=None)

    assert calls["send"] == [], "wrote chunks into a course with no active version"
    assert calls["prune"] == [], "pruned content without being able to replace it"
    assert result["skipped"] == "no active version"


def test_no_supported_leaves_sends_nothing(wired):
    """Unchanged behaviour: a container with nothing ingestible is not an error,
    and must not prune or write."""
    calls, result = _run([], wired)

    assert result == {"blocks": 0}
    assert calls["send"] == [] and calls["prune"] == []


# --- the other lifecycles must not move -------------------------------------


def test_the_sweep_still_uses_its_own_lifecycle():
    """`reconcile` keeps `run_id=<active>`, `is_final=False`, `topup=True`. The
    publish path now matches it; this asserts the source of that pattern did not
    drift while being reused."""
    import inspect

    from coursemate_platform.tasks import reconcile

    src = inspect.getsource(reconcile)
    assert "topup=True" in src
    assert "is_final=False" in src
    assert "run_id=version" in src


def test_bootstrap_still_owns_the_swap():
    """Only a full run may flip the pointer. If this ever stops being true, the
    publish path's `is_final=False` stops being a safety property."""
    import inspect

    from coursemate_platform.tasks import bootstrap

    src = inspect.getsource(bootstrap)
    assert "is_final=" in src, "bootstrap no longer controls finalisation"
    assert "is_final=True" in src or "is_final=(" in src
