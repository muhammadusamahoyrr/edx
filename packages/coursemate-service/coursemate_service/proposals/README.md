# Proposal queue — designed, dormant

**Nothing imports this.** `.importlinter` contract 4 enforces that, and deleting
the contract is the deliberate act that switches the instructor loop on.

Specified in design §9.1. Dormant because §1.2 defers the entire instructor loop:
**the MVP generates no course content at all**, so the queue has nothing to hold
and nothing requires instructor approval. Principle 2 is satisfied by construction
rather than by a review UI, which is a stronger position than shipping the UI.

It is specified in full anyway, and kept here rather than deleted, because it is
the design's answer to a real platform hazard:

> Open edX publish is **subtree semantics**. Content parked in a unit's draft
> branch goes live when an instructor publishes that unit for an unrelated reason
> — so "the AI writes to draft and a human publishes" is not a gate, it is hoping
> the instructor notices a new block.

The queue lives outside the course tree, so no publish action can reach it.
Approval *causes* the write rather than filtering it afterward.

The same bug points the other way at accept time: if the target container holds
the instructor's own unpublished work, "write to draft and publish" would ship
their unfinished edits as a side effect. Accept therefore checks for other pending
draft changes and shows exactly what else would go live before doing anything.

The `origin` field (`ai_proposal` | `student_request`) exists so that neither the
AI path nor "ask my instructor to add this" needs a schema change when it lands.
