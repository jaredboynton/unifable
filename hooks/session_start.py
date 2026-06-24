#!/usr/bin/env python3
"""SessionStart hook: refresh the stable ~/.unifable runtime from the plugin cache.

The deterministic upgrade trigger. On every session start, sync the newest cached plugin
version into ~/.unifable and atomically flip ~/.unifable/current, so hooks never exec from
a versioned cache dir the host marketplace may delete (the exit-127 dangle bug). This is a
hook, not a skill, precisely because it MUST run without the model's involvement.

Emits {} and never blocks: a sync failure must not stop a session from starting.
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(here, "..", "scripts", "gate"))
    try:
        import runtime_sync

        runtime_sync.sync_runtime()
    except Exception:
        pass
    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
