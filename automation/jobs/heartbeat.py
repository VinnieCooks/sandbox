"""Heartbeat: proves the scheduler fires; pings a dead-man's switch if configured.

A scheduler cannot tell you that it stopped running. Point HEARTBEAT_URL at a
check on a service such as healthchecks.io (free tier) with a period matching
this job's schedule; that service alerts you when the pings stop, which covers
GitHub dropping or disabling the schedule and a server going down.
"""

import os
import sys
import urllib.request
from datetime import datetime, timezone


def main() -> int:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    url = os.environ.get("HEARTBEAT_URL")
    if not url:
        print(f"alive at {now}; HEARTBEAT_URL not set, nothing to ping")
        return 0
    request = urllib.request.Request(url, headers={"User-Agent": "ops-heartbeat/1"})
    with urllib.request.urlopen(request, timeout=10) as response:
        print(f"alive at {now}; dead-man's switch answered HTTP {response.status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
