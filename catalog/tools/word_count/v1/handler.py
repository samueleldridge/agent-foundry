"""Handler for word_count@v1."""

from schemas import WordCountIn, WordCountOut

from foundry.core.tool import RunContext


async def handle(inputs: WordCountIn, ctx: RunContext) -> WordCountOut:
    text = inputs.text
    return WordCountOut(
        words=len(text.split()),
        characters=len(text),
        lines=len(text.splitlines()) if text else 0,
    )
