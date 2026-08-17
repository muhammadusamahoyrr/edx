"""What to search for — which is not the same thing as what the student typed.

Until this module existed the two were conflated: `pipeline.py` passed
`request.question` straight to the retriever, so a follow-up like *"why?"* was
searched with no idea what "why" referred to. The conversation reached the MODEL
through `history` and never reached the RETRIEVER.

Measured before the change, over 12 multi-turn cases against the live DemoX
index: **r@3 = 0.333, and 7 of 12 retrieved unrelated content ABOVE the
confidence threshold** — so the pipeline answered, fluently and with a citation,
from the wrong lesson. The sharpest case scored 1.000: *"How do I try it out?"*
after *"What is Studio?"* matched a block literally named `Try it -`. Maximum
confidence, wrong content.

**Pure, and that is the point.** No I/O, no clock, no model. The whole decision
about what gets searched is one function that can be exercised exhaustively in
microseconds, which matters because this is behaviour that will be tuned more
than once.

**No LLM rewriter, deliberately.** The standard RAG answer is to ask a model to
rewrite the follow-up into a standalone question. That would put a model call
*before* the confidence gate and destroy the property this system is built on:
abstention costs 3 ms and no spend. On a local CPU model it would add ~24 s to
every question including the ones we refuse.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from coursemate_contracts.chat import Role, Turn

#: Words that cannot mean anything on their own — they stand in for something
#: said earlier. Their presence is the signal that a question needs its
#: conversation to be searchable.
#:
#: **Pronouns only, and that boundary is deliberate.** Personal, demonstrative
#: and indefinite pro-forms are a closed grammatical class, so this list is
#: finite and stable rather than a growing pile of observed phrasings. The moment
#: it starts collecting ordinary nouns because a case failed, it has stopped
#: being a rule and become a lookup table for the eval set.
#:
#: Measured on the 18 conversational cases: this separates 17 of 18 correctly.
#: The one it misses (`m05`, *"Can you give an example?"*) is under-specified by
#: **ellipsis** rather than anaphora — "an example *of what*" — and contains no
#: pronoun to find. Catching it needs to know which nouns are topical in this
#: corpus, which is a different kind of signal and is not attempted here.
_PRO_FORMS = frozenset({
    # personal
    "it", "its", "they", "them", "their", "theirs",
    "he", "him", "his", "she", "her", "hers",
    # demonstrative
    "this", "that", "these", "those",
    # indefinite, standing for a prior referent
    "one", "ones", "another", "others", "something", "anything",
})

_WORD = re.compile(r"[a-z']+")


def is_under_specified(question: str) -> bool:
    """Whether this question needs its conversation to be searchable.

    A question is under-specified when it leans on a pro-form: *"why would I use
    **one**"*, *"who grades **it**"*, *"can I moderate **them**"*. Strip the
    conversation away and there is nothing left to search for.

    A self-contained question — *"What is SCORM?"* — is not, and must be left
    alone. B1 measured what happens when it is not: prepending history to a
    topic change dragged the previous subject into the query and pushed the
    correct block from rank 1 to rank 3.
    """
    return any(tok in _PRO_FORMS for tok in _WORD.findall(question.lower()))


def last_student_turn(history: Sequence[Turn], exclude: str | None = None) -> str | None:
    """The most recent thing the student said BEFORE `exclude`, or None.

    Tutor turns are skipped. They are model output, and searching on our own
    previous answer would feed the retriever whatever the model happened to say
    — including, on a bad turn, content it drew from the wrong lesson. Only what
    the student actually typed is evidence of what they are asking about.

    **`exclude` exists because the browser sends the current question inside
    `history`.** `tutor.js` pushes the question onto its history array before
    building the request, so the wire payload ends
    `[…, {student: "why?"}]` *and* carries `question: "why?"`. Without this the
    "previous" turn is the current question echoed back, the query becomes
    `"why? why?"`, and the whole reconstruction is a no-op that merely doubles
    the question.

    Found by browser verification on 2026-08-12, after the unit tests and the
    offline eval both passed: they build history the way the *contract* reads,
    with prior turns only, and production sends a different shape. A harness that
    constructs its own input cannot discover what a real client actually sends.
    """
    for turn in reversed(list(history)):
        if turn.role != Role.STUDENT:
            continue
        text = turn.content.strip()
        if not text or (exclude is not None and text == exclude):
            continue
        return text
    return None


#: Words that ask for a COMPLETE SET rather than for an answer to a question.
#:
#: Deliberately excludes "summarise"/"summary": *"summarise the roadmap section"*
#: asks for a précis of one part, not for the course's contents, and admitting it
#: would route a narrowing question down the exhaustive path.
#:
#: **`complete`, `full`, `whole` and `entire` were here and were removed**, after
#: adversarial probing showed they are usually predicate adjectives describing
#: something *other* than a set of topics. They misrouted four plausible
#: questions — *"Is this course complete?"*, *"Is the full course available
#: offline?"*, *"Can I download the full course?"*, *"Do I get a certificate for
#: the whole course?"* — each of which would have been answered with the course
#: overview instead of with an answer. None of the questions this path exists for
#: needs them: they all carry `list`, `enumerate`, `all`, `every` or `contents`.
_ENUMERATION = frozenset({
    "list", "lists", "listing", "enumerate", "enumerates",
    "all", "every", "contents",
})

#: Enumeration signals that are only signals in PHRASE form.
#:
#: Bare "overview" and "outline" are frequently nouns naming a page — *"where is
#: the course overview page?"* — and routing those produced an overview of the
#: course in answer to a question about navigation. Requiring the `of` complement
#: keeps *"give me an overview of this course"* while dropping the noun use.
#:
#: The verb use (*"outline the contents of this course"*) still routes, via
#: `contents` in `_ENUMERATION`, so this narrowing costs nothing measured.
_ENUMERATION_PHRASES: tuple[str, ...] = ("overview of", "outline of")

#: Nouns that mean THE COURSE AS A WHOLE. Unambiguous on their own.
_WHOLE_COURSE = frozenset({"course", "syllabus", "curriculum"})

#: Content nouns that mean "everything of this kind" — but only in the PLURAL.
#: Singular "section"/"module" narrows ("the roadmap section"), so it is absent
#: here on purpose: that asymmetry is what separates *"list all modules"* from
#: *"what does the roadmap section teach"*.
_CONTENT_PLURAL = frozenset({
    "topics", "modules", "sections", "units", "lessons", "chapters",
})

#: Phrases that narrow an otherwise-exhaustive question to one part of the
#: course. Their presence returns the question to ordinary retrieval, because a
#: FILTERED enumeration is not something the author-summary source can answer —
#: it would return the whole overview to a student who asked about one topic.
_NARROWING = (
    "about", "regarding", "concerning", "related to", "with respect to",
    "in the section", "in the module", "in the chapter",
    "in section", "in module", "in chapter", "specific to",
)


def is_outline_query(question: str) -> bool:
    """Whether this asks for the course's CONTENTS rather than about its content.

    Three conditions, all required, and the conjunction is the whole design:

    1. an enumeration signal — the student wants a complete set, not an answer
    2. a whole-course scope signal — that set is *the course*, not one part
    3. no narrowing phrase — "all topics **about** contributing" is a filtered
       question and the author-summary source cannot answer it honestly

    **Fails safe.** Every uncertain case returns False and falls through to the
    ordinary retrieve → rerank → gate → LLM path. That direction is chosen
    deliberately: a false negative reproduces today's behaviour, which is known
    and survivable, while a false positive answers a specific question with a
    course overview — surprising, and wrong in a way the student cannot detect.

    Pure, like `is_under_specified` above and for the same reason: the whole
    routing decision can be exercised exhaustively in microseconds.

    It is knowingly conservative. *"What is covered in this course?"* carries no
    enumeration word and therefore does NOT route here. Widening the rule to
    catch it is a measurable change and should be made against real questions,
    not guessed.

    **Three known false positives remain, and they are recorded rather than
    papered over.** *"Are all lessons graded?"*, *"I finished all the modules,
    what now?"* and *"Do all sections have quizzes?"* all pair a bare quantifier
    with a plural content noun while asking about a PROPERTY of the course rather
    than its contents. Separating those needs to know that "graded" is a property
    and "covered" is not, which a word list cannot do; the honest options are a
    parser or a small classifier, and neither is worth it for three phrasings.
    `test_outline_query.py` pins them as `xfail(strict=True)`, so whoever does
    fix it is told immediately.
    """
    text = " ".join(str(question or "").split()).lower()
    if not text:
        return False

    # Narrowing is checked on the raw string, because these are phrases rather
    # than tokens ("in the section", "related to").
    if any(phrase in text for phrase in _NARROWING):
        return False

    tokens = set(_WORD.findall(text))
    enumerating = bool(tokens & _ENUMERATION) or any(
        phrase in text for phrase in _ENUMERATION_PHRASES
    )
    if not enumerating:
        return False
    return bool(tokens & (_WHOLE_COURSE | _CONTENT_PLURAL))


def retrieval_query(
    question: str,
    history: Sequence[Turn] = (),
    usage_key: str | None = None,
) -> str:
    """The string to retrieve on.

    **Empty history returns the question untouched.** That is not an
    optimisation — it is what keeps the single-turn arms byte-identical to their
    measured baseline, so a change here cannot silently move numbers that were
    not supposed to move.

    **`usage_key` is accepted and deliberately not used yet.** It is an opaque
    block id (`block-v1:…+block@0f594fe7…`) with no searchable text in it, so
    consuming it means resolving it to a title through the store — I/O, which
    does not belong in a pure function. What this signature does buy now is the
    precedence rule, pinned by a test: **when history exists, the result must not
    depend on `usage_key` at all.** The page is where the student happens to be
    standing; the conversation is what they are asking about, and the two
    disagree the moment a student carries a question to a different page. Wiring
    page context later must not be able to override the conversation, and the
    test makes that impossible to get wrong quietly.
    """
    question = question.strip()

    # A question that stands on its own is searched exactly as asked. Augmenting
    # it can only dilute it: B1 prepended history unconditionally and topic-change
    # r@1 fell from 0.750 to 0.250 — the right block still retrieved, no longer
    # first, because the previous subject's terms were competing with it.
    if not is_under_specified(question):
        return question

    # `exclude=question`: the browser puts the current question in `history` as
    # well as in `question`, so without this the "previous" turn is the question
    # itself. See `last_student_turn`.
    previous = last_student_turn(history, exclude=question)
    if previous is None:
        return question

    # Previous turn first, current question second — the order the measurement
    # was taken with. One turn only: it bounds how long the query can grow, and
    # the measurement says one is enough.
    return f"{previous} {question}"
