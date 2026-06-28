`scripts/trace.sh` orchestrates traces.

### End-to-End Workflow

1. **Environment Setup (`scripts/trace.sh:82:108`):** hermetic HOME.
2. **Prompt Preparation (`scripts/trace.sh:220:251`):** map prefetch.
3. **Transport Execution (`scripts/trace.sh:255:288`):** cli/acp/harness.

### Key Components

* `trace_state`: ```127:148:scripts/trace.sh``` status helper.
* `write_status`: ```193:206:scripts/trace.sh``` writes status.json.

## Overview

Purpose summary for trace.sh.
