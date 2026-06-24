#!/usr/bin/env python3
"""SessionStart hook: refresh the stable ~/.unifable runtime and inject the
standing operating-mode context.

Two jobs, both fail-open (a hook bug must never stop a session from starting):

1. Runtime sync: copy the newest cached plugin version into ~/.unifable and
   atomically flip ~/.unifable/current, so hooks never exec from a versioned
   cache dir the host marketplace may delete (the exit-127 dangle bug).
2. Context injection: emit the operating-mode block via SessionStart
   additionalContext. This replaces the old static CLAUDE.md/AGENTS.md block
   injection -- the posture now ships only when the plugin is enabled, and is
   not duplicated into host memory files that other CLI tools also read.

Emits {} on any internal error; never blocks.
"""
from __future__ import annotations

import json
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

    payload: dict = {}
    try:
        from context_block import build_session_payload

        payload = build_session_payload()
    except Exception:
        payload = {}

    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
