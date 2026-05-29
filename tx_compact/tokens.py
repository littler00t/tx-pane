"""Token estimation — chars/4 for now.

Per design plan §4.4 step 2 and §12: keep `chars/4` until we have
evidence it's measurably wrong. Shimmed behind a single function so a
future swap to a real BPE tokenizer is a one-line change here.
"""

from __future__ import annotations


def estimate(text: str) -> int:
    """Approximate token count.

    chars/4 is the rule-of-thumb for English-ish text used across the
    Anthropic ecosystem and matches rtk's approximation. Not exact, but
    within ±15% for typical sysadmin output, which is good enough for
    budget enforcement.
    """
    return (len(text) + 3) // 4
