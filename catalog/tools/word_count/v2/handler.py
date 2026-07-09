"""Handler for word_count@v2.

v2 counts WORDS, not whitespace runs: alphanumeric tokens (with internal
apostrophes) are words, so hyphenated compounds count per component and
punctuation-only tokens don't count at all. characters/lines unchanged.
"""

import re

from schemas import WordCountIn, WordCountOut

from foundry.core.tool import RunContext

_WORD = re.compile(r"[A-Za-z0-9']+")


async def handle(inputs: WordCountIn, ctx: RunContext) -> WordCountOut:
    text = inputs.text
    return WordCountOut(
        words=len(_WORD.findall(text)),
        characters=len(text),
        lines=len(text.splitlines()) if text else 0,
    )
