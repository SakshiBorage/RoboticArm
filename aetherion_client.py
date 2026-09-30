"""
Triggers the published factory_arm_agent on Aetherion sbox over its HTTP API —
the same two calls as the curl instructions (get a token, then /agent/run), so
the arm code doesn't need the Aetherion SDK (which only supports Python 3.12).

Credentials come from .env (AETHERION_*), never from this file. The platform
assigns each run its own run_id (any "id" we send is ignored), returned here so
the run can be looked up in the sbox UI.

Run directly to send one test fault:
    python3 aetherion_client.py
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from dotenv import load_dotenv

load_dotenv()

AGENT_NAME = "factory_arm_agent"
TIMEOUT_SECONDS = 15
# Platform run statuses that mean the run is still going; anything else is final.
IN_PROGRESS_STATUSES = {"PENDING", "RUNNING"}

_token_cache = {"token": None, "expires_at": 0.0}


def _env(name):
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is not set in .env")
    return value


def _base_headers():
    return {"X-Tenant-ID": _env("AETHERION_TENANT_ID"), "X-API-Key": _env("AETHERION_API_KEY")}


def _send(request):
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _post(url, body, headers):
    return _send(urllib.request.Request(url, data=body, headers=headers, method="POST"))


def _cached_token():
    """Reuses one token across the many status polls of a long Slack wait, refreshing
    a minute before it expires."""
    if _token_cache["token"] is None or time.time() > _token_cache["expires_at"]:
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": _env("AETHERION_CLIENT_ID"),
            "client_secret": _env("AETHERION_CLIENT_SECRET"),
        }).encode()
        headers = {**_base_headers(), "Content-Type": "application/x-www-form-urlencoded"}
        status, text = _post(f"{_env('AETHERION_BASE_URL')}/auth/token", body, headers)
        if status != 200:
            raise RuntimeError(f"token request failed ({status}): {text}")
        data = json.loads(text)
        _token_cache["token"] = data["access_token"]
        _token_cache["expires_at"] = time.time() + data.get("expires_in", 300) - 60
    return _token_cache["token"]


def _multipart(fields):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n')
    parts.append(f"--{boundary}--\r\n")
    return "".join(parts).encode(), f"multipart/form-data; boundary={boundary}"


def run_agent(agent_params):
    """Start one factory_arm_agent run on sbox (async — returns once it's started,
    not once the human has replied in Slack). Returns (run_id, status, response_text);
    run_id is None if the platform didn't start a run."""
    body, content_type = _multipart({
        "agent_name": AGENT_NAME,
        "run_in_sync": "false",
        "agent_params": json.dumps(agent_params),
    })
    headers = {**_base_headers(), "Authorization": f"Bearer {_cached_token()}", "Content-Type": content_type}
    status, text = _post(f"{_env('AETHERION_BASE_URL')}/api/v1/agent/run", body, headers)
    try:
        run_id = json.loads(text).get("run_id")
    except ValueError:
        run_id = None
    return run_id, status, text


def get_run(run_id):
    """Current state of one run: {"status": ..., "output_payload": ..., ...}."""
    headers = {**_base_headers(), "Authorization": f"Bearer {_cached_token()}"}
    url = f"{_env('AETHERION_BASE_URL')}/api/v1/agent/run/{urllib.parse.quote(run_id)}"
    status, text = _send(urllib.request.Request(url, headers=headers))
    if status != 200:
        raise RuntimeError(f"run status request failed ({status}): {text}")
    return json.loads(text)


def wait_for_run(run_id, timeout_seconds, poll_interval_seconds=5):
    """Poll until the run reaches a final status. Returns the final run dict, or None if
    timeout_seconds passes first. A failed poll is retried on the next interval — only a
    run that never finishes gives up, not one network hiccup."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            run = get_run(run_id)
            if run.get("status") not in IN_PROGRESS_STATUSES:
                return run
        except (OSError, RuntimeError, ValueError):
            pass
        time.sleep(poll_interval_seconds)
    return None


TEST_FAULT = {
    "fault": {
        "fault_type": "JOINT_OVER_TEMPERATURE",
        "safetystatus": "Safetystatus: JOINT_OVER_TEMPERATURE",
        "robotmode": "Robotmode: RUNNING",
    },
    "live_status": {"safetystatus": "Safetystatus: NORMAL", "robotmode": "Robotmode: RUNNING"},
    "fault_duration_seconds": 12,
    "recent_history": [],
}


if __name__ == "__main__":
    run_id, status, text = run_agent(TEST_FAULT)
    print(f"run id: {run_id}")
    print(f"HTTP {status}")
    print(text)
    sys.exit(0 if 200 <= status < 300 else 1)
