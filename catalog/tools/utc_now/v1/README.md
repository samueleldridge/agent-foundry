# utc_now

Return the current UTC time as ISO 8601 + unix milliseconds.

## When to use
- Timestamping agent output; "what time is it" style grounding.

## When NOT to use
- Timezone-aware local time (compose with a converter, or fetch from a
  time API via `http_get_json`).

## Connections required
None.
