---
name: counting-switch-and-test-recipes
description: How the per-camera counting_enabled switch is wired (registry + published state, fail-closed) and the test recipes/traps found building it - verified 2026-10-01
metadata:
  type: project
---

Verified in the production-hardening worktree, 2026-10-01.

- **Switch wiring.** `CameraConfig.counting_enabled` / `counting_disabled_reason` (cameras.py) and the additive
  `CameraState.counting_enabled` (domain.py). In `CameraPipeline._process_frame` the *absence* of `_line_counter`
  is the off switch: it is only built when the camera counts, and the crossing/throughput/congestion block is
  guarded by `if self._line_counter is not None`. The disabled branch omits `counts/throughput_per_min/congestion`
  from the `CameraState(**counted)` kwargs so the model defaults apply.
- **UI decision point is `components.counting_disabled_reason(camera_id, state=None)`**: off if the registry OR the
  published state says off (fail closed), generic reason when only the state says so. Dashboard, analytics and the
  map colour (`_corridor_color`) all go through it; do not re-derive the decision elsewhere.
- **API gate** (`api/dependencies.py::get_counting_camera_or_409`, shared reason default
  `cameras.DEFAULT_COUNTING_DISABLED_REASON`): 409 with the camera's reason on `/cameras/{id}/events` and on
  `/history/*` when `camera_id` names it; `/events` skips disabled cameras at read time (not post-filter, so `limit`
  is not wasted). Verified FastAPI order: a dependency's HTTPException fires before any later dependency's or the
  endpoint's own validation errors, so 404 -> 409 -> 422 (window/limit/unparseable date) -> 503 falls out of
  declaring `camera_id` first; `tests/api/test_counting_gate.py` pins it. History aggregates with NO `camera_id`
  are not gated (SQL sums; needs a repository change).
- **UI client** maps any 409 in `WorkerClient._request` to `CountingNotCalibrated` (fixed message, never the response
  body). Dashboard live region and analytics `fetch_history` both handle it; without it a 409 read as "worker
  unreachable".
- **Pytest basenames must be unique across `tests/`**: `tests/api` and `tests/ui` have no `__init__.py`, so a new
  `tests/test_cameras.py` collides with `tests/api/test_cameras.py` ("import file mismatch"). A targeted run that
  skips `tests/api` hides it - always finish with the full suite.
- **Map colour test recipe**: monkeypatch `components.st_folium` to capture the `folium.Map`, then read
  `fmap._children`: `Marker.location` + `Marker.icon.options["marker_color"]`; `PolyLine.locations` +
  `PolyLine.options["color"]`. No Streamlit script context needed.
- **Dashboard AppTest recipe**: `Traffic_Analysis.py` defaults to camera B, `Toll_Booth.py` to camera A; patch
  `dashboard.get_worker_client` with a real `WorkerClient` over `httpx.MockTransport` serving `/api/healthz`,
  `/api/cameras`, `/api/cameras/{id}/state`, `/api/cameras/{id}/events`. The `@st.fragment` region renders in
  AppTest; selected camera is `at.session_state["<page_key>:selected_camera_id"]`.
- **Bash heredocs (`cat >> file <<EOF`) are refused** by the worktree guard even for plain appends; use the Edit
  tool anchored on the last test.
- **Mutation check**: 14 hand-written mutants (script in /tmp, not kept) were all killed; a mutant that breaks
  module-level `next(c for c in CAMERAS if not c.counting_enabled)` kills by collection error, no FAILED line.
