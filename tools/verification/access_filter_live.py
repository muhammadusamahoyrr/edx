"""Does the block-access filter actually hide anything on the LIVE index?

Everything up to now proved the parts: probe 7 found two group-restricted blocks,
the deploy carried their tokens into the index. Neither shows the filter *doing
its job* — and a filter that is present but matching nothing looks exactly like a
filter that is working, because both return "no restricted content leaked".

So this asks the store the same question twice, as two different callers, and
requires the answers to DIFFER. A test that cannot fail is not evidence.

    docker exec tutor_local-coursemate-1 python /tmp/access_filter_live.py
"""

from __future__ import annotations

import sys

from coursemate_service.config import settings
from coursemate_service.knowledge import get_store


def main() -> int:
    store = get_store()

    rows = list(store._conn.execute(
        """SELECT c.id, c.offering_id, c.usage_key, c.display_name, c.text,
                  g.group_token
           FROM chunks c JOIN chunk_groups g ON g.chunk_id = c.id
           WHERE c.active = 1"""
    ))
    if not rows:
        print("FAIL: no restricted chunks in the live index — nothing to test.")
        return 1

    print(f"restricted chunks on the active index: {len(rows)}\n")
    failures = 0

    for row in rows:
        offering = row["offering_id"]
        token = row["group_token"]
        # A term the chunk actually contains, so retrieval CAN find it when
        # permitted. Picking a word at random would test the tokenizer instead.
        words = [w for w in row["text"].split() if len(w) > 6][:3]
        if not words:
            print(f"  SKIP {row['usage_key']}: no distinctive term to query on")
            continue
        query = " ".join(words)

        print(f"chunk   : {row['display_name']!r}")
        print(f"  token : {token}")
        print(f"  query : {query[:60]!r}")

        without = store.search(query, tenant=settings.tenant, offering_id=offering, limit=20)
        with_tok = store.search(query, tenant=settings.tenant, offering_id=offering,
                                group_tokens=frozenset({token}), limit=20)

        hidden = row["usage_key"] not in {c.usage_key for c in without}
        shown = row["usage_key"] in {c.usage_key for c in with_tok}

        print(f"  caller WITHOUT the group: {len(without)} hit(s), "
              f"restricted chunk hidden = {hidden}")
        print(f"  caller WITH the group   : {len(with_tok)} hit(s), "
              f"restricted chunk shown  = {shown}")

        if hidden and shown:
            print("  => PASS: hidden from one caller, served to the other\n")
        else:
            failures += 1
            # Both halves matter. Only the first is security; the second is the
            # thing a blunt index-time filter would have broken -- a student who
            # paid still has to receive what they paid for.
            print("  => FAIL: " + (
                "restricted chunk leaked to a caller without the group"
                if not hidden else
                "entitled caller was denied content they should see") + "\n")

    print("=" * 60)
    print("RESULT:", "PASS" if failures == 0 else f"FAIL ({failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
