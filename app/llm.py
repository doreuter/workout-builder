"""Chat with Claude to create and edit a workout through a `set_workout` tool."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import anthropic

from .workout import WORKOUT_SCHEMA, WorkoutError, summary, validate

DEFAULT_MODEL = "claude-opus-5-5"
# Models that support the server-side refusal fallback ("fallbacks": "default").
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}
MAX_TOOL_ROUNDS = 4
# USD per million tokens (input, output), for the app's own budget tracking.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}


def model() -> str:
    """CLAUDE_MODEL in .env overrides the default, e.g. claude-haiku-5-5 for cheap testing."""
    return os.environ.get("CLAUDE_MODEL") or DEFAULT_MODEL

SYSTEM_PROMPT = """You are a cycling coach inside a workout-builder app. The user describes a structured \
indoor or outdoor ride in plain language; you turn it into the app's workout format by calling the \
`set_workout` tool, and you also edit the current workout when they ask for changes.

The app supports cycling only for now. If the user asks for a running (or other non-cycling) workout, \
don't call the tool: say briefly that only cycling is supported at the moment.

How the format works:
- Intensity is a fraction of FTP (0.95 = 95% FTP). The tool always takes fractions, even if the user \
gives watts; convert using the athlete profile below.
- Step types: warmup / cooldown (ramp from intensity to intensity_end), steady, ramp, rest (easy \
recovery at a set intensity), free (no target). Use a `repeat` block for intervals.
- Each step has duration_s (do not use distance_m).
- Give every interval step a short label like "VO2 max" or "Threshold".
- Workouts loaded from Zwift may have `text_events` (on-screen coaching messages) and `tags`. Keep \
them when you edit the workout, adjusting offsets if a step gets shorter, unless the user asks otherwise.

The user can load an existing workout into the app. When that happens, or when the workout changed \
outside this chat, their message starts with the current workout as JSON: edit that one.

Zone guide (% FTP): recovery <55, endurance 56-75, tempo 76-90, sweet spot 88-94, threshold 95-105, \
VO2 max 106-120, anaerobic >120.

Whenever the user asks for a workout or a change, call `set_workout` with the COMPLETE workout (it \
replaces the current one). If a detail is missing, choose a sensible default and say what you chose; ask \
only when the request is truly ambiguous. After the tool succeeds, \
reply in 1-3 short sentences: what the workout is and any assumptions. Do not list every step; the app \
shows a chart. If the tool returns an error, fix the workout and call it again."""

class MissingCredentials(RuntimeError):
    pass


TOOLS = [
    {
        "name": "set_workout",
        "description": "Create or replace the current workout shown to the user. Always pass the full workout.",
        "input_schema": WORKOUT_SCHEMA,
    }
]


def _profile_text(user) -> str:
    parts = []
    if user["ftp"]:
        parts.append(f"FTP {user['ftp']} W")
    if user["max_hr"]:
        parts.append(f"max HR {user['max_hr']} bpm")
    return "Athlete profile: " + (", ".join(parts) if parts else "not set (use relative intensities only).")


def chat(user, history: list, current: dict | None, user_text: str, client: anthropic.Anthropic | None = None):
    """Run one chat turn. Returns (reply_text, workout, new_history, usage).

    `history` holds raw API message dicts and is only ever appended to, so earlier turns
    (including thinking blocks) are replayed unchanged.
    """
    client = client or anthropic.Anthropic()
    messages = list(history)
    # Context rides along as a separate text block: the profile every turn (settings may have
    # changed) and the workout itself whenever Claude hasn't seen this version yet (e.g. imported).
    context = _profile_text(user)
    if current is not None and current != _last_set_workout(history):
        context += "\n\nCurrent workout:\n" + json.dumps(current)
    messages.append({"role": "user", "content": [
        {"type": "text", "text": context},
        {"type": "text", "text": user_text},
    ]})

    workout = current
    reply = ""
    usage = Usage()
    name = model()
    extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if name in FALLBACK_MODELS else {}
    for _ in range(MAX_TOOL_ROUNDS):
        try:
            response = client.beta.messages.create(
                model=name,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
                output_config={"effort": "medium"},
                **extra,
            )
        except TypeError as e:
            # The SDK raises TypeError when it finds no API key or auth profile at all.
            if "authentication method" in str(e):
                raise MissingCredentials() from e
            raise
        messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in response.content]})
        usage.add(name, getattr(response, "usage", None))

        if response.stop_reason == "refusal":
            reply = "Sorry, I can't help with that request."
            break

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        texts = [b.text for b in response.content if b.type == "text"]
        if not tool_uses:
            reply = "\n\n".join(texts).strip()
            break

        results = []
        for tu in tool_uses:
            try:
                candidate = validate(tu.input)
            except WorkoutError as e:
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": f"Invalid workout: {e}", "is_error": True})
            else:
                workout = candidate
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": "Saved. " + summary(workout, user["threshold_pace_s_per_km"])})
        messages.append({"role": "user", "content": results})
    else:
        reply = "I couldn't build a valid workout. Could you rephrase the request?"

    return reply or "Done.", workout, messages, usage


@dataclass
class Usage:
    """Tokens and estimated cost of one chat turn (all API calls in it)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, model_name: str, u) -> None:
        if u is None:
            return
        # Cached prompt tokens are billed differently; counting them at the full input
        # price keeps the budget estimate on the safe side.
        inp = (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) \
            + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        out = u.output_tokens or 0
        in_price, out_price = PRICES.get(model_name, PRICES[DEFAULT_MODEL])
        self.input_tokens += inp
        self.output_tokens += out
        self.cost_usd += (inp * in_price + out * out_price) / 1_000_000


def _last_set_workout(history: list) -> dict | None:
    """The workout from Claude's most recent successful set_workout call."""
    inputs, last = {}, None
    for msg in history:
        if not isinstance(msg["content"], list):
            continue
        for block in msg["content"]:
            if block.get("type") == "tool_use":
                inputs[block["id"]] = block["input"]
            elif block.get("type") == "tool_result" and not block.get("is_error") and block["tool_use_id"] in inputs:
                try:
                    last = validate(inputs[block["tool_use_id"]])
                except WorkoutError:
                    pass
    return last


def visible_transcript(history: list) -> list[dict]:
    """The user/assistant text turns to show in the chat window."""
    out = []
    for msg in history:
        content = msg["content"]
        if msg["role"] == "user":
            if isinstance(content, str):  # older format: profile and text in one string
                text = content.split("\n\n", 1)[1] if content.startswith("Athlete profile:") else content
                out.append({"role": "user", "text": text})
            elif content and all(b.get("type") == "text" for b in content):
                out.append({"role": "user", "text": content[-1]["text"]})
        else:
            texts = [b.get("text", "") for b in content if b.get("type") == "text"]
            text = "\n\n".join(t for t in texts if t).strip()
            if text:
                out.append({"role": "assistant", "text": text})
    return out
