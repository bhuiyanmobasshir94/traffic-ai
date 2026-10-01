---
name: streamlit-apptest-and-httpx-token-traps
description: Verified traps from the UI slice - AppTest has no typed chart accessor and a quirky session_state API, and httpx/h11 quotes a malformed Authorization header (token included) in LocalProtocolError.
metadata:
  type: project
---

Verified 2026-10-01 against streamlit 1.64.0 and httpx 0.28.1 in this repo's `.venv`.

- **AppTest charts**: `st.bar_chart` and `st.line_chart` both appear as `UnknownElement` of type `vega_lite_chart`. Count them with `at.get("vega_lite_chart")`; there is no `at.bar_chart`, and `arrow_bar_chart` matches nothing.
- **AppTest session state**: `at.session_state` has `keys/items/values/to_dict/get`, but no `filtered_state` attribute. `set(at.session_state.to_dict())` gives the user-visible keys, widget-backed ones included.
- **Page tests**: monkeypatch `traffic_ai.ui.analytics.get_worker_client` to return a real `WorkerClient` over `httpx.MockTransport`. The AppTest script runs in-process, so the patch holds and the real header/status/parse code is exercised.
- **Token leak path**: a token containing a newline or control character makes h11 raise `LocalProtocolError: Illegal header value b'Bearer <token>...'` at send time, and `WorkerUnavailable(f"...{exc}")` would then carry the token. `ui/client.py::_checked_token` rejects such a token at construction with a fixed message. Keep that check if the header-building code changes.
- **Ruff in tests**: `S105`/`S106` fire on any `TOKEN = "..."` or `token="..."` literal in tests. `# noqa: S105` is valid here because `S` is selected, so it does not trip RUF100.

**Why:** each cost a failed test run or would have shipped a credential leak.
**How to apply:** reuse the monkeypatch pattern for any new page test; re-check the AppTest element names if streamlit is bumped.
