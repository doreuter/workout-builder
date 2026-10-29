"""The app's internal workout format: schema, validation, flattening and stats.

Intensity is always a fraction of the athlete's threshold:
  cycling -> fraction of FTP (1.0 = FTP)
  running -> fraction of threshold *speed* (1.0 = threshold pace, 1.1 = 10% faster)
"""

from __future__ import annotations

SPORTS = ("cycling", "running")
ENABLED_SPORTS = SPORTS
STEP_TYPES = ("warmup", "cooldown", "steady", "ramp", "free", "rest")
DEFAULT_THRESHOLD_PACE = 300  # s/km, used only to estimate time for distance steps

TEXT_EVENTS_SCHEMA = {
    "type": "array",
    "description": "On-screen messages shown during the step (Zwift). Keep existing ones unless asked to change them.",
    "items": {
        "type": "object",
        "properties": {
            "offset_s": {"type": "integer", "description": "Seconds after the step (or repeat block) starts."},
            "message": {"type": "string"},
        },
        "required": ["offset_s", "message"],
        "additionalProperties": False,
    },
}

STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": list(STEP_TYPES)},
        "duration_s": {"type": "integer", "description": "Step length in seconds. Use this OR distance_m."},
        "distance_m": {"type": "integer", "description": "Step length in metres (running only). Use this OR duration_s."},
        "intensity": {
            "type": "number",
            "description": "Target as a fraction of threshold (FTP for cycling, threshold speed for running). "
            "For warmup/cooldown/ramp this is the starting value. Omit for 'free'.",
        },
        "intensity_end": {
            "type": "number",
            "description": "End intensity for warmup/cooldown/ramp steps.",
        },
        "cadence": {"type": "integer", "description": "Optional target cadence (rpm or steps/min)."},
        "label": {"type": "string", "description": "Short on-screen text, e.g. 'Threshold 1/4'."},
        "text_events": TEXT_EVENTS_SCHEMA,
    },
    "required": ["type"],
    "additionalProperties": False,
}

REPEAT_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["repeat"]},
        "count": {"type": "integer", "description": "Number of repetitions."},
        "steps": {"type": "array", "items": STEP_SCHEMA, "description": "Steps inside one repetition."},
        "text_events": TEXT_EVENTS_SCHEMA,
    },
    "required": ["type", "count", "steps"],
    "additionalProperties": False,
}

WORKOUT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "sport": {"type": "string", "enum": list(ENABLED_SPORTS)},
        "description": {"type": "string"},
        "steps": {"type": "array", "items": {"anyOf": [STEP_SCHEMA, REPEAT_SCHEMA]}},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "Zwift tags. Keep existing ones."},
    },
    "required": ["name", "sport", "description", "steps"],
    "additionalProperties": False,
}

INTENSITY_RANGE = {"cycling": (0.2, 3.0), "running": (0.4, 1.5)}


class WorkoutError(ValueError):
    pass


def validate(workout: dict, sports: tuple[str, ...] = ENABLED_SPORTS) -> dict:
    """Validate and normalise a workout. Raises WorkoutError with a readable message."""
    if not isinstance(workout, dict):
        raise WorkoutError("workout must be an object")
    sport = workout.get("sport")
    if sport not in sports:
        raise WorkoutError(f"sport must be one of {sports}")
    name = str(workout.get("name") or "").strip()[:80]
    if not name:
        raise WorkoutError("name is required")
    steps = workout.get("steps")
    if not isinstance(steps, list) or not steps:
        raise WorkoutError("steps must be a non-empty list")

    clean_steps = []
    for i, block in enumerate(steps):
        where = f"steps[{i}]"
        if isinstance(block, dict) and block.get("type") == "repeat":
            count = block.get("count")
            if not isinstance(count, int) or not 1 <= count <= 50:
                raise WorkoutError(f"{where}.count must be an integer between 1 and 50")
            inner = block.get("steps")
            if not isinstance(inner, list) or not inner:
                raise WorkoutError(f"{where}.steps must be a non-empty list")
            rep = {
                "type": "repeat",
                "count": count,
                "steps": [_validate_step(s, sport, f"{where}.steps[{j}]") for j, s in enumerate(inner)],
            }
            events = _validate_text_events(block.get("text_events"), where)
            if events:
                rep["text_events"] = events
            clean_steps.append(rep)
        else:
            clean_steps.append(_validate_step(block, sport, where))

    clean = {
        "name": name,
        "sport": sport,
        "description": str(workout.get("description") or "").strip()[:1000],
        "steps": clean_steps,
    }
    tags = workout.get("tags")
    if isinstance(tags, list):
        clean_tags = [str(t).strip()[:40] for t in tags if str(t).strip()][:20]
        if clean_tags:
            clean["tags"] = clean_tags
    total = sum(seg["duration_s"] for seg in flatten(clean))
    if total > 8 * 3600:
        raise WorkoutError("workout is longer than 8 hours")
    return clean


def _validate_step(step, sport: str, where: str) -> dict:
    if not isinstance(step, dict):
        raise WorkoutError(f"{where} must be an object")
    kind = step.get("type")
    if kind not in STEP_TYPES:
        raise WorkoutError(f"{where}.type must be one of {STEP_TYPES}")

    out: dict = {"type": kind}
    duration, distance = step.get("duration_s"), step.get("distance_m")
    if (duration is None) == (distance is None):
        raise WorkoutError(f"{where} needs exactly one of duration_s or distance_m")
    if duration is not None:
        if not isinstance(duration, (int, float)) or not 1 <= duration <= 6 * 3600:
            raise WorkoutError(f"{where}.duration_s must be between 1 and 21600")
        out["duration_s"] = int(round(duration))
    else:
        if sport != "running":
            raise WorkoutError(f"{where}: distance_m is only supported for running")
        if not isinstance(distance, (int, float)) or not 10 <= distance <= 50000:
            raise WorkoutError(f"{where}.distance_m must be between 10 and 50000")
        out["distance_m"] = int(round(distance))

    lo, hi = INTENSITY_RANGE[sport]
    if kind == "free":
        pass
    else:
        intensity = step.get("intensity")
        if not isinstance(intensity, (int, float)) or not lo <= intensity <= hi:
            raise WorkoutError(f"{where}.intensity must be a number between {lo} and {hi}")
        out["intensity"] = round(float(intensity), 3)
        if kind in ("warmup", "cooldown", "ramp"):
            end = step.get("intensity_end", intensity)
            if not isinstance(end, (int, float)) or not lo <= end <= hi:
                raise WorkoutError(f"{where}.intensity_end must be a number between {lo} and {hi}")
            out["intensity_end"] = round(float(end), 3)

    cadence = step.get("cadence")
    if cadence is not None:
        if not isinstance(cadence, (int, float)) or not 30 <= cadence <= 220:
            raise WorkoutError(f"{where}.cadence must be between 30 and 220")
        out["cadence"] = int(cadence)
    if step.get("label"):
        out["label"] = str(step["label"])[:60]
    events = _validate_text_events(step.get("text_events"), where)
    if events:
        out["text_events"] = events
    return out


def _validate_text_events(events, where: str) -> list[dict]:
    if events is None:
        return []
    if not isinstance(events, list) or len(events) > 50:
        raise WorkoutError(f"{where}.text_events must be a list of at most 50 messages")
    out = []
    for e in events:
        if not isinstance(e, dict) or not isinstance(e.get("offset_s"), (int, float)) or e["offset_s"] < 0:
            raise WorkoutError(f"{where}.text_events items need offset_s >= 0 and a message")
        message = str(e.get("message") or "").strip()[:200]
        if message:
            out.append({"offset_s": int(e["offset_s"]), "message": message})
    return out


def flatten(workout: dict, threshold_pace_s_per_km: int | None = None) -> list[dict]:
    """Expand repeats into a flat list of timed segments (for charts, stats and exports)."""
    pace = threshold_pace_s_per_km or DEFAULT_THRESHOLD_PACE
    segments = []
    t = 0
    for block in workout["steps"]:
        if block["type"] == "repeat":
            for rep in range(block["count"]):
                for step in block["steps"]:
                    seg = _segment(step, t, pace, repeat=(rep + 1, block["count"]))
                    segments.append(seg)
                    t += seg["duration_s"]
        else:
            seg = _segment(block, t, pace)
            segments.append(seg)
            t += seg["duration_s"]
    return segments


def _segment(step: dict, start: int, pace: int, repeat=None) -> dict:
    start_i = step.get("intensity")
    end_i = step.get("intensity_end", start_i)
    if "duration_s" in step:
        duration = step["duration_s"]
    else:
        avg = ((start_i or 0.7) + (end_i or start_i or 0.7)) / 2
        speed = 1000 / pace * avg  # m/s
        duration = int(round(step["distance_m"] / speed))
    return {
        "type": step["type"],
        "start_s": start,
        "duration_s": duration,
        "distance_m": step.get("distance_m"),
        "intensity": start_i,
        "intensity_end": end_i,
        "cadence": step.get("cadence"),
        "label": step.get("label"),
        "repeat": list(repeat) if repeat else None,
    }


def stats(workout: dict, threshold_pace_s_per_km: int | None = None) -> dict:
    """Total time and a training-stress estimate (TSS for cycling, rTSS-style for running)."""
    segments = flatten(workout, threshold_pace_s_per_km)
    total = sum(s["duration_s"] for s in segments)
    # Normalised intensity: 4th-power mean, sampled per second for ramps.
    acc = 0.0
    for s in segments:
        a = s["intensity"] if s["intensity"] is not None else 0.5
        b = s["intensity_end"] if s["intensity_end"] is not None else a
        if a == b:
            acc += a**4 * s["duration_s"]
        else:
            n = s["duration_s"]
            acc += sum((a + (b - a) * (k + 0.5) / n) ** 4 for k in range(n))
    if_ = (acc / total) ** 0.25 if total else 0
    tss = total / 3600 * if_**2 * 100
    return {"duration_s": total, "intensity_factor": round(if_, 2), "tss": round(tss)}


def summary(workout: dict, threshold_pace_s_per_km: int | None = None) -> str:
    st = stats(workout, threshold_pace_s_per_km)
    mins = st["duration_s"] // 60
    return f"{workout['name']}: {mins // 60}h{mins % 60:02d}m, IF {st['intensity_factor']}, ~{st['tss']} TSS"


# --- training guidelines -----------------------------------------------------

# From this intensity on a step counts as hard work that needs a warm-up: sweet spot for cycling (% FTP),
# a bit below threshold pace for running (fraction of threshold speed).
HARD_FROM = {"cycling": 0.88, "running": 0.92}
MIN_WARMUP_S = 600
MIN_COOLDOWN_S = 300


def guideline_problems(workout: dict, threshold_pace_s_per_km: int | None = None) -> list[str]:
    """What a workout misses of the app's training guidelines (empty = fine). Applied to workouts the
    chats build, not to imported ones."""
    hard_from = HARD_FROM[workout["sport"]]
    segments = flatten(workout, threshold_pace_s_per_km)
    hard = [i for i, s in enumerate(segments)
            if max(s["intensity"] or 0, s["intensity_end"] or 0) >= hard_from and s["duration_s"] >= 30]
    if not hard:
        return []
    problems = []
    before = sum(s["duration_s"] for s in segments[:hard[0]])
    after = sum(s["duration_s"] for s in segments[hard[-1] + 1:])
    if before < MIN_WARMUP_S:
        problems.append(f"the hard work starts after {before // 60} min; warm up for at least "
                        f"{MIN_WARMUP_S // 60} min first, building gradually from easy")
    if after < MIN_COOLDOWN_S:
        problems.append(f"only {after // 60} min after the last hard effort; end with at least "
                        f"{MIN_COOLDOWN_S // 60} min easy cool-down")
    return problems

