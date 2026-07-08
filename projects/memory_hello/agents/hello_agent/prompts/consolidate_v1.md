# user_facts consolidator — v1

You maintain a compact Markdown summary of everything learned about the
user in this conversation.

Current summary:

{current}

New conversation since the last update:

{recent_messages}

Rewrite the summary, merging in any new facts (name, preferences, plans,
goals). Drop nothing that is still true; correct anything contradicted.
Keep it under {max_size_tokens} tokens. Respond with ONLY the updated
Markdown summary — no preamble, no commentary.
