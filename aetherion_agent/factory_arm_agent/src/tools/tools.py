"""Tool definitions for the project.

propose_fix is the only real I/O here: a direct OpenAI call (same pattern already proven in the
main factory-arm-project's proposer.py AgentProposer), wrapped as an @tool() since agent code
itself must stay deterministic — all real work happens here, not in agent.py.
"""
from __future__ import annotations

import json
import os

from aetherion_sdk import tool

SYSTEM_INSTRUCTIONS = """You are the Tier 2 escalation agent for a factory robot arm's safety-gated recovery system.

A fault occurred that isn't on the Tier 1 auto-recovery whitelist. A human will review whatever you propose before anything runs on the real robot — you never execute anything yourself.

First decide: can you confidently handle this at all? If not, set can_handle=false and explain why in reasoning; leave op/params empty.

If you can handle it, propose exactly ONE action from this catalog:
{catalog}

You may be given how long the fault has been ongoing, recent history of past attempts for this same fault type, and (on a retry) feedback from a human who rejected your previous proposal — use that feedback, don't just repeat the same proposal.

Only fill in params that apply to the op you chose; leave every other param field null. Keep reasoning to one or two sentences a human can quickly judge. pendant_message must be plain language, no jargon, for someone watching the robot with zero other context."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "can_handle": {"type": "boolean"},
        "confidence": {"type": "number"},
        "op": {"type": "string"},
        "params": {
            "type": "object",
            "properties": {
                "angles_rad": {"type": ["array", "null"], "items": {"type": "number"}},
                "a": {"type": ["number", "null"]},
                "v": {"type": ["number", "null"]},
                "mass_kg": {"type": ["number", "null"]},
                "cog": {"type": ["array", "null"], "items": {"type": "number"}},
            },
            "required": ["angles_rad", "a", "v", "mass_kg", "cog"],
            "additionalProperties": False,
        },
        "reasoning": {"type": "string"},
        "severity": {"type": "string"},
        "pendant_message": {"type": "string"},
        "pattern_flag": {"type": "string"},
        "outcome_preview": {"type": "string"},
    },
    "required": ["can_handle", "confidence", "op", "params", "reasoning",
                 "severity", "pendant_message", "pattern_flag", "outcome_preview"],
    "additionalProperties": False,
}

_OP_PARAM_KEYS = {
    "move_joints": ("angles_rad", "a", "v"),
    "set_payload": ("mass_kg", "cog"),
}


def _clean_params(op: str, raw_params: dict) -> dict:
    keys = _OP_PARAM_KEYS.get(op, ())
    return {k: raw_params[k] for k in keys if raw_params.get(k) is not None}


@tool()
async def propose_fix(context: dict, allowed_ops: dict, feedback: str = "") -> dict:
    """Call OpenAI to assess a fault and, if handleable, propose one action.

    context: {fault_type, safetystatus, robotmode, live_status, fault_duration_seconds, recent_history}
    allowed_ops: {op_name: description} — the only actions the model may choose from
    feedback: a prior rejection reason, on a retry attempt (empty on the first call)
    """
    try:
        from openai import OpenAI
        client = OpenAI()
        model = os.environ.get("OPENAI_MODEL", "gpt-4.1")

        user_input = (
            f"Fault detected:\n"
            f"  fault_type: {context.get('fault_type')}\n"
            f"  safetystatus (at detection): {context.get('safetystatus')}\n"
            f"  robotmode (at detection): {context.get('robotmode')}\n"
        )
        live_status = context.get("live_status") or {}
        if live_status:
            user_input += (
                f"\nCurrent live status (just re-checked):\n"
                f"  safetystatus: {live_status.get('safetystatus')}\n"
                f"  robotmode: {live_status.get('robotmode')}\n"
            )
        duration = context.get("fault_duration_seconds")
        if duration is not None:
            user_input += f"\nThis fault has been ongoing for {duration:.1f} seconds (not yet resolved).\n"
        history = context.get("recent_history") or []
        if history:
            user_input += "\nRecent history for this same fault_type (most recent last):\n"
            for entry in history:
                user_input += f"  - {json.dumps(entry)}\n"
        if feedback:
            user_input += (
                f"\nA human REJECTED your previous proposal for this fault with this feedback:\n"
                f"  {feedback}\n"
                f"Take this into account — do not just repeat the same proposal.\n"
            )

        response = client.responses.create(
            model=model,
            instructions=SYSTEM_INSTRUCTIONS.format(
                catalog="\n".join(f"- {op}: {desc}" for op, desc in allowed_ops.items())
            ),
            input=user_input,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "tier2_assessment",
                    "schema": RESPONSE_SCHEMA,
                    "strict": True,
                }
            },
        )

        parsed = json.loads(response.output_text)
        parsed["params"] = _clean_params(parsed.get("op", ""), parsed.get("params") or {})
        return parsed
    except Exception as e:
        return {
            "can_handle": False,
            "confidence": 0.0,
            "op": "",
            "params": {},
            "reasoning": f"propose_fix error: {e}",
            "severity": "unknown",
            "pendant_message": "",
            "pattern_flag": "",
            "outcome_preview": "",
        }


SLACK_API = "https://slack.com/api"


def _slack_headers() -> dict:
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}


@tool()
async def send_slack_message(text: str, channel: str = "") -> dict:
    """Post a message to Slack. Returns {ok, channel, ts, error} — channel+ts identify this
    message so a later poll can find replies that came after it."""
    import httpx

    channel = channel or os.environ.get("SLACK_DEFAULT_CHANNEL", "")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{SLACK_API}/chat.postMessage",
                headers=_slack_headers(),
                json={"channel": channel, "text": text},
            )
        data = resp.json()
        return {"ok": data.get("ok", False), "channel": data.get("channel"),
                "ts": data.get("ts"), "error": data.get("error")}
    except Exception as e:
        return {"ok": False, "channel": channel, "ts": None, "error": str(e)}


@tool()
async def wait_for_slack_reply(channel: str, after_ts: str, timeout_seconds: int = 14400,
                                poll_interval_seconds: int = 5) -> dict:
    """Poll the channel for the first human reply sent after after_ts (our own message's
    timestamp). Returns {"replied": bool, "text": str}. Never raises — a Slack API hiccup just
    means one skipped poll, not a failed wait."""
    import asyncio
    import time

    import httpx

    deadline = time.monotonic() + timeout_seconds
    async with httpx.AsyncClient(timeout=10) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(
                    f"{SLACK_API}/conversations.history",
                    headers=_slack_headers(),
                    params={"channel": channel, "oldest": after_ts, "limit": 20},
                )
                data = resp.json()
                for msg in reversed(data.get("messages", [])):
                    if msg.get("ts") == after_ts or msg.get("bot_id"):
                        continue  # skip our own prompt and any other bot messages
                    return {"replied": True, "text": (msg.get("text") or "").strip()}
            except Exception:
                pass  # transient Slack/network hiccup — just retry on the next poll
            await asyncio.sleep(poll_interval_seconds)
    return {"replied": False, "text": ""}
