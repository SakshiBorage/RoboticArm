"""Append-only JSONL audit trail for every cycle the service runs."""
import json
import os
import time


class AuditLog:
    def __init__(self, path="audit_log.jsonl"):
        self.path = path

    def record(self, entry: dict):
        entry = {"timestamp": time.time(), **entry}
        with open(self.path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        return entry

    def read_recent(self, n=5, fault_type=None):
        """Last n audit entries, optionally filtered to a single fault_type — lets a
        proposer see whether this same fault was already tried and what happened."""
        if not os.path.exists(self.path):
            return []
        entries = []
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if fault_type:
            entries = [e for e in entries if e.get("fault_type") == fault_type]
        return entries[-n:]
