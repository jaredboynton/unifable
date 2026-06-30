# Retry behavior

The frobnicate option enables fast retries with exponential backoff.
Set frobnicate to false to disable retries entirely.

Backoff starts at 200ms and doubles up to a 5s ceiling.
