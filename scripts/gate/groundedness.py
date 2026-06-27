#!/usr/bin/env python3
"""Overconfidence / groundedness breaker (unifable).

Thin facade: the implementation lives in focused sub-modules and is re-exported
here so existing callers (`import groundedness as gb; gb.X` / `from groundedness
import X`) keep working unchanged. New production code should import from the real
module instead:

  - breaker_filters        claim text filters (harness self-ref, task board, skills)
  - breaker_prompts        judge system prompts + JSON schemas
  - breaker_runtime        tool-class constants, transcript assembly, state, messages
  - breaker_judges         arm/disarm/monitor judges + predicate self-verify
  - breaker_orchestration  evaluate_pre_tool / evaluate_post_tool_release entrypoints

Two directional decisions, both by GPT-realtime-2 over merged transcript material
(host JSONL tail + prior breaker-event records + optional fresh PostToolUse output):

ARM (while disarmed). On PreToolUse the strict judge asks two questions from the
transcript: (1) did the model say something CONFIDENTLY WITHOUT BACKING IT UP, and
(2) is that assertion LOAD-BEARING for the work currently in progress? Only when
both hold (verdict 1) does the breaker arm and block mutation tools. On the same
call it also returns a minimal next-step DIRECTIVE and a TOOL_SCOPE (the stepwise
director), persisted to breaker state and enforced by tool_scope.in_scope.

DISARM (while armed). On PostToolUse after a read-only tool, and on any PreToolUse
while still armed, a claim-bound release judge asks whether the flagged claim is
grounded, retracted, or no longer load-bearing. If so, the breaker disarms.

PROVISIONAL LIFT lets the model keep mutating within a scope while it pursues the
steered verification; a monitor judge re-arms only on egregious drift. SAFETY CAP:
after BREAKER_MAX_BLOCKS consecutive blocks the breaker fails open.

Fails open: any judge or transcript error leaves tools unblocked.
Always on (no env disable). Cap override: UNIFABLE_BREAKER_MAX_BLOCKS.
"""

from __future__ import annotations

try:  # bare import when scripts/gate is on sys.path (hooks + tests); package import otherwise
    from breaker_filters import (  # noqa: F401
        _HARNESS_SELF_REF_RE,
        _HYPOTHESIS_PHRASE_RE,
        _QUOTED_VALUE_RE,
        _READ_TOOL_USE_RE,
        _REPO_PATH_IN_TEXT_RE,
        _SKILL_TOOL_USE_RE,
        _SPEC_BOARD_BEGIN,
        _SPEC_BOARD_END,
        _SPEC_BOARD_MAX,
        _TASK_BOARD_STATUS_CLAIM_RE,
        _TASK_ID_RE,
        _USER_GOAL_MAX,
        _claim_supported_by_spec_board,
        _extract_spec_board,
        _imminent_read_target,
        _norm_repo_path,
        _path_targets_match,
        _segment_plans_read,
        _task_ids_in_text,
        claim_describes_loaded_skill,
        is_harness_self_referential,
        is_task_board_status_claim,
        loaded_skill_names,
        paths_in_text,
        should_suppress_path_hypothesis_arm,
    )
    from breaker_judges import (  # noqa: F401
        _VERIFY_MAX_BYTES,
        _VERIFY_MAX_ENTRIES,
        JudgeFn,
        ReleaseVerdict,
        _default_judge,
        _parse_director_fields,
        _verify_read,
        arm_judge,
        disarm_judge,
        judge_segment,
        monitor_provisional_judge,
        verify_claim_predicate,
    )
    from breaker_orchestration import (  # noqa: F401
        _dispatch_auto_verify,
        _poll_auto_verify,
        evaluate,
        evaluate_post_tool_release,
        evaluate_pre_tool,
        evaluate_pre_tool_locked,
    )
    from breaker_prompts import (  # noqa: F401
        _DISARM_SCHEMA,
        _DISARM_SYSTEM,
        _JUDGE_SCHEMA,
        _JUDGE_SYSTEM,
        _MONITOR_SCHEMA,
        _MONITOR_SYSTEM,
        _SCOPE_HINT_PREFIX,
        _research_bash_whitelist_summary,
        _steering_description,
    )
    from breaker_runtime import (  # noqa: F401
        _TRANSCRIPT_TOKEN_BUDGET,
        AUTO_VERIFY_WINDOW_SECONDS,
        BREAKER_MAX_BLOCKS_DEFAULT,
        DIRECTIVE_MAX_CHARS,
        JUDGE_COALESCE_WINDOW_SECONDS,
        JUDGE_WINDOW_SECONDS,
        MUTATION_TOOLS,
        RELEASE_TOOLS,
        _apply_release,
        _disarm_message,
        _encode_cwd,
        _fail_open_message,
        _needed_message,
        _provisional_lift_message,
        _release_log,
        _spec_board_block,
        _stale_arm_message,
        _user_goal_block,
        _verify_confirmed_message,
        _verify_disarm_digest,
        _verify_dispatched_message,
        _verify_failed_message,
        arm,
        auto_verify_in_progress,
        breaker_key,
        clear_auto_verify,
        disarm,
        is_mutation_tool,
        is_release_tool,
        judge_transcript,
        locate_transcript,
        max_blocks,
        record_verdict,
        should_coalesce,
        should_judge,
        transcript_segment,
    )
    from breaker_state import adjudicated_claims  # noqa: F401
except ImportError:  # pragma: no cover  (package-relative import path)
    from scripts.gate.breaker_filters import (  # noqa: F401
        _HARNESS_SELF_REF_RE,
        _HYPOTHESIS_PHRASE_RE,
        _QUOTED_VALUE_RE,
        _READ_TOOL_USE_RE,
        _REPO_PATH_IN_TEXT_RE,
        _SKILL_TOOL_USE_RE,
        _SPEC_BOARD_BEGIN,
        _SPEC_BOARD_END,
        _SPEC_BOARD_MAX,
        _TASK_BOARD_STATUS_CLAIM_RE,
        _TASK_ID_RE,
        _USER_GOAL_MAX,
        _claim_supported_by_spec_board,
        _extract_spec_board,
        _imminent_read_target,
        _norm_repo_path,
        _path_targets_match,
        _segment_plans_read,
        _task_ids_in_text,
        claim_describes_loaded_skill,
        is_harness_self_referential,
        is_task_board_status_claim,
        loaded_skill_names,
        paths_in_text,
        should_suppress_path_hypothesis_arm,
    )
    from scripts.gate.breaker_judges import (  # noqa: F401
        _VERIFY_MAX_BYTES,
        _VERIFY_MAX_ENTRIES,
        JudgeFn,
        ReleaseVerdict,
        _default_judge,
        _parse_director_fields,
        _verify_read,
        arm_judge,
        disarm_judge,
        judge_segment,
        monitor_provisional_judge,
        verify_claim_predicate,
    )
    from scripts.gate.breaker_orchestration import (  # noqa: F401
        _dispatch_auto_verify,
        _poll_auto_verify,
        evaluate,
        evaluate_post_tool_release,
        evaluate_pre_tool,
        evaluate_pre_tool_locked,
    )
    from scripts.gate.breaker_prompts import (  # noqa: F401
        _DISARM_SCHEMA,
        _DISARM_SYSTEM,
        _JUDGE_SCHEMA,
        _JUDGE_SYSTEM,
        _MONITOR_SCHEMA,
        _MONITOR_SYSTEM,
        _SCOPE_HINT_PREFIX,
        _research_bash_whitelist_summary,
        _steering_description,
    )
    from scripts.gate.breaker_runtime import (  # noqa: F401
        _TRANSCRIPT_TOKEN_BUDGET,
        AUTO_VERIFY_WINDOW_SECONDS,
        BREAKER_MAX_BLOCKS_DEFAULT,
        DIRECTIVE_MAX_CHARS,
        JUDGE_COALESCE_WINDOW_SECONDS,
        JUDGE_WINDOW_SECONDS,
        MUTATION_TOOLS,
        RELEASE_TOOLS,
        _apply_release,
        _disarm_message,
        _encode_cwd,
        _fail_open_message,
        _needed_message,
        _provisional_lift_message,
        _release_log,
        _spec_board_block,
        _stale_arm_message,
        _user_goal_block,
        _verify_confirmed_message,
        _verify_disarm_digest,
        _verify_dispatched_message,
        _verify_failed_message,
        arm,
        auto_verify_in_progress,
        breaker_key,
        clear_auto_verify,
        disarm,
        is_mutation_tool,
        is_release_tool,
        judge_transcript,
        locate_transcript,
        max_blocks,
        record_verdict,
        should_coalesce,
        should_judge,
        transcript_segment,
    )
    from scripts.gate.breaker_state import adjudicated_claims  # noqa: F401
