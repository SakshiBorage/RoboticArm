"""Agent definitions for the project.

factory_arm_agent is the Tier 2 escalation agent for the factory arm project. It never touches
the robot itself — it only ever returns a proposal (or "no proposal") for the calling system's
own Guard to independently re-verify before anything executes. Flow:

  1. Assess: can the fault even be confidently handled? (via propose_fix tool)
  2. Not handleable -> notify a human on Slack directly, stop.
  3. Handleable -> post the proposed fix to Slack and wait for a reply.
  4. Reply starts with "approve"/"yes" -> done, return the proposal.
  5. Anything else -> treated as a rejection with that text as feedback; retry once with it
     folded back into the next LLM call, then repeat from step 3.
  6. Still rejected after the retry -> final one-way escalation message, stop.

Notification is done with our own Slack tools (send_slack_message / wait_for_slack_reply), not
Aetherion's humanInput.request — this keeps everything grounded in a mechanism we've actually
tested end-to-end against a real Slack workspace, rather than an unconfirmed platform integration.
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Dict

from aetherion_sdk import agent, toolExecutor

# The only actions this agent may ever propose — must match what the calling system's
# ControllerAdapter actually implements. Kept here (not hardcoded in the tool) so it's easy to
# see/change without touching the LLM-calling code.
ALLOWED_OPS = {
    "clear_protective_stop": "Clear a protective stop / safety popup. Takes no params.",
    "reset_program_pointer": "Stop and restart the currently loaded program from the top. Takes no params.",
    "move_joints": "Move to specific joint angles (radians). Params: angles_rad (6 floats), a (acceleration), v (velocity).",
    "set_payload": "Set the active payload mass (kg) and optional center of gravity. Params: mass_kg, cog ([x,y,z] or null).",
    "emergency_stop": "Immediately halt whatever program is running. Takes no params.",
}

MAX_RETRIES = 1
REPLY_TIMEOUT = timedelta(hours=4)
NOTIFY_ONLY_TIMEOUT = timedelta(minutes=5)


async def _propose(context: dict, feedback: str = "") -> dict:
    return await toolExecutor.execute(
        "propose_fix",
        context,
        ALLOWED_OPS,
        feedback,
        start_to_close_timeout=timedelta(seconds=60),
    )


async def _notify(text: str) -> None:
    await toolExecutor.execute(
        "send_slack_message",
        text,
        "",
        start_to_close_timeout=timedelta(seconds=15),
    )


async def _notify_and_wait(text: str, timeout: timedelta) -> dict:
    """Post to Slack, then poll for the first human reply after it. Returns
    {"replied": bool, "text": str} — replied=False on timeout."""
    sent = await toolExecutor.execute(
        "send_slack_message", text, "", start_to_close_timeout=timedelta(seconds=15),
    )
    if not sent.get("ok"):
        return {"replied": False, "text": ""}
    return await toolExecutor.execute(
        "wait_for_slack_reply",
        sent["channel"],
        sent["ts"],
        int(timeout.total_seconds()),
        5,
        start_to_close_timeout=timeout + timedelta(seconds=30),
    )


def _is_approval(reply_text: str) -> bool:
    first_word = reply_text.strip().split()[0].lower() if reply_text.strip() else ""
    return first_word in ("approve", "approved", "yes", "y")


def _coerce_dict(value) -> dict:
    """The platform's manual-trigger UI serializes dict-typed fields as JSON strings, not
    native objects (confirmed live — 'fault'/'live_status' arrived as strings and broke
    fault.get(...)). Tolerate that, same defensive pattern as the reference project's
    _build_inputs_map."""
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return {}
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def _coerce_list(value) -> list:
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return []
    return value if isinstance(value, list) else []


@agent()
async def factory_arm_agent(payload: Dict[str, Any]) -> dict:
    fault = _coerce_dict(payload.get("fault"))
    context = {
        "fault_type": fault.get("fault_type"),
        "safetystatus": fault.get("safetystatus"),
        "robotmode": fault.get("robotmode"),
        "live_status": _coerce_dict(payload.get("live_status")),
        "fault_duration_seconds": payload.get("fault_duration_seconds"),
        "recent_history": _coerce_list(payload.get("recent_history")),
    }

    proposal = await _propose(context)

    if not proposal.get("can_handle"):
        await _notify(
            f"Fault {context['fault_type']} could not be confidently handled by the agent.\n"
            f"Reasoning: {proposal.get('reasoning', '')}"
        )
        return {"status": "escalated_unhandled", "proposal": proposal}

    attempt = 0
    while True:
        reply = await _notify_and_wait(
            f"Fault: {context['fault_type']}\n"
            f"Proposed action: {proposal.get('op')} {proposal.get('params')}\n"
            f"Reasoning: {proposal.get('reasoning')}\n"
            f"Confidence: {proposal.get('confidence')}\n"
            f"{proposal.get('pendant_message', '')}\n\n"
            f"Reply \"approve\" to run this, or anything else to reject (your reply is the feedback).",
            REPLY_TIMEOUT,
        )

        if not reply.get("replied"):
            return {"status": "timed_out", "proposal": proposal}

        if _is_approval(reply["text"]):
            return {"status": "approved", "proposal": proposal}

        feedback = reply["text"] or "rejected without a reason"
        attempt += 1

        if attempt > MAX_RETRIES:
            await _notify(
                f"Could not reach an agreed solution for {context['fault_type']} after "
                f"{attempt} attempt(s). Last rejection reason: {feedback}"
            )
            return {"status": "escalated_after_retry", "last_proposal": proposal, "feedback": feedback}

        proposal = await _propose(context, feedback)
