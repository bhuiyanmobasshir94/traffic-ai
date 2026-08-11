---
name: anchor-files-can-change-between-resumed-sessions
description: Read-only contract/anchor files can be rewritten by another worker or the orchestrator between an interrupted session and its resume — re-Read them, don't trust carried-over context.
metadata:
  type: feedback
---

Mid-task on the `traffic-ai` worker-package slice, a session was interrupted (API session
limit) and resumed with "nothing you were asked to build reached disk, start from the
beginning." On resume, `src/traffic_ai/store.py` (read-only, owned by another worker) had
in fact been rewritten since the earlier read in the same conversation — dropped the
`orjson` import in favor of `model_dump_json()`, and changed `ping()`'s exception comment
from a `# noqa: BLE001` tag to a plain multi-line comment. Nothing else in the read-only
set (`domain.py`, `config.py`, `cameras.py`, `logging.py`, `tests/conftest.py`) had changed.

**Why:** the file changed while this task was paused; the version in context from before
the interruption was stale even though it came from a `Read` call earlier in the very same
conversation. Discovered by a `grep` for `noqa` in `store.py` matching zero results despite
the file having been read as containing one. See [[ruff-noqa-unused-directive]] for the
concrete lint failure this caused (a noqa comment copied from the stale read).

**How to apply:** after any resume (explicit "start from the beginning" instruction, a long
gap, or any signal the working tree may have moved under the task), re-`Read` every
read-only anchor file before writing code against it — do not rely on an earlier `Read` in
the same conversation. This is cheap insurance against building against a contract that
already moved.
