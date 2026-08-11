# Demo footage attribution

The two demo clips fetched by `scripts/fetch_demo_videos.py` are sample assets
from Roboflow's open-source `supervision` project, used here as placeholder
toll-plaza footage for a CPU inference demo. This file records their source;
it does not assert a licence beyond what is stated at the link below —
consult that project directly before any use beyond this internal demo.

| File | Source project | URL |
| --- | --- | --- |
| `toll-plaza-a.mp4` | Roboflow `supervision` — video examples | https://media.roboflow.com/supervision/video-examples/vehicles.mp4 |
| `toll-plaza-b.mp4` | Roboflow `supervision` — video examples | https://media.roboflow.com/supervision/video-examples/vehicles-2.mp4 |

Project: https://github.com/roboflow/supervision

This record was written 2026-08-10, documenting the source URLs and expected
checksums built into `scripts/fetch_demo_videos.py`; it does not itself
certify that a download has occurred on this machine. The fetch script
prints the actual retrieval date to stderr each time it downloads a file —
check the deployment log for when the files present under `data/videos/`
were actually pulled. If the source URLs, checksums, or licence terms
change, update this file and the script together.
