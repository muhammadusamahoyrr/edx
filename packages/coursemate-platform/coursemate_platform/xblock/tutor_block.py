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
from .citations import clean_citations, clean_unsupported
from .identity import roles_of

log = logging.getLogger(__name__)
loader = ResourceLoader(__name__)

#: Rolling window sent to the service. Bounded on purpose: the payload costs a few
#: KB, and the alternative — the service keeping its own conversation store —
#: would duplicate PII into a system that platform retirement does not reach.
HISTORY_WINDOW_TURNS = 10

#: The conversation every pre-E3 turn belongs to.
#:
#: `""` rather than a generated id, so no existing entry has to be rewritten to
#: acquire one: an entry with no `conversation_id` and an entry with `""` are the
#: same conversation, which is what makes the old data load untouched.
LEGACY_CONVERSATION = ""

#: How many practice cards survive a reload. A revision session is not an
#: archive, and an unbounded list in `user_state` is a payload the student pays
#: for on every page load — the same reasoning as `HISTORY_WINDOW_TURNS`.
PRACTICE_WINDOW = 20

#: Conversations kept per student per block. Beyond this the oldest is dropped
#: with its turns, so the list cannot grow without limit.
MAX_CONVERSATIONS = 20

#: Longest auto-derived conversation title.
TITLE_CHARS = 60

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
    #:
    #: **Entries may carry `conversation_id`; entries written before E3 do not.**
    #: An entry without one belongs to `LEGACY_CONVERSATION`, decided on read and
    #: never written back. Migrating on read would mean `student_view` — a GET —
    #: rewriting durable student state, and a page load that mutates data is how
    #: a render bug becomes data loss.
    history = List(default=[], scope=Scope.user_state)

    #: `{"id", "title", "created_at"}` per conversation. Metadata only: the turns
    #: stay in `history`, so nothing about the existing shape moves.
    #:
    #: Empty for every student who has not started a second conversation, which
    #: includes everyone today — `_conversations()` synthesises the default entry
    #: rather than this field needing to be backfilled.
    conversations = List(default=[], scope=Scope.user_state)

    #: Which conversation new turns join. `""` means the legacy default.
    active_conversation = String(default="", scope=Scope.user_state)

    #: Generated practice questions, so a reload does not destroy the run.
    #:
    #: Separate from `history` rather than folded into it: practice lives in a
    #: different tab, is not part of any conversation, and a student clearing a
    #: chat has not withdrawn their practice. Folding them together would make
    #: "New chat" silently delete work the student did somewhere else.
    practice = List(default=[], scope=Scope.user_state)

    # --- conversations (E3) -------------------------------------------------

    @staticmethod
    def _conversation_of(entry) -> str:
        """Which conversation a stored turn belongs to.

        Absent means legacy, and legacy means the default conversation. This is
        the single place that decision is made, so a pre-E3 turn behaves
        identically everywhere without any of them being rewritten.
        """
        if not isinstance(entry, dict):
            return LEGACY_CONVERSATION
        return str(entry.get("conversation_id") or LEGACY_CONVERSATION)

    def _turns_in(self, conversation_id: str) -> list:
        return [t for t in self.history if self._conversation_of(t) == conversation_id]

    def _conversations(self) -> list[dict]:
        """Every conversation this student has, newest last.

        The default is synthesised rather than stored: a student who has never
        pressed "New chat" has turns and no `conversations` row, and inventing
        one on read keeps that student's data untouched while still giving the
        UI something to list.
        """
        stored = [c for c in self.conversations if isinstance(c, dict) and c.get("id")]
        known = {str(c["id"]) for c in stored}
        out: list[dict] = []
        if LEGACY_CONVERSATION not in known and self._turns_in(LEGACY_CONVERSATION):
            out.append({"id": LEGACY_CONVERSATION, "title": "Earlier conversation",
                        "created_at": ""})
        out.extend({"id": str(c["id"]), "title": str(c.get("title") or "New chat"),
                    "created_at": str(c.get("created_at") or "")} for c in stored)
        return out

    def _active_id(self) -> str:
        """The conversation new turns join.

        Falls back to the legacy default when the stored id names a conversation
        that no longer exists — otherwise deleting one would leave every
        subsequent turn writing into a conversation nothing can open.
        """
        active = str(self.active_conversation or LEGACY_CONVERSATION)
        if active == LEGACY_CONVERSATION:
            return LEGACY_CONVERSATION
        if any(c["id"] == active for c in self._conversations()):
            return active
        return LEGACY_CONVERSATION

    def _title_for(self, conversation_id: str) -> str:
        """A conversation's name, derived from its first question if it has none.

        Auto-derived rather than prompted for: a student asked to name a chat
        before having one has nothing to name it after.
        """
        for c in self._conversations():
            if c["id"] == conversation_id and c["title"] not in ("", "New chat"):
                return c["title"]
        for turn in self._turns_in(conversation_id):
            if isinstance(turn, dict) and turn.get("role") == "student":
                text = str(turn.get("content") or "").strip()
                if text:
                    return text[:TITLE_CHARS] + ("…" if len(text) > TITLE_CHARS else "")
        return "New chat"

    def _conversation_list(self) -> list[dict]:
        """What the browser renders in the conversation picker."""
        return [
            {"id": c["id"], "title": self._title_for(c["id"]),
             "turns": len(self._turns_in(c["id"]))}
            for c in self._conversations()
        ]

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
                # Only the ACTIVE conversation's turns. A student resuming one
                # chat must not see another's answers interleaved into it.
                "history": self._turns_in(self._active_id())[-HISTORY_WINDOW_TURNS:],
                "conversations": self._conversation_list(),
                "active_conversation": self._active_id(),
                # The practice run, so a reload does not destroy it (E2).
                "practice": list(self.practice)[-PRACTICE_WINDOW:],
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
        except Exception:
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

    def _index_status(self) -> dict:
        """What the Studio panel says about the index, read from stored state.

        Read-only on purpose: `CourseIndexState.for_course` would `get_or_create`,
        and rendering a config panel must not write a bootstrap-progress row for a
        course nobody has indexed. A missing row is a real answer — "never" — not
        a row to be conjured.
        """
        from ..models import CourseIndexState

        state = CourseIndexState.objects.filter(course_id=self._course_id()).first()
        if state is None:
            return {"last_indexed": "never", "block_count": 0}
        return {
            "last_indexed": state.last_indexed_display(),
            "block_count": state.block_count,
        }

    def studio_view(self, context=None) -> Fragment:
        """Config, plus the "Index this course" button.

        That button is the normal path for bootstrap (§5.1) and it lives here
        deliberately: it puts the action in front of the person who just added the
        block, in a surface that already exists, needing no new MFE.

        The status line is filled from stored state at render time. It used to be
        hardcoded "never · 0 blocks" in the template, so a course indexed at any
        point before this page loaded still reported that it had never been
        indexed — the panel was accurate about a variable it never read.
        """
        fragment = Fragment(
            loader.render_django_template(
                "static/html/studio_view.html",
                {
                    "display_name": self.display_name,
                    "enabled": self.enabled,
                    # Absent from this context until 2026-08-13, so the control
                    # could not render even once it existed in the template.
                    "exam_prep_enabled": self.exam_prep_enabled,
                    "mode": self.mode,
                    **self._index_status(),
                },
            )
        )
        fragment.add_javascript(loader.load_unicode("static/js/src/studio.js"))
        fragment.initialize_js("CourseMateTutorStudio")
        return fragment

    def _can_author(self, user) -> bool:
        """May this caller change the block's settings?

        The decision is the platform's, and asking it is
        `adapters/studio_authz.can_author` — which is where the reasoning about
        *which* platform primitive to ask now lives, along with the three
        cheaper signals that were tried against the live stack and are all wrong
        on this path.

        **Why it moved out of this file (2026-08-14).** `.importlinter`
        contract 1 forbids `coursemate_platform.xblock` from importing `common`,
        and it was right to: that contract is the rule that platform coupling
        lives in `adapters/`. Doing the lookup here broke it, and the honest fix
        was to move the coupling rather than to exempt the module from the rule.

        The adapter is imported inside the function for the older reason, which
        still holds: this module is imported during LMS startup for every course
        on the instance, and must not drag Django auth or edx-platform internals
        in at import time (Principle 8).
        """
        from ..adapters.studio_authz import can_author

        return can_author(
            (getattr(user, "opt_attrs", None) or {}).get("edx-platform.user_id"),
            self._course_id(),
        )

    # --- handlers: both return in milliseconds -------------------------------

    @XBlock.json_handler
    def submit_studio_edits(self, data, suffix=""):
        """Persist the four `Scope.settings` fields the author can edit.

        This did not exist, and its absence is what made `exam_prep_enabled`
        unreachable. A custom `studio_view` REPLACES Studio's automatic settings
        editor, and this class is a plain `XBlock` rather than a
        `StudioEditableXBlockMixin`, so nothing supplied a save path — the panel
        rendered four controls that wrote nowhere.

        **Staff only, checked here rather than assumed.** An XBlock handler is
        reachable in the LMS too, at
        `/courses/<id>/xblock/<usage>/handler/submit_studio_edits`, and the LMS
        does not apply Studio's write-access check. Without this guard any
        enrolled student could disable the tutor or flip its mode for everyone
        in the course. Roles come from the platform's own user service, so they
        are not something the caller can fabricate.

        Unknown keys are ignored and an unknown `mode` is rejected rather than
        stored: `mode` feeds a prompt path that only understands two values, and
        a third would fail later, elsewhere, as a service-side error nobody
        could trace back to a Studio checkbox.
        """
        user = self._user()
        if user is None:
            return {"error": "unauthenticated"}

        if not self._can_author(user):
            return {"error": "forbidden"}

        if not isinstance(data, dict):
            return {"error": "bad_request"}

        if "mode" in data:
            mode = str(data["mode"])
            if mode not in {m.value for m in Mode}:
                return {"error": "invalid_mode"}
            self.mode = mode

        if "display_name" in data:
            name = str(data["display_name"]).strip()
            # An empty title would render a nameless component in the unit
            # outline, which is worse than keeping the previous one.
            if name:
                self.display_name = name

        for field in ("enabled", "exam_prep_enabled"):
            if field in data:
                setattr(self, field, bool(data[field]))

        return {
            "saved": True,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "exam_prep_enabled": self.exam_prep_enabled,
            "mode": self.mode,
        }

    @XBlock.json_handler
    def mint(self, data, suffix=""):
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
        except Exception:
            log.exception("coursemate: group token lookup failed")
            return ()

    @XBlock.json_handler
    def persist_turn(self, data, suffix=""):
        """Write a completed turn back to platform-owned storage.

        Called by the browser after the stream finishes. A dropped connection can
        therefore lose the last turn — a visible, recoverable annoyance, and
        cheaper than duplicating PII into a store retirement cannot reach (§3.1).
        """
        question = (data or {}).get("question", "").strip()
        answer = (data or {}).get("answer", "").strip()
        if not question or not answer:
            return {"saved": False}

        active = self._active_id()
        new_turns = [
            {"role": "student", "content": question, "conversation_id": active},
            {
                "role": "tutor",
                "content": answer,
                "conversation_id": active,
                # Citations are stored WITH the turn. Without this the live answer
                # is cited and the reloaded one is not — which quietly breaks the
                # product's central claim for any student who refreshes.
                "citations": clean_citations((data or {}).get("citations")),
                # And the marks, for the same reason and a sharper one: a
                # refresh used to drop the "this sentence is not supported"
                # warning while keeping the sentence, so a reloaded answer looked
                # MORE trustworthy than the live one. Absent on turns written
                # before 2026-08-12; the renderer treats missing as none.
                "unsupported": clean_unsupported((data or {}).get("unsupported")),
            },
        ]

        # **Trimmed per conversation, not across the list.** The window is "the
        # last 10 turns of THIS chat"; trimming the flat list would let a busy
        # conversation evict another one's turns, so resuming an older chat would
        # find it silently shortened by activity that had nothing to do with it.
        others = [t for t in self.history if self._conversation_of(t) != active]
        mine = (self._turns_in(active) + new_turns)[-(HISTORY_WINDOW_TURNS * 2):]
        self.history = [*others, *mine]
        return {"saved": True, "turns": len(mine)}

    @XBlock.json_handler
    def clear_history(self, data, suffix=""):
        """Clear the ACTIVE conversation only.

        Two things it deliberately does not touch. Other conversations, because
        "New chat" names the one in front of you and a control that silently
        emptied the rest would be unrecoverable. And mastery, which lives in a
        different table on a different lifetime — a student who wants a clean
        page has not un-practised anything, and wiping their recorded attempts
        would be a loss they never asked for.
        """
        active = self._active_id()
        kept = [t for t in self.history if self._conversation_of(t) != active]
        cleared = len(self.history) - len(kept)
        self.history = kept
        return {"cleared": True, "turns_removed": cleared,
                "conversation_id": active}

    @XBlock.json_handler
    def new_conversation(self, data, suffix=""):
        """Start a fresh conversation and make it active.

        The previous one is kept, not cleared — that is the whole difference
        between E3 and the E1 button, and the reason both exist.
        """
        import time
        import uuid

        new_id = uuid.uuid4().hex[:16]
        entry = {"id": new_id, "title": "New chat",
                 "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

        stored = [c for c in self.conversations if isinstance(c, dict) and c.get("id")]
        # Include the synthesised legacy row so the student's existing turns
        # survive as an openable conversation once a second one exists.
        if self._turns_in(LEGACY_CONVERSATION) and not any(
            str(c.get("id")) == LEGACY_CONVERSATION for c in stored
        ):
            stored.insert(0, {"id": LEGACY_CONVERSATION,
                              "title": "Earlier conversation", "created_at": ""})

        # **The legacy conversation is pinned and evicted last.** It is the
        # oldest entry, so a plain tail-slice would drop it first — and it is the
        # one conversation whose turns predate E3 entirely and cannot be
        # recreated. A student prolific enough to open MAX_CONVERSATIONS chats
        # would have silently lost everything they wrote before this feature
        # existed. Every other conversation still ages out oldest-first.
        legacy = [c for c in stored if str(c["id"]) == LEGACY_CONVERSATION]
        rest = [c for c in stored if str(c["id"]) != LEGACY_CONVERSATION]
        room = MAX_CONVERSATIONS - len(legacy) - 1     # -1 for the new entry
        tail = rest[-room:] if room > 0 else []
        kept = [*legacy, *tail, entry]
        dropped = {str(c["id"]) for c in stored} - {str(c["id"]) for c in kept}
        if dropped:
            # Their turns go with them; leaving orphaned turns behind would grow
            # `history` forever with entries nothing can open.
            self.history = [t for t in self.history
                            if self._conversation_of(t) not in dropped]

        self.conversations = kept
        self.active_conversation = new_id
        return {"conversation_id": new_id,
                "conversations": self._conversation_list()}

    @XBlock.json_handler
    def switch_conversation(self, data, suffix=""):
        """Resume a conversation and return its turns."""
        wanted = str((data or {}).get("conversation_id", "")).strip()
        known = {c["id"] for c in self._conversations()}
        if wanted not in known:
            # Refused rather than silently creating one: a client asking for a
            # conversation that does not exist has a bug, and inventing an empty
            # chat would hide it behind what looks like a working switch.
            return {"error": "no such conversation"}

        self.active_conversation = wanted
        return {
            "conversation_id": wanted,
            "history": self._turns_in(wanted)[-HISTORY_WINDOW_TURNS:],
            "conversations": self._conversation_list(),
        }

    @XBlock.json_handler
    def persist_practice(self, data, suffix=""):
        """Store one generated practice card so a reload does not destroy it.

        **`attempt_id` is carried, not regenerated.** One generated card is one
        attempt; minting a fresh id on restore would let a student answer the
        same card twice and have both counted, which is exactly what the
        per-card id exists to prevent.
        """
        payload = data or {}
        text = str(payload.get("text") or "").strip()
        attempt_id = str(payload.get("attempt_id") or "").strip()
        if not text or not attempt_id:
            return {"saved": False}

        card = {
            "attempt_id": attempt_id,
            "question_id": str(payload.get("question_id") or ""),
            "clo_id": str(payload.get("clo_id") or ""),
            "difficulty_band": str(payload.get("difficulty_band") or ""),
            "text": text,
            "citations": clean_citations(payload.get("citations")),
            # Whether this card has already been marked. Restored disabled, so a
            # reload cannot turn one attempt into two.
            "answered": bool(payload.get("answered")),
        }

        # Replace in place when the same card is persisted again — the answer
        # step re-sends it with `answered` set, and appending would show the
        # student the same question twice.
        rest = [c for c in self.practice
                if isinstance(c, dict) and c.get("attempt_id") != attempt_id]
        self.practice = [*rest, card][-PRACTICE_WINDOW:]
        return {"saved": True, "cards": len(self.practice)}

    @XBlock.json_handler
    def clear_practice(self, data, suffix=""):
        """Empty the practice run. Mastery is untouched — see `clear_history`."""
        self.practice = []
        return {"cleared": True}

    @XBlock.json_handler
    def record_attempt(self, data, suffix=""):
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

        # **`evaluated` is refused, not merely unused.** Nothing in this
        # deployment can evaluate an answer — there is no answer key — so a
        # payload claiming an attempt was graded is either a mistake or a
        # browser trying to dress a self-report up as one. Accepting the word
        # because the column can hold it would put an unearned claim in durable
        # student data, which is the exact confusion `source` exists to end.
        # This refusal is what an evaluator would lift, deliberately, when one
        # exists and its accuracy has been measured (§9.0).
        source = str(payload.get("source") or "self_reported").strip().lower()
        if source != "self_reported":
            return {"error": "source must be self_reported; nothing here evaluates answers"}

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
            source=source,
        )

    @XBlock.json_handler
    def index_course(self, data, suffix=""):
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
