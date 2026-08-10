"""Entrypoint — toll-booth camera view.

Thin by design: layout and refresh live in `traffic_ai.ui.dashboard`. This
file only wires page config and picks the default camera. See `CLAUDE.md`
before adding a second page function here — the old file defined two and
called one, leaving a dead, diverged copy.
"""

from __future__ import annotations

import streamlit as st

from traffic_ai.ui.dashboard import render_dashboard

st.set_page_config(
    page_title="Traffic Monitoring — Toll Booth",
    layout="wide",
    page_icon="🧊",
    initial_sidebar_state="auto",
)

render_dashboard(
    page_key="toll_booth",
    title="Toll Booth",
    default_camera_id="toll-plaza-a",
)
