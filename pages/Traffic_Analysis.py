"""Entrypoint — traffic congestion / analysis view.

Auto-registered as the second nav entry by Streamlit's `pages/` convention.
Thin by design: see `traffic_ai.ui.dashboard.render_dashboard` for the actual
layout. Do not add a page function here — see `CLAUDE.md`.
"""

from __future__ import annotations

import streamlit as st

from traffic_ai.ui.dashboard import render_dashboard

st.set_page_config(
    page_title="Traffic Monitoring — Traffic Analysis",
    layout="wide",
    page_icon="🧊",
    initial_sidebar_state="auto",
)

render_dashboard(
    page_key="traffic_analysis",
    title="Traffic Analysis",
    default_camera_id="toll-plaza-b",
)
