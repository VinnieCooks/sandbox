"""Starting point for a new job. Copy it, then register the copy in automation/jobs.yaml:

    - name: my-check
      run: python automation/jobs/my_check.py
      schedule: "*/30 13-20 * * 1-5"   # every 30 min, 13:00-20:59, weekdays
      notify: signal                  # only tell me when signal() is called
      secrets: [MY_API_KEY]           # repository secret / .env value, passed as env var

Contract with the runner:
  * exit 0 = success; any other exit code = failure (you are notified with the
    tail of the output); running past `timeout` = timeout (also notified)
  * signal(message) = "tell me this" even though the run succeeded
  * add third-party packages to automation/jobs/requirements.txt
"""

import os
import sys


def signal(message: str) -> None:
    """Queue a notification. Several calls are joined into one message."""
    path = os.environ.get("OPS_NOTIFY")
    if not path:                      # running by hand, outside ops
        print(f"[signal] {message}")
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(message.rstrip() + "\n")


def main() -> int:
    threshold = float(os.environ.get("THRESHOLD", "100"))
    value = 42.0                      # replace with a real measurement
    print(f"value={value} threshold={threshold}")
    if value > threshold:
        signal(f"value {value} crossed {threshold}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
