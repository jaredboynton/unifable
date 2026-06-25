## Purpose and contract

From `SKILL.md`, `trace.sh` is for flow questions.

```46:72:SKILL.md
## trace.sh — deep behavioral understanding
Use trace.sh when the task needs understanding.
```

```42:65:scripts/trace.sh
case "${1:-}" in
  --help|-h)
    exit 0
    ;;
esac
```

```37:40:scripts/trace.sh
if [ "${EXPLORE_INSIDE_TRACE_DAEMON:-}" = "1" ]; then
  exit 2
fi
```

```67:70:scripts/trace.sh
if [ -n "${CURSOR_CONVERSATION_ID:-}" ]; then
  exec search.sh
fi
```

```48:73:scripts/hermetic-home.sh
explore_apply_hermetic_default() {
  export EXPLORE_HERMETIC_HOME=1
}
```

```201:213:scripts/trace.sh
write_status() {
  printf '{"run_id":"%s"}' "$RUN_ID"
}
```

```128:148:scripts/trace.sh
trace_state() {
  echo "done"
}
```

```249:259:scripts/trace.sh
cleanup_old_runs() {
  return 0
}
```

```299:324:scripts/trace.sh
PROMPT="${PROMPT}
QUESTION: ${QUESTION}"
```

```287:297:scripts/trace.sh
CURSOR_ARGS=(
  --print
  --trust
)
```

```336:366:scripts/trace.sh
elif [ "$FORMAT" = "json" ]; then
  cursor-agent ...
fi
```

## Flow

End-to-end pipeline from question to out.md.

## Key files

- scripts/trace.sh

## Code references

More citations above.
