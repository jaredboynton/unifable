#!/usr/bin/env python3
"""Canonical Realtime transport for the gpt-realtime-2 judge.

``realtime_ws`` owns the host-agnostic RFC 6455 WebSocket client, frame
encode/decode, the Codex OAuth token lifecycle, and the connection lifecycle.
``realtime_session`` owns the pure structured/batch session helpers (response
routing, reask classification, reasoning config). The gate adapter
``scripts/gate/codex_judge.py`` composes both and keeps the public
``ask_structured`` / ``ask_structured_batch`` API stable.
"""

from __future__ import annotations
