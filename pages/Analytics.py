"""Entrypoint — historical analytics view.

Auto-registered as a nav entry by Streamlit's `pages/` convention. Thin by
design: see `traffic_ai.ui.analytics.render_analytics` for the layout and the
states it distinguishes. Do not add a page function here — see `CLAUDE.md`.
"""

from __future__ import annotations

import streamlit as st

from traffic_ai.ui.analytics import render_analytics

st.set_page_config(
    page_title="Traffic Monitoring — Analytics",
    layout="wide",
    page_icon="🧊",
    initial_sidebar_state="auto",
)

render_analytics(page_key="analytics", title="Analytics")
