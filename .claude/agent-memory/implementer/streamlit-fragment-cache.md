---
name: streamlit-fragment-cache
description: st.fragment(run_every=...) and st.cache_resource both execute fine when a module is bare-imported (python -c "import module"), not just under `streamlit run`.
metadata:
  type: project
---

Verified in traffic-ai (streamlit 1.61.1): a module-level `st.set_page_config(...)` call plus
a call into a function wrapped in `@st.fragment(run_every="2s")`, and a separate
`@st.cache_resource`-decorated factory, all execute without raising when the file is imported
via plain `python -c "import Toll_Booth"` (no real `streamlit run` context). Streamlit prints
"missing ScriptRunContext! This warning can be ignored when running in bare mode." to stderr
for every widget call — that is noise, not a failure signal. `st.session_state` writes also
succeed in bare mode in this version (a "Session state does not function..." message is
logged but the assignment does not raise).

This means "does this page import without an ImportError" is a real, cheap check worth
running as its own done-means step before reaching for [[streamlit-testing]]'s heavier
`AppTest` harness.

Practical implication for `@st.fragment`: arguments passed to a fragment-decorated function
are captured from the calling script's most recent full run and reused on fragment-only
reruns (per Streamlit's own docs) — a fragment fed by `st.session_state` reads at call time,
not at every 2s tick, so a fragment call site should pull fresh values *before* the call
rather than caching them across fragment ticks.
