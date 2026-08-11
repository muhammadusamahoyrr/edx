"""The CourseMate tutor XBlock — design §3.1, §3.4 rule 3.

**This block is not an application, and it is not a proxy either.** It does two
things, both of which return in milliseconds:

    json_handler("mint")    -> a short-lived JWT for the browser
    json_handler("persist") -> writes a completed turn to Scope.user_state

The browser then streams the answer from the CourseMate service directly, on a
same-origin path routed at the ingress. No LMS worker is held for the duration of
a generation.

Why not proxy the stream, which is the obvious design? Because a streaming relay
holds a gunicorn worker for the whole answer — five to fifteen seconds — and the
LMS worker pool is exhausted by *occupancy*, not by computation. Two hundred
students streaming concurrently would be two hundred occupied workers, which is
precisely the incident the topology in §3.4 exists to prevent. "The XBlock holds
no work" was true of CPU and false of connections, and only the second is what the
pool counts.

Chat history lives here, in Scope.user_state, and nowhere else (§3.1). The service
is stateless: it receives a rolling window with each request and forgets it. That
is what keeps "each student's chat is kept privately by the platform" literally
true, and keeps every byte of it inside user-retirement's reach.
"""

from __future__ import annotations

import logging

from django.conf import settings
from web_fragments.fragment import Fragment
from xblock.core import XBlock
from xblock.fields import Boolean, List, Scope, String

try:  # pragma: no cover - import shape differs across releases
    from xblock.utils.resources import ResourceLoader
except ImportError:  # pragma: no cover
    from xblockutils.resources import ResourceLoader

from coursemate_contracts.chat import Mode

from ..client.jwt import mint_student_token
from .citations import clean_citations
from .identity import roles_of

log = logging.getLogger(__name__)
loader = ResourceLoader(__name__)

#: Rolling window sent to the service. Bounded on purpose: the payload costs a few
#: KB, and the alternative — the service keeping its own conversation store —
#: would duplicate PII into a system that platform retirement does not reach.
HISTORY_WINDOW_TURNS = 10

#: Mastery entries carried per request. Matches `MasterySnapshot`'s `max_length`,
#: which is the constraint that actually rejects an over-long payload — this is
#: the trim that keeps a valid request from becoming an invalid one. A course with
#: more than 64 learning outcomes is outside the MVP, and the overflow is reported
#: rather than dropped silently.
MASTERY_WINDOW_CLOS = 192



@XBlock.wants("user")
class CourseMateTutorXBlock(XBlock):
    """An AI tutor, grounded in this course, inside the lesson."""

    display_name = String(
        display_name="Display name",
        default="AI Tutor",
        scope=Scope.settings,
    )

    #: Instructor configuration. NOTHING SECRET LIVES HERE. There is no mechanism
    #: that excludes a Scope.settings field from OLX export, so the only safe
    #: place for a credential is somewhere the course package cannot reach —
    #: which for us is the service, since this block makes no model calls at all
    #: (design §10.4, corrected in v7).
    enabled = Boolean(display_name="Enabled", default=True, scope=Scope.settings)
    #: Feature B's tab. Per-block rather than global: an instructor who wants the
    #: tutor in every unit does not necessarily want a revision planner in every
    #: unit, and the service-side `agent_enabled` flag is an operator control, not
    #: an instructor one. Both must be on for the agent path to run.
    exam_prep_enabled = Boolean(
        display_name="Show the exam-prep tab", default=False, scope=Scope.settings
    )
    mode = String(
        display_name="Default mode",
        default=Mode.DIRECT.value,
        values=[Mode.DIRECT.value, Mode.SOCRATIC.value],
        scope=Scope.settings,
    )

    #: Private per-student conversation. Scope.user_state is per-student and
    #: per-block, and the platform owns it — so deleting a student's data through
    #: the platform deletes this without CourseMate doing anything.
    history = List(default=[], scope=Scope.user_state)

    def _user(self):
        service = self.runtime.service(self, "user")
        return service.get_current_user() if service else None

    def _course_id(self) -> str:
        return str(getattr(self.runtime, "course_id", "") or "")

    def _offering_id(self) -> str:
        """The offering is the real isolation unit (§6.5): CS-101 Fall 2026 holds
        a different exam-prep pack and a different cohort from the same course run
        a year later. In the MVP a course maps to one offering."""
        return self._course_id()

    # --- views ---------------------------------------------------------------

    def student_view(self, context=None) -> Fragment:
        """Render the chat UI, seeded with this student's history."""
        fragment = Fragment(
            loader.render_django_template(
                "static/html/student_view.html",
                {
                    "display_name": self.display_name,
                    "enabled": self.enabled,
                    "exam_prep_enabled": self.exam_prep_enabled,
                },
            )
        )
        fragment.add_css(loader.load_unicode("static/css/tutor.css"))
        fragment.add_javascript(loader.load_unicode("static/js/src/tutor.js"))
        fragment.initialize_js(
            "CourseMateTutor",
            {
                "history": self.history[-HISTORY_WINDOW_TURNS:],
                "mode": self.mode,
                "enabled": self.enabled,
                "exam_prep_enabled": self.exam_prep_enabled,
                # The memory layer, seeded for the browser to carry (§3.1). Same
                # courier as `history`, and for the same reason: the service must
                # hold no per-student state, so what it needs has to arrive with
                # the request. Trimmed here rather than service-side, because the
                # payload cost is paid on this side of the wire.
                "mastery": self._mastery_snapshot(),
            },
        )
        return fragment

    def _mastery_snapshot(self) -> dict:
        """This student's practice counters for this offering.

        Reads the platform's own database, so it is exactly what
        `record_attempt` wrote — no cache, no second copy, nothing to go stale.

        Never raises. A mastery read failing must degrade the plan's ordering,
        not break the lesson page: the tutor renders, the plan simply treats
        every outcome as unattempted, which is the honest fallback.
        """
        user = self._user()
        if user is None:
            return {"offering_id": self._offering_id(), "clos": [], "truncated": False}

        student_id = str(user.opt_attrs.get("edx-platform.user_id", ""))
        try:
            from ..models import StudentMastery

            rows = StudentMastery.snapshot(student_id, self._offering_id())
        except Exception:  # noqa: BLE001
            log.exception("coursemate: mastery snapshot failed; continuing without it")
            rows = []

        return {
            "offering_id": self._offering_id(),
            "clos": rows[:MASTERY_WINDOW_CLOS],
            # Told, not silently trimmed: "no history for that outcome" and
            # "trimmed away" are different facts, and the agent is given the
            # difference rather than left to infer it.
            "truncated": len(rows) > MASTERY_WINDOW_CLOS,
        }

    def studio_view(self, context=None) -> Fragment:
        """Config, plus the "Index this course" button.

        That button is the normal path for bootstrap (§5.1) and it lives here
        deliberately: it puts the action in front of the person who just added the
        block, in a surface that already exists, needing no new MFE.
        """
        fragment = Fragment(
            loader.render_django_template(
                "static/html/studio_view.html",
                {
                    "display_name": self.display_name,
                    "enabled": self.enabled,
                    "mode": self.mode,
                },
            )
        )
        fragment.add_javascript(loader.load_unicode("static/js/src/studio.js"))
        fragment.initialize_js("CourseMateTutorStudio")
        return fragment

    # --- handlers: both return in milliseconds -------------------------------

    @XBlock.json_handler
    def mint(self, data, suffix=""):  # noqa: ARG002
        """Issue a short-lived token and get out of the way.

        This is the entire LMS involvement in answering a question.
        """
        if not self.enabled:
            return {"error": "disabled"}

        user = self._user()
        if user is None:
            return {"error": "unauthenticated"}

        user_id = str(user.opt_attrs.get("edx-platform.user_id", ""))
        username = user.opt_attrs.get("edx-platform.username", "") or None
        # `user_role` is a STRING, not a list. See identity.roles_of — reading it
        # with list() spelled every role out one letter at a time.
        roles = roles_of(user.opt_attrs)

        if not settings.COURSEMATE_JWT_SIGNING_KEY:
            # Unset key disables the tutor; it never breaks the platform (§10.4).
            return {"error": "unavailable"}

        usage_id = self.scope_ids.usage_id
        token = mint_student_token(
            signing_key=settings.COURSEMATE_JWT_SIGNING_KEY,
            user_id=user_id,
            username=username,
            course_id=self._course_id(),
            offering_id=self._offering_id(),
            roles=roles,
            usage_key=str(usage_id),
            block_id=usage_id.block_id,
            # Resolved here, inside the LMS, because only the platform can answer
            # which partition groups a user is in. Empty on any failure, which
            # denies gated content rather than granting it.
            group_tokens=list(self._group_tokens()),
            stream_path=settings.COURSEMATE_STREAM_PATH,
        )
        return token.model_dump()

    def _group_tokens(self) -> tuple[str, ...]:
        """The caller's access groups, via the adapter.

        Goes through `content_adapter` rather than importing PartitionService
        here: §3.3 puts every platform content read behind that one module, and
        contract 1 fails the build on a direct import from `xblock`.
        """
        from ..adapters import content_adapter

        real_user = self.runtime.service(self, "user")
        django_user = getattr(real_user, "_django_user", None) if real_user else None
        if django_user is None:
            return ()
        try:
            return content_adapter.user_group_tokens(
                self.scope_ids.usage_id.course_key, django_user
            )
        except Exception:  # noqa: BLE001 - a mint must never fail on this
            log.exception("coursemate: group token lookup failed")
            return ()

    @XBlock.json_handler
    def persist_turn(self, data, suffix=""):  # noqa: ARG002
        """Write a completed turn back to platform-owned storage.

        Called by the browser after the stream finishes. A dropped connection can
        therefore lose the last turn — a visible, recoverable annoyance, and
        cheaper than duplicating PII into a store retirement cannot reach (§3.1).
        """
        question = (data or {}).get("question", "").strip()
        answer = (data or {}).get("answer", "").strip()
        if not question or not answer:
            return {"saved": False}

        self.history = [
            *self.history,
            {"role": "student", "content": question},
            {
                "role": "tutor",
                "content": answer,
                # Citations are stored WITH the turn. Without this the live answer
                # is cited and the reloaded one is not — which quietly breaks the
                # product's central claim for any student who refreshes.
                "citations": clean_citations((data or {}).get("citations")),
            },
        ][-(HISTORY_WINDOW_TURNS * 2):]
        return {"saved": True, "turns": len(self.history)}

    @XBlock.json_handler
    def clear_history(self, data, suffix=""):  # noqa: ARG002
        self.history = []
        return {"cleared": True}

    @XBlock.json_handler
    def record_attempt(self, data, suffix=""):  # noqa: ARG002
        """Count one practice attempt. **The only write in Feature B.**

        It lives here, in the platform, and not on the agent's tool surface — and
        that placement is doing real work. Design §10.6 claims *"the agent's
        entire tool surface is read-only … there is no prompt that makes
        CourseMate change what students see."* A `record_mastery` tool would end
        that claim on the day it was added, in exchange for saving one HTTP
        handler. The claim is worth more.

        The student id comes from the platform session, never from the payload.
        The browser carries mastery *out*; it does not get to say whose it is on
        the way back.
        """
        user = self._user()
        if user is None:
            return {"error": "unauthenticated"}

        payload = data or {}
        clo_id = str(payload.get("clo_id", "")).strip()
        question_id = str(payload.get("question_id", "")).strip()
        attempt_id = str(payload.get("attempt_id", "")).strip()
        if not (clo_id and question_id and attempt_id):
            # `attempt_id` is required rather than defaulted, because a default
            # would make every attempt on the same question share a key — so a
            # student's second try at a question they got wrong would be
            # discarded as a replay, and their record would freeze at the first
            # answer they ever gave.
            return {"error": "clo_id, question_id and attempt_id are all required"}

        student_id = str(user.opt_attrs.get("edx-platform.user_id", ""))
        offering_id = self._offering_id()
        # Derived service-side from the question's difficulty and carried here;
        # "" when the source question had no estimate. Not trusted for anything
        # but bucketing this student's own counters.
        band = str(payload.get("difficulty_band") or "").strip().lower()
        if band not in ("", "easy", "medium", "hard"):
            return {"error": "difficulty_band must be easy, medium or hard"}

        from coursemate_contracts.mastery import idempotency_key

        from ..models import StudentMastery

        try:
            key = idempotency_key(
                offering_id=offering_id, student_id=student_id,
                clo_id=clo_id, question_id=question_id, attempt_id=attempt_id,
            )
        except ValueError:
            # A component containing the field separator could collide two
            # different attempts onto one digest. Refused rather than sanitised:
            # silently rewriting an id makes the collision harder to find, not
            # less likely.
            return {"error": "invalid identifier"}

        return StudentMastery.record(
            idempotency_key=key,
            student_id=student_id,
            offering_id=offering_id,
            clo_id=clo_id,
            difficulty_band=band,
            correct=bool(payload.get("correct")),
        )

    @XBlock.json_handler
    def index_course(self, data, suffix=""):  # noqa: ARG002
        """Enqueue the bootstrap job for this course (§5.1).

        Course staff only, and it *enqueues* rather than working: the platform's
        own `reindex_studio` learned this lesson before us — a command that
        indexes hundreds of blocks in-process dies with the session and resumes
        from nothing, so the work belongs in a resumable task.

        Guarded by an in-flight lock, because a bootstrap is an expensive job and
        this is a button: a second click attaches to the running job rather than
        queueing another.
        """
        from ..locks import bootstrap_lock
        from ..models import CourseIndexState
        from ..tasks.bootstrap import bootstrap_course

        user = self._user()
        if user is None:
            return {"error": "unauthenticated"}

        course_id = self._course_id()
        acquired = bootstrap_lock.acquire(course_id)
        if acquired:
            bootstrap_course.delay(course_id)

        state = CourseIndexState.for_course(course_id)
        return {
            "already_running": not acquired,
            "last_indexed": state.last_indexed_display(),
            "block_count": state.block_count,
        }

    @staticmethod
    def workbench_scenarios():
        return [("CourseMate Tutor", "<coursemate_tutor/>")]
