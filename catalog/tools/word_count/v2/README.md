# word_count@v2

Counts words, characters, and lines in a text.

**v2 change:** words are alphanumeric tokens (`[A-Za-z0-9']+`), not
whitespace runs — `state-of-the-art` is 4 words, `!!!` is 0. `characters`
and `lines` are unchanged from v1.

Pure transformation tool: no connections, no LLM. Standalone eval in
`eval.yaml` (also the shared spec for `foundry eval compare --tool
word_count v1 v2`).
