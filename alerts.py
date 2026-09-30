"""
Direct Slack alerts from the arm side — for things that must reach a person
without going through the Tier 2 agent: Tier 3 critical faults and preventive
early warnings. Same bot + channel as the Aetherion agent (SLACK_BOT_TOKEN /
SLACK_DEFAULT_CHANNEL in .env). Best-effort: a Slack outage is logged, never
raised, so it can't take down a cycle that already halted the arm.
"""
import json
import os
import urllib.request

from dotenv import load_dotenv

from app_logging import get_logger

load_dotenv()

logger = get_logger(__name__)


def send_slack(text) -> bool:
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    channel = os.environ.get("SLACK_DEFAULT_CHANNEL", "")
    if not token or not channel:
        logger.warning("Slack alert not sent: SLACK_BOT_TOKEN / SLACK_DEFAULT_CHANNEL not set in .env")
        return False
    request = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps({"channel": channel, "text": text}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode())
    except Exception:
        logger.exception("Slack alert failed")
        return False
    if not data.get("ok"):
        logger.warning(f"Slack alert rejected: {data.get('error')}")
        return False
    return True
