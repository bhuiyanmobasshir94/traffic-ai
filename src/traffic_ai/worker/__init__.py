"""Inference worker: video decode, detection, tracking, counting, annotation.

Everything in this package runs off the API's request thread. `pipeline.py` is the
composition root — it wires a `Detector`, a `VehicleTracker`, a `LineCounter`, and a
`FrameAnnotator` around one camera's video loop and publishes results to the
Redis-backed `StateStore` (`traffic_ai.store`).
"""

from __future__ import annotations
