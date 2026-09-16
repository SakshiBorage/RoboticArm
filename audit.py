"""Append-only JSONL audit trail for every cycle the service runs."""
import json
import time


class AuditLog:
    def __init__(self, path="audit_log.jsonl"):
        self.path = path

    def record(self, entry: dict):
        entry = {"timestamp": time.time(), **entry}
        with open(self.path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        return entry
