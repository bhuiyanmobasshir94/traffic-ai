---
paths:
  - "*.py"
  - "pages/*.py"
---

# Dashboard source standards

Every rule here is mandatory. A "small" change is not an exception.

## Secrets and configuration

- **Never hardcode credentials, RTSP URLs, API keys, or tokens.** Streamlit's mechanism is
  `st.secrets`, backed by `.streamlit/secrets.toml`, which must stay gitignored. There is no
  other secret store in this project; do not invent a second one.
- **Never log or render a credential.** This app writes to a page that is typically being
  shown on a shared screen. Anything passed to `st.write`, `st.toast`, or a chat message is
  visible to whoever is watching the demo.

## Untrusted input

- **Guard every lookup on a user-driven value.** `st_folium` returns click payloads
  (`last_object_clicked_popup` and friends) reflecting what the user clicked, and they are
  `None` on first render. Use `.get()` with a fallback — an unguarded `MAPPER[...]` raises
  `KeyError` and blanks the page.
- **Treat `unsafe_allow_html=True` as an injection point.** The iframe HTML is assembled by
  string concatenation. Never interpolate a value originating from user input, a config file,
  a database, or an API response into that string without escaping it.
- **Validate anything read from a camera, model, or third-party API** before it reaches the
  UI — frame metadata, detected plate text, confidence scores. Detected text is
  attacker-influenceable: it is whatever was physically on the vehicle.

## State and the rerun model

- **Assume the script restarts from the top on every interaction.** Module-level variables and
  local counters do not survive; only `st.session_state` does.
- **Namespace session-state keys per page.** The existing pattern is `VIDEO_URL` on the toll
  page and `T_VIDEO_URL` on the analysis page. Two pages sharing a key clobber each other.
- **Initialize session-state keys before reading them.** The current code sets
  `st.session_state["VIDEO_URL"]` inside an `else` branch that does not render, so the video
  is blank until the second rerun. Do not copy that pattern — set defaults at the top.

## Blocking and long-running work

- **Never call `time.sleep()` on the script thread** in new code. It freezes the whole UI, not
  just one component. The existing generators do exactly this and are the pattern being
  replaced.
- **Camera ingest and inference run outside the script thread.** The UI reads the latest
  available result; it does not drive the capture loop. A dashboard that owns the loop cannot
  serve a second viewer and loses all state on any click.
- **Fail visibly, not silently.** There is no logging and no error handling anywhere in this
  repo today. New code that talks to a camera, a model, or a network service surfaces failures
  in the UI with a real message — never an empty container and never a bare `except: pass`.
- **Cache deliberately.** `@st.cache_resource` for connections and loaded models,
  `@st.cache_data` for derived data. A model reloaded on every rerun makes the app unusable.

## Duplication

- **Extract before you duplicate.** The first 155 lines are byte-identical across
  `Toll_Booth.py`, `pages/Traffic_Analysis.py`, and `Starter.py`; the map-render block appears
  five times and the iframe HTML six. A change to shared logic must be applied to every live
  copy or extracted to a module — a fix in one file is silently absent from the others.
- **Check which functions actually execute.** Each live file defines both page functions but
  calls only one. Verify against `main()` before editing.
