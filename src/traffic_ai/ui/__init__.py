"""traffic_ai.ui — the Streamlit dashboard, a thin viewer over worker state.

Talks to `traffic_ai.worker` over HTTP only (`client.py`); never imports the
worker or API packages directly. `components.py` holds every piece of
rendering logic used by more than one page, so it exists exactly once instead
of being copy-pasted per page as in the original demo.
"""

from __future__ import annotations
