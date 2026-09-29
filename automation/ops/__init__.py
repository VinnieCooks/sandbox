"""ops: one job registry, several engines, one notification layer.

jobs.yaml is the single source of truth. Engines that execute it:
  * github  - GitHub Actions cron (generated workflow)
  * server  - `python -m ops serve` on an always-on machine
Claude Cloud Routines run on Anthropic's side; jobs can start them via `routine:`.
"""

__version__ = "0.1.0"
