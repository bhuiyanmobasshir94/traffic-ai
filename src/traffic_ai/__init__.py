"""traffic-ai — toll-booth and traffic congestion monitoring.

Two services share this package:

- `traffic_ai.worker`  — decodes demo footage, detects and tracks vehicles, counts
  line crossings, and publishes state and annotated frames.
- `traffic_ai.ui`      — the Streamlit dashboard, a thin viewer over that state.

`domain.py` is the contract between them; nothing else crosses the boundary.
"""

__version__ = "1.0.0"
