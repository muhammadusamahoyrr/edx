"""Exhaustive/outline questions — a different query shape, answered deterministically.

The defect this exists for was measured, not suspected. Against the live OEX101
index, *"List all the topics covered in this course"* retrieved 3 of 55 chunks
(6.2% of distinct titles) and, from a byte-identical prompt, produced **ten
different answers in ten runs** — item counts of 0, 0, 2, 3, 6, 7, 7, 8, 10, 12.
The model was improvising the completeness the student asked for, out of a 6%
sample.

Three fixes were rejected on evidence and the tests below pin why:

* **more `top_k`** — a bigger selection is still a selection, and
  `max_output_tokens=400` cannot hold 48 cited topics regardless
* **`temperature=0`** — measured; narrowed the spread and still produced four
  different answers under a fixed seed, `top_p=1` and a pinned upstream provider
* **enumerating `display_name`** — technically complete and pedagogically
  useless: the live course's titles include `Text` x4, `Module Summary` x3,
  `Feedback` x3 and `Thank You!`

What remains is to stop asking a relevance ranker a question about structure.
"""

from __future__ import annotations

import time

import pytest
from coursemate_contracts.auth import AUDIENCE_STUDENT, StudentClaims
from coursemate_contracts.chat import ChatRequest, FrameType, Mode
from coursemate_contracts.errors import ErrorCode
from coursemate_service.ai.pipeline import AnswerPipeline
from coursemate_service.ai.query import is_outline_query
from coursemate_service.knowledge.store import ChunkStore

OFFERING = "course-v1:OpenedX+OEX101+2023"

#: Real sentences from the live course, used because the selector matches on
#: authored prose. Invented prose would test the test, not the course.
LEARNING_OBJECTIVES = (
    "After finishing this course you'll: Learn a bit about the project's "
    "history; Know what we mean when we talk about the \"Open edX community\"; "
    "Understand how the community operates."
)
MODULE_SUMMARY_1 = (
    "In this module, we learned: The Open edX project is a leading open-source "
    "learning software technology that powers e-learning sites worldwide."
)
MODULE_SUMMARY_2 = (
    "In this module, we learned: As a community, we use different "
    "communications mediums: Discourse, Confluence wiki, GitHub, Slack."
)
#: Blocks that TITLE matching was measured to select wrongly. They must not be
#: selected by body matching.
HISTORY_OVERVIEW = "The Open edX project began as a collaboration in 2012."
TAKEAWAYS = "The following video is a presentation given by Feanil Patel."
COMMUNITY_CHANNELS = "We use Discourse, Slack, Confluence and GitHub."


def _rows(offering: str, version: str, blocks, tenant="default"):
    return [
        {
            "tenant": tenant, "course_id": offering, "offering_id": offering,
            "usage_key": f"block-v1:{offering}+type@html+block@{i}",
            "block_id": f"b{i}", "block_type": "html", "content_type": "lesson",
            "display_name": name, "version": version, "ordinal": 0, "text": text,
            "group_tokens": groups,
        }
        for i, (name, text, groups) in enumerate(blocks)
    ]


COURSE = [
    ("Text", "Welcome.", ()),
    ("Learning Objectives", LEARNING_OBJECTIVES, ()),
    ("History overview", HISTORY_OVERVIEW, ()),
    ("Community Channels", COMMUNITY_CHANNELS, ()),
    ("Module Summary", MODULE_SUMMARY_1, ()),
    ("Takeaways", TAKEAWAYS, ()),
    ("Module Summary", MODULE_SUMMARY_2, ()),
    ("Feedback", "Tell us what you think.", ()),
]


@pytest.fixture
def store(tmp_path) -> ChunkStore:
    s = ChunkStore(tmp_path / "idx.db")
    s.write_chunks(_rows(OFFERING, "v1", COURSE))
    s.swap(OFFERING, "v1")
    return s


def _claims(offering=OFFERING, groups=()) -> StudentClaims:
    now = int(time.time())
    return StudentClaims(
        sub="5", username="cm_student", course_id=offering, offering_id=offering,
        aud=AUDIENCE_STUDENT, exp=now + 300, iat=now, group_tokens=list(groups),
    )


# --- 1. classification ------------------------------------------------------


@pytest.mark.parametrize("q", [
    "List all the topics covered in this course.",
    "Enumerate all modules in this course.",
    "Give me an overview of this course.",
    "What is the full list of topics in this course?",
    # "complete"/"full" are no longer enumeration signals on their own; this
    # routes on "list", which is what actually asks for a set.
    "List the syllabus for this course.",
    # The VERB use of outline still routes, via "contents" — only the NOUN use
    # ("the course overview page") was dropped.
    "outline the contents of this course",
    "What are all the topics covered in the course?",
    "List all topics in this course.",
    "Give me an outline of this course.",
    ("List all the topics covered in this course. Use only the course material. "
     "Do not add information from your general knowledge."),
    ("List the topics covered in this course. Use only the retrieved course "
     "material and cite each topic."),
])
def test_exhaustive_questions_route_to_the_outline_path(q):
    assert is_outline_query(q) is True


@pytest.mark.parametrize("q", [
    # The measured near-miss: has "topics" and "course", but asks about ONE
    # subject. Answering it with the whole overview would be a worse answer.
    ("What topics does this course teach about contributing to the Open edX "
     "community? Use only the course material and cite the relevant course "
     "sources."),
    "What topics are covered in the section about the roadmap?",
    "What is a named release?",
    "What does the roadmap section teach?",
    "Summarise the roadmap section.",
    "Give me an overview of the roadmap section.",
    "List all the ways to contribute code, regarding pull requests.",
    "How do I contribute code?",
    "Why would I use one?",
    "",
    "   ",
])
def test_ordinary_questions_do_not(q):
    assert is_outline_query(q) is False


# --- adversarial: the false positives review B1 found ----------------------
#
# These were NOT in the original negative set, and that is the point. The first
# negative set was drawn from the specification — the questions someone had
# already thought of — and it passed while the classifier misrouted six of nine
# plausible adjacent questions. These are the probes that found them.


@pytest.mark.parametrize("q", [
    # "complete", "full", "whole", "entire" as predicate adjectives: each of
    # these asks about a PROPERTY of the course, not for its contents.
    "Is this course complete?",
    "Is the full course available offline?",
    "Can I download the full course?",
    "Do I get a certificate for the whole course?",
    "Is the entire course free?",
    # "overview" as a NOUN naming a page, rather than a request for one.
    "Where is the course overview page?",
    "Is there a course outline page?",
])
def test_adversarial_questions_do_not_route_to_the_outline_path(q):
    """Answering any of these with the author's overview would replace an answer
    the student asked for with one they did not."""
    assert is_outline_query(q) is False


@pytest.mark.parametrize("q", [
    "Are all lessons graded?",
    "I finished all the modules, what now?",
    # Found by a later sweep, after the B1 fix landed — same family, same cause.
    # Recorded here rather than left in a report, because a known defect that
    # lives only in prose is one nobody is told about when they touch the rule.
    "Do all sections have quizzes?",
])
@pytest.mark.xfail(strict=True, reason=(
    "Known remaining false positive. All three pair a bare quantifier with a "
    "plural content noun while asking about a PROPERTY of the course rather "
    "than its contents. Separating them needs to know 'graded' and 'have "
    "quizzes' are properties while 'covered' is not, which a word list cannot "
    "do. strict=True so this flips the suite red the moment someone fixes it."
))
def test_quantifier_plus_plural_noun_is_still_over_eager(q):
    assert is_outline_query(q) is False


def test_the_removed_adjectives_no_longer_signal_on_their_own():
    """Pinned as behaviour, not as a word list, so re-adding one is visible."""
    assert is_outline_query("Is this course complete?") is False
    assert is_outline_query("Show me the complete course") is False
    # ...but a real enumeration request containing them still routes, because
    # the routing was never resting on the adjective.
    assert is_outline_query("List the complete set of topics in this course") is True


def test_overview_and_outline_need_their_complement():
    """The noun/verb split, pinned in both directions."""
    assert is_outline_query("Give me an overview of this course.") is True
    assert is_outline_query("Give me an outline of this course.") is True
    assert is_outline_query("Where is the course overview page?") is False
    assert is_outline_query("The course outline page is missing") is False


def test_a_narrowing_phrase_always_wins():
    """Enumeration + whole-course scope is not enough if the student narrowed it.

    `about` is the measured case: the second reproduction question carries every
    other signal and is a question about ONE topic."""
    assert is_outline_query("List all the topics in this course") is True
    assert is_outline_query("List all the topics in this course about grading") is False


def test_the_singular_narrows_and_the_plural_does_not():
    """"the roadmap section" is one part; "all sections" is the whole course.
    That asymmetry is the rule doing its job, so it is pinned."""
    assert is_outline_query("List every section of this course") is True
    assert is_outline_query("What does this section cover?") is False


def test_enumeration_alone_is_not_enough():
    """Fail-safe direction: without a whole-course scope word this is a normal
    question and must keep its normal answer."""
    assert is_outline_query("List the steps to open a pull request") is False


def test_scope_alone_is_not_enough():
    assert is_outline_query("What is this course about?") is False


def test_classification_is_pure():
    q = "List all the topics covered in this course."
    assert len({is_outline_query(q) for _ in range(10)}) == 1


# --- 2. source selection ----------------------------------------------------


def test_only_author_summary_blocks_are_selected(store):
    got = store.summary_blocks(OFFERING, tenant="default")
    assert [c.display_name for c in got] == [
        "Learning Objectives", "Module Summary", "Module Summary",
    ]


def test_title_lookalikes_are_not_selected(store):
    """`History overview`, `Takeaways` and DemoX's `Assessments Summary` were
    measured to fool TITLE matching. Body matching must reject them."""
    selected = {c.display_name for c in store.summary_blocks(OFFERING, tenant="default")}
    assert "History overview" not in selected
    assert "Takeaways" not in selected
    assert "Community Channels" not in selected


def test_a_course_with_no_author_summaries_returns_nothing(tmp_path):
    """The DemoX case: 227 chunks, zero summary blocks. Empty is the correct
    answer, and the caller must fall back rather than invent an overview."""
    s = ChunkStore(tmp_path / "d.db")
    s.write_chunks(_rows("DemoX", "v1", [
        ("Overview", "This page introduces the demonstration course.", ()),
        ("Assessments Summary", "A summary of assessment types available.", ()),
    ]))
    s.swap("DemoX", "v1")
    assert s.summary_blocks("DemoX", tenant="default") == []


def test_selection_is_deterministic_and_ordered(store):
    runs = [
        [(c.usage_key, c.text) for c in store.summary_blocks(OFFERING, tenant="default")]
        for _ in range(10)
    ]
    assert all(r == runs[0] for r in runs)


def test_punctuation_does_not_decide_selection(tmp_path):
    """"we learned:" and "we learned" are the same authored sentence."""
    s = ChunkStore(tmp_path / "p.db")
    s.write_chunks(_rows("C", "v1", [
        ("A", "In this module we learned that ordering matters.", ()),
        ("B", "In this module, we learned that punctuation does not.", ()),
    ]))
    s.swap("C", "v1")
    assert len(s.summary_blocks("C", tenant="default")) == 2


# --- 3. isolation and access control ---------------------------------------


def test_another_offering_is_never_returned(tmp_path):
    s = ChunkStore(tmp_path / "two.db")
    s.write_chunks(_rows(OFFERING, "v1", COURSE))
    s.write_chunks(_rows("course-v1:OpenedX+DemoX+DemoCourse", "v1", [
        ("Leak", "In this module, we learned DemoX secrets.", ()),
    ]))
    s.swap(OFFERING, "v1")
    s.swap("course-v1:OpenedX+DemoX+DemoCourse", "v1")

    got = s.summary_blocks(OFFERING, tenant="default")
    assert all("DemoX" not in c.usage_key for c in got)
    assert "Leak" not in {c.display_name for c in got}


def test_another_tenant_is_never_returned(tmp_path):
    s = ChunkStore(tmp_path / "t.db")
    s.write_chunks(_rows(OFFERING, "v1", COURSE, tenant="default"))
    s.write_chunks(_rows(OFFERING, "v2", [
        ("Other tenant", "In this module, we learned tenant-b material.", ()),
    ], tenant="tenant-b"))
    s.swap(OFFERING, "v1")

    got = s.summary_blocks(OFFERING, tenant="default")
    assert "Other tenant" not in {c.display_name for c in got}


def test_restricted_summary_blocks_are_hidden_without_the_group_token(tmp_path):
    """**The mandatory one.** A summary block is course content. This path must
    hide restricted content exactly as `search()` does — it shares that SQL
    rather than restating it, and this is the test that proves the sharing
    works rather than merely looks right."""
    s = ChunkStore(tmp_path / "r.db")
    s.write_chunks(_rows(OFFERING, "v1", [
        ("Public Summary", "In this module, we learned the public part.", ()),
        ("Cohort Summary", "In this module, we learned the restricted part.",
         ("cohort:paid",)),
    ]))
    s.swap(OFFERING, "v1")

    without = {c.display_name for c in s.summary_blocks(OFFERING, tenant="default")}
    assert without == {"Public Summary"}, "restricted summary leaked to a caller with no groups"

    with_token = {
        c.display_name for c in s.summary_blocks(
            OFFERING, tenant="default", group_tokens=frozenset({"cohort:paid"})
        )
    }
    assert with_token == {"Public Summary", "Cohort Summary"}


def test_a_wrong_group_token_does_not_unlock_restricted_content(tmp_path):
    s = ChunkStore(tmp_path / "w.db")
    s.write_chunks(_rows(OFFERING, "v1", [
        ("Cohort Summary", "In this module, we learned the restricted part.",
         ("cohort:paid",)),
    ]))
    s.swap(OFFERING, "v1")
    got = s.summary_blocks(
        OFFERING, tenant="default", group_tokens=frozenset({"cohort:free"})
    )
    assert got == []


def test_inactive_versions_are_not_selected(tmp_path):
    """Superseded content is unreachable by retrieval and must be unreachable
    here — otherwise an outline could describe a course that no longer exists."""
    s = ChunkStore(tmp_path / "v.db")
    s.write_chunks(_rows(OFFERING, "v1", [
        ("Old Summary", "In this module, we learned the old thing.", ()),
    ]))
    s.swap(OFFERING, "v1")
    s.write_chunks(_rows(OFFERING, "v2", [
        ("New Summary", "In this module, we learned the new thing.", ()),
    ]))
    s.swap(OFFERING, "v2")

    assert [c.display_name for c in s.summary_blocks(OFFERING, tenant="default")] == [
        "New Summary"
    ]


# --- 4. the pipeline path ---------------------------------------------------


class _Recorder:
    """A context provider that records whether ordinary retrieval was used."""

    def __init__(self, outline_chunks, index_missing=False):
        self.outline_chunks = outline_chunks
        self.index_missing = index_missing
        self.fetch_calls = 0
        self.outline_calls = 0

    async def fetch(self, question, claims):
        from coursemate_service.ai.context import ContextResult
        self.fetch_calls += 1
        return ContextResult(chunks=[], top_score=0.0, index_missing=False,
                             index_version="v1")

    async def fetch_outline(self, claims):
        from coursemate_service.ai.context import ContextResult
        self.outline_calls += 1
        return ContextResult(chunks=list(self.outline_chunks), top_score=0.0,
                             index_missing=self.index_missing, index_version="v1")


def _chunks_from(store_):
    from coursemate_contracts.chat import Citation
    from coursemate_service.ai.context import ContextChunk
    return [
        ContextChunk(
            text=b.text,
            citation=Citation(usage_key=b.usage_key,
                              display_name=b.display_name or b.block_id,
                              url=f"/courses/{OFFERING}/jump_to/{b.usage_key}"),
            score=b.score,
        )
        for b in store_.summary_blocks(OFFERING, tenant="default")
    ]


def _request(q="List all the topics covered in this course."):
    return ChatRequest(question=q, history=[], mode=Mode.DIRECT)


async def _drain(pipeline, request, claims):
    return [f async for f in pipeline.stream(request, claims)]


@pytest.mark.asyncio
async def test_an_outline_question_never_calls_the_model(store, monkeypatch):
    """The property that makes the answer reproducible. A model call here would
    reintroduce exactly the sampling this path exists to remove."""
    import coursemate_service.ai.pipeline as pl

    def _boom():
        raise AssertionError("the outline path must not reach the provider")

    monkeypatch.setattr(pl, "get_router", _boom)
    provider = _Recorder(_chunks_from(store))
    frames = await _drain(AnswerPipeline(provider), _request(), _claims())

    assert provider.outline_calls == 1
    assert provider.fetch_calls == 0
    assert frames[0].type is FrameType.TOKEN


@pytest.mark.asyncio
async def test_ten_identical_outline_requests_are_byte_identical(store):
    provider = _Recorder(_chunks_from(store))
    pipeline = AnswerPipeline(provider)
    runs = []
    for _ in range(10):
        frames = await _drain(pipeline, _request(), _claims())
        runs.append("".join(f.model_dump_json() for f in frames))
    assert len(set(runs)) == 1, "the outline path is not deterministic"


@pytest.mark.asyncio
async def test_every_selected_block_is_cited(store):
    provider = _Recorder(_chunks_from(store))
    frames = await _drain(AnswerPipeline(provider), _request(), _claims())

    cited = [f.citation for f in frames if f.type is FrameType.CITATION]
    expected = store.summary_blocks(OFFERING, tenant="default")
    assert len(cited) == len(expected)
    assert [c.usage_key for c in cited] == [b.usage_key for b in expected]
    assert all(c.url.endswith(c.usage_key) for c in cited)


def test_duplicate_titles_stay_distinguishable(store):
    """Two blocks are both called `Module Summary`. The citation identity is the
    usage_key, so they must remain separate links rather than collapsing."""
    blocks = store.summary_blocks(OFFERING, tenant="default")
    dupes = [b for b in blocks if b.display_name == "Module Summary"]
    assert len(dupes) == 2
    assert len({b.usage_key for b in dupes}) == 2


@pytest.mark.asyncio
async def test_the_answer_claims_authorship_not_completeness(store):
    provider = _Recorder(_chunks_from(store))
    frames = await _drain(AnswerPipeline(provider), _request(), _claims())
    text = "".join(f.text or "" for f in frames if f.type is FrameType.TOKEN)

    assert "author-provided overview" in text
    assert "may not name every page" in text
    # The failure this change exists for was a partial answer that read as a
    # complete one. It must not describe itself as every topic in the course.
    assert "all the topics" not in text.lower()
    assert "every topic" not in text.lower()


@pytest.mark.asyncio
async def test_the_author_text_is_reproduced_not_paraphrased(store):
    provider = _Recorder(_chunks_from(store))
    frames = await _drain(AnswerPipeline(provider), _request(), _claims())
    text = "".join(f.text or "" for f in frames if f.type is FrameType.TOKEN)

    for block in store.summary_blocks(OFFERING, tenant="default"):
        assert block.text.strip() in text


@pytest.mark.asyncio
async def test_no_author_summaries_falls_back_to_ordinary_retrieval(store):
    """DemoX's case. Falling through is the honest outcome; claiming an outline
    would be the defect in a new costume."""
    provider = _Recorder([])
    frames = await _drain(AnswerPipeline(provider), _request(), _claims())

    assert provider.outline_calls == 1
    assert provider.fetch_calls == 1, "did not fall back to ordinary retrieval"
    # Ordinary retrieval returned nothing, so the gate abstains — unchanged.
    assert frames[-1].type is FrameType.ERROR
    assert frames[-1].error_code is ErrorCode.ABSTAINED


@pytest.mark.asyncio
async def test_an_unindexed_course_says_preparing_not_abstained(store):
    """"still being prepared" invites the student back; "not covered" does not.
    §5.1's distinction has to survive on this path too."""
    provider = _Recorder([], index_missing=True)
    frames = await _drain(AnswerPipeline(provider), _request(), _claims())

    assert frames[-1].error_code is ErrorCode.PREPARING


@pytest.mark.asyncio
async def test_a_provider_without_outline_support_is_not_an_error(store):
    """Existing test doubles implement only `fetch`. They must keep working, and
    an outline question through one of them must degrade to retrieval."""

    class _OldProvider:
        def __init__(self):
            self.fetch_calls = 0

        async def fetch(self, question, claims):
            from coursemate_service.ai.context import ContextResult
            self.fetch_calls += 1
            return ContextResult(chunks=[], top_score=0.0, index_missing=False)

    provider = _OldProvider()
    frames = await _drain(AnswerPipeline(provider), _request(), _claims())
    assert provider.fetch_calls == 1
    assert frames[-1].type is FrameType.ERROR


# --- 5. ordinary questions are untouched ------------------------------------


@pytest.mark.asyncio
async def test_an_ordinary_question_does_not_touch_the_outline_path(store):
    provider = _Recorder(_chunks_from(store))
    await _drain(AnswerPipeline(provider), _request("What is a named release?"), _claims())

    assert provider.outline_calls == 0
    assert provider.fetch_calls == 1
