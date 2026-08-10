---
name: streamlit-testing
description: How to smoke-test a Streamlit page (including st.fragment and st_folium) without a running server, verified in traffic-ai's UI rewrite.
metadata:
  type: project
---

`streamlit.testing.v1.AppTest.from_file(path)` then `at.run(timeout=...)` executes a whole
page script headlessly and is a real check, not a guess — `at.exception` is an `ElementList`
of any uncaught exceptions raised during the run (empty means clean), and `at.error` /
`at.warning` / `at.title` etc. give the rendered element text for assertions.

Verified working in this repo with `st_folium` (a bidirectional custom component) and
`@st.fragment(run_every="2s")` present on the page — neither broke the harness. A full page
render (worker unreachable, real `httpx.Client` failing to connect to `http://worker:8000`)
completed in ~1.7s wall time; DNS resolution for an unreachable service hostname failed fast
in this sandbox rather than hanging to the `httpx` timeout, so `AppTest.run(timeout=15)` never
came close to timing out. If that stops being true in a different network sandbox, the fix is
lowering `Settings.api_timeout_seconds` for the test, not raising `AppTest`'s timeout.

Use `Path(__file__).resolve().parents[N] / "Toll_Booth.py"` to build the path passed to
`from_file` — relative paths are resolved against cwd, which breaks depending on where pytest
is invoked from.

See [[streamlit-fragment-cache]] for why `st.fragment`/`st.cache_resource` don't need this
harness to be importable at all — plain `python -c "import module"` already works.
