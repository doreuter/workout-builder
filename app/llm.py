"""Chat with Claude: one workout through a `set_workout` tool, or a training plan through planning tools."""

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

Some athletes connect Garmin. Then their message context also contains a readiness summary (the app's \
own estimate from HRV, sleep, Garmin's recovery time, resting heart rate, Body Battery and training load) and their recent \
activities. Use it like a coach would:
- When they ask what to do today, or leave the intensity open, match the session to readiness: about 75+ \
supports intervals or a hard session; 55-74 endurance, tempo or sweet spot; 40-54 an easy Zone 1-2 ride; \
below 40 rest or a very short spin. Also consider load: a 7-day/28-day ratio above about 1.3, or a big \
load in the last 48 h, argues for easier work; a ratio below about 0.8 means they're fresh.
- The summary may also carry Garmin's recovery time with a traffic light. Red (over 36 h left): a recovery \
ride or rest. Yellow (15-36 h): Zone 1-2 or a short tempo block, no hard intervals. Green (under 15 h): \
anything readiness allows. When readiness and recovery time disagree, follow the more cautious one.
- When they ask for a specific workout, build what they asked for. If readiness clearly argues against it \
(for example a VO2 max session at readiness 40), build it anyway, say so in one sentence and offer an \
easier alternative. Don't lecture.
- Mention the one or two readings that drove your choice (e.g. "HRV is below your usual range"). Don't \
recite the whole summary.
- Without a readiness summary, don't guess about their recovery.

Zone guide (% FTP): recovery <55, endurance 56-75, tempo 76-90, sweet spot 88-94, threshold 95-105, \
VO2 max 106-120, anaerobic >120.

Whenever the user asks for a workout or a change, call `set_workout` with the COMPLETE workout (it \
replaces the current one). If a detail is missing, choose a sensible default and say what you chose; ask \
only when the request is truly ambiguous. After the tool succeeds, \
reply in 1-3 short sentences: what the workout is and any assumptions. Do not list every step; the app \
shows a chart. If the tool returns an error, fix the workout and call it again."""

PLAN_PROMPT = """You are a cycling coach planning the user's training calendar inside a workout app. They \
tell you what they want ("plan my week: 3 rides, long one on Saturday", "move Thursday's session to Friday", \
"make next week easier"); you put complete structured workouts on dates with `plan_workouts`, replace them \
(pass `replaces_id`) or take them off with `remove_planned`.

The app supports cycling only. Each workout uses the same format as the app's single-workout chat: \
intensity as a fraction of FTP, steps warmup / cooldown / steady / ramp / rest / free with duration_s, \
`repeat` blocks for intervals, a short label on each interval step, and a clear name such as \
"Sweet Spot 3x12" (no date in the name).

The context lists today's date and weekday and what's already planned (with ids). Plan from today on, in \
the athlete's local dates (YYYY-MM-DD). Don't put workouts on days the user wants free. Unless they say \
otherwise: at most one workout per day, no more than two hard days (threshold, VO2 max, anaerobic) in a \
row, an easy or rest day after a hard day, and most of the volume in Zone 2. Leave planned workouts alone \
unless the user asks you to change them.

If the context has readiness or Garmin's recovery time, use it for the next two days only: red recovery \
or low readiness means today and tomorrow stay easy. Without it, don't guess.

Zone guide (% FTP): recovery <55, endurance 56-75, tempo 76-90, sweet spot 88-94, threshold 95-105, \
VO2 max 106-120, anaerobic >120.

After the tools succeed, reply in 2-4 short sentences: the idea of the plan and anything you assumed. \
Don't list every workout; the app shows the calendar. If a tool returns an error, fix it and call it again."""

PLAN_TOOLS = [
    {
        "name": "plan_workouts",
        "description": "Put one or more complete workouts on dates. Pass replaces_id to replace a planned "
                       "workout (its date may change too).",
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 14,
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string", "description": "YYYY-MM-DD"},
                            "replaces_id": {"type": "integer", "description": "Id of a planned workout to replace"},
                            "workout": WORKOUT_SCHEMA,
                        },
                        "required": ["date", "workout"],
                    },
                },
            },
            "required": ["items"],
        },
    },
    {
        "name": "remove_planned",
        "description": "Take planned workouts off the calendar (they stay in the user's workout list).",
        "input_schema": {
            "type": "object",
            "properties": {"ids": {"type": "array", "items": {"type": "integer"}, "minItems": 1}},
            "required": ["ids"],
        },
    },
]


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
    if user["lthr_bike"]:
        parts.append(f"cycling threshold HR {user['lthr_bike']} bpm")
    if user["lthr_run"]:
        parts.append(f"running threshold HR {user['lthr_run']} bpm")
    if user["threshold_pace_s_per_km"]:
        p = user["threshold_pace_s_per_km"]
        parts.append(f"running threshold pace {p // 60}:{p % 60:02d} /km")
    return "Athlete profile: " + (", ".join(parts) if parts else "not set (use relative intensities only).")


def chat(user, history: list, current: dict | None, user_text: str, client: anthropic.Anthropic | None = None,
         readiness_text: str | None = None):
    """Run one chat turn. Returns (reply_text, workout, new_history, usage).

    `history` holds raw API message dicts and is only ever appended to, so earlier turns
    (including thinking blocks) are replayed unchanged.
    """
    messages = list(history)
    # Context rides along as a separate text block: the profile every turn (settings may have
    # changed) and the workout itself whenever Claude hasn't seen this version yet (e.g. imported).
    context = _profile_text(user)
    if readiness_text:
        context += "\n\n" + readiness_text
    if current is not None and current != _last_set_workout(history):
        context += "\n\nCurrent workout:\n" + json.dumps(current)
    messages.append({"role": "user", "content": [
        {"type": "text", "text": context},
        {"type": "text", "text": user_text},
    ]})

    workout = current

    def handle(tu):
        nonlocal workout
        try:
            candidate = validate(tu.input)
        except WorkoutError as e:
            return f"Invalid workout: {e}", True
        workout = candidate
        return "Saved. " + summary(workout, user["threshold_pace_s_per_km"]), False

    reply, usage = _run(client, SYSTEM_PROMPT, TOOLS, messages, handle,
                        "I couldn't build a valid workout. Could you rephrase the request?")
    return reply or "Done.", workout, messages, usage


def plan_chat(user, history: list, user_text: str, plan_text: str, planner, client: anthropic.Anthropic | None = None,
              readiness_text: str | None = None):
    """Run one planning turn. `plan_text` describes today and what's on the calendar; `planner` applies
    the tools (add / replace / remove workouts on dates) and says what it did. Returns (reply, new_history, usage)."""
    messages = list(history)
    context = _profile_text(user) + "\n\n" + plan_text
    if readiness_text:
        context += "\n\n" + readiness_text
    messages.append({"role": "user", "content": [
        {"type": "text", "text": context},
        {"type": "text", "text": user_text},
    ]})

    def handle(tu):
        if tu.name == "plan_workouts":
            return planner.plan(tu.input.get("items") or [])
        if tu.name == "remove_planned":
            return planner.remove(tu.input.get("ids") or [])
        return f"Unknown tool {tu.name}.", True

    reply, usage = _run(client, PLAN_PROMPT, PLAN_TOOLS, messages, handle,
                        "I couldn't finish the plan. Could you rephrase the request?")
    return reply or "Done.", messages, usage


def trim_history(history: list, max_turns: int) -> list:
    """Keep only the last `max_turns` exchanges, cut at the start of a user text turn so tool calls and
    their results stay together. Keeps a long-running chat (the planner) from growing without bound."""
    starts = [i for i, m in enumerate(history) if m["role"] == "user" and isinstance(m["content"], list)
              and m["content"] and m["content"][0].get("type") == "text"]
    return history[starts[-max_turns]:] if len(starts) > max_turns else history


def _run(client, system: str, tools: list, messages: list, handle, give_up: str):
    """The tool loop shared by both chats: call Claude, run its tool calls through `handle(tool_use)`
    -> (result_text, is_error), repeat until it answers in text. Appends to `messages` in place."""
    client = client or anthropic.Anthropic()
    reply = ""
    usage = Usage()
    name = model()
    extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if name in FALLBACK_MODELS else {}
    for _ in range(MAX_TOOL_ROUNDS):
        try:
            response = client.beta.messages.create(
                model=name,
                max_tokens=16000,
                system=system,
                tools=tools,
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
            content, is_error = handle(tu)
            result = {"type": "tool_result", "tool_use_id": tu.id, "content": content}
            if is_error:
                result["is_error"] = True
            results.append(result)
        messages.append({"role": "user", "content": results})
    else:
        reply = give_up
    return reply, usage


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
