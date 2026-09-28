"""
Per-run log storage, kept for later reference. Each demo/service run gets its
own folder under logs/ (git-ignored, never cleaned up automatically):

  logs/2026-09-28_101500_demo/
    service.log   full detail for this run only, including tracebacks
    audit.jsonl   one line per fault cycle (tier, proposal, gates, decision,
                  Aetherion run id for Tier 2)
    events.jsonl  everything else worth keeping: scenario steps, monitor
                  events, preventive warnings, Slack alerts, human actions

The cumulative audit_log.jsonl in the project root is still written too — the
Tier 2 agent reads its recent history across runs.
"""
import json
import os
import threading
import time

from app_logging import add_log_file

LOGS_ROOT = "logs"


class RunLog:
    def __init__(self, label, root=LOGS_ROOT):
        self.dir = os.path.join(root, f"{time.strftime('%Y-%m-%d_%H%M%S')}_{label}")
        os.makedirs(self.dir, exist_ok=True)
        self.audit_path = os.path.join(self.dir, "audit.jsonl")
        self._events_path = os.path.join(self.dir, "events.jsonl")
        self._lock = threading.Lock()  # the preventive monitor writes from its own thread
        add_log_file(os.path.join(self.dir, "service.log"))

    def event(self, kind, **data):
        entry = {"timestamp": time.time(), "time": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, **data}
        with self._lock, open(self._events_path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        return entry
