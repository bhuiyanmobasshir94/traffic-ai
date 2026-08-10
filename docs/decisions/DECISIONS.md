# Decision log

Append-only. Each entry: date, decision, why, and what it rules out. Rejected alternatives
matter as much as the choice — most wasted sessions are a rediscovery of an option that was
already ruled out for a reason nobody wrote down.

---

## 2024-02-23 — Streamlit multipage via the `pages/` directory convention

**Decision.** Navigation is Streamlit's filesystem convention: `Toll_Booth.py` is the
entrypoint and `pages/Traffic_Analysis.py` is auto-registered as the second nav entry.

**Why.** Replaced the earlier single-file design in `Starter.py`, which put both views behind
a `st.sidebar.radio("Services", ...)` selector. The radio approach re-rendered everything on
switch and could not deep-link to a view.

**Rules out.** The `st.navigation` / `st.Page` programmatic API, and any return to a
radio-driven single page. `Starter.py` is retained but dead; it is not a second entrypoint.
Reintroducing programmatic navigation means migrating both pages together, not one.

---

## 2024-02-23 — Camera feeds are YouTube iframes, as placeholders

**Decision.** Each page renders its "camera feed" as an autoplaying, muted, looping YouTube
`<iframe>` injected through `st.write(..., unsafe_allow_html=True)`.

**Why.** The demo needed continuous plausible footage with no infrastructure. YouTube provided
hosting, transcoding, and looping for free.

**Rules out.** Treating the current video path as the ingest path. It carries no frames into
Python — nothing can be run over it. Real ingest is a separate mechanism, not an extension of
this one. It also means the demo silently depends on YouTube availability and on the client's
network not blocking it.

---

## 2024-02-23 — Telemetry is simulated; no persistence layer chosen

**Decision.** Vehicle counts, number plates, and congestion flags are produced by in-process
generators using `random`. Nothing is stored: no database, no cache, no file writes.

**Why.** The dashboard was built to demonstrate the intended UI ahead of any inference
pipeline existing.

**Rules out.** Assuming any storage exists or that a schema has been agreed. There is no
prior art to match when persistence is added — that is an open decision, not a settled one.
It also means no historical query, no replay, and no cross-session continuity is possible
today.

---

## 2024-02-23 — UI layout is pinned to Streamlit internal selectors

**Decision.** Layout relies on hardcoded pixel container heights and CSS targeting Streamlit's
internal test IDs (`stSidebarNav`, `stSidebarNavSeparator`, `stAppViewBlockContainer`,
`toastContainer`). Dependencies are pinned at `streamlit==1.31.1`.

**Why.** Streamlit exposed no supported API for the density and chrome-hiding the demo needed.

**Rules out.** Treating a Streamlit upgrade as routine maintenance. These selectors are
unversioned internals with no compatibility guarantee, so a version bump is a UI change
requiring visual verification, and must be scoped as its own piece of work.

---

## 2026-08-10 — Orchestrator–worker onboarding; no verification commands recorded

**Decision.** Onboarded the repository with `CLAUDE.md`, one path-scoped rule file
(`.claude/rules/streamlit-app.md`), and this decision log. `.claude/continuity.json` records
the changelog and decision paths but deliberately omits `verifyCommands`.

**Why.** The repository has no tests, no linter, no formatter, and no CI. Listing a command
that does not exist would make the verification reminder fire against nothing and teach every
future session to report a check it never ran.

**Rules out.** Claiming any automated verification in this repo until a suite actually exists.
Until then, "verified" means the app was launched and looked at, and the report says so.
Dropped the template's `data-and-migrations` and `reliability-and-performance` rule files
entirely: their path globs matched zero files, as this project has no models, migrations,
SQL, jobs, queues, or request handlers.
