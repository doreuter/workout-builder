"""Read Zwift .zwo files into the app's workout format."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from .workout import ENABLED_SPORTS, INTENSITY_RANGE, WorkoutError, validate

MAX_ZWO_BYTES = 512 * 1024


class ImportError_(ValueError):
    pass


def from_zwo(data: bytes, fallback_name: str = "Imported workout") -> dict:
    if len(data) > MAX_ZWO_BYTES:
        raise ImportError_("That file is too large to be a Zwift workout.")
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ImportError_("That file isn't a valid Zwift workout.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise ImportError_("That file isn't valid XML, so it can't be a Zwift workout.")
    if _tag(root) != "workout_file":
        raise ImportError_("That file isn't a Zwift workout (.zwo).")

    sport = "running" if (_text(root, "sportType") or "").lower() == "run" else "cycling"
    if sport not in ENABLED_SPORTS:
        raise ImportError_(f"{sport.capitalize()} workouts can't be imported.")
    # Zwift running workouts can measure steps in metres instead of seconds.
    by_distance = sport == "running" and (_text(root, "durationType") or "").strip().lower() == "distance"
    body = _child(root, "workout")
    if body is None or len(body) == 0:
        raise ImportError_("That workout has no steps.")

    lo, hi = INTENSITY_RANGE[sport]

    def power(el, *names, default=0.5):
        for n in names:
            v = _attr(el, n)
            if v is not None:
                return round(min(max(float(v), lo), hi), 3)
        return default

    steps = []
    for el in body:
        kind = _tag(el)
        duration = int(round(float(_attr(el, "Duration") or 0)))
        events = _events(el)
        cadence = _cadence(_attr(el, "Cadence"))

        if kind in ("steadystate", "solidstate"):
            step = {"type": "steady", "duration_s": duration, "intensity": power(el, "Power", "PowerLow", "PowerHigh")}
        elif kind in ("warmup", "cooldown", "ramp"):
            step = {
                "type": kind,
                "duration_s": duration,
                "intensity": power(el, "PowerLow", "Power"),
                "intensity_end": power(el, "PowerHigh", "Power"),
            }
        elif kind == "freeride":
            step = {"type": "free", "duration_s": duration}
        elif kind == "maxeffort":
            step = {"type": "steady", "duration_s": duration, "intensity": hi, "label": "Max effort"}
        elif kind == "intervalst":
            on = {"type": "steady", "duration_s": int(round(float(_attr(el, "OnDuration") or 0))),
                  "intensity": power(el, "OnPower", "PowerOnHigh", "PowerOnLow", default=1.0)}
            off = {"type": "rest", "duration_s": int(round(float(_attr(el, "OffDuration") or 0))),
                   "intensity": power(el, "OffPower", "PowerOffLow", "PowerOffHigh")}
            if cadence:
                on["cadence"] = cadence
            if c := _cadence(_attr(el, "CadenceResting")):
                off["cadence"] = c
            if on["duration_s"] < 1 or off["duration_s"] < 1:
                continue
            block = {"type": "repeat", "count": max(1, int(_attr(el, "Repeat") or 1)), "steps": [on, off]}
            if events:
                block["text_events"] = events
            steps.append(block)
            continue
        elif kind == "textevent":
            continue
        else:
            raise ImportError_(f"This workout uses a block type the app doesn't support yet: {el.tag}.")

        if duration < 1:
            continue
        if cadence and step["type"] != "free":
            step["cadence"] = cadence
        if events:
            step["text_events"] = events
        steps.append(step)

    tags_el = _child(root, "tags")
    if by_distance:
        _durations_to_distances(steps)
    workout = {
        "name": (_text(root, "name") or fallback_name).strip(),
        "sport": sport,
        "description": (_text(root, "description") or "").strip(),
        "steps": steps,
        "tags": [t.get("name") for t in tags_el if t.get("name")] if tags_el is not None else [],
    }
    try:
        return validate(workout)
    except WorkoutError as e:
        raise ImportError_(f"Couldn't read this workout: {e}")


# --- Garmin .fit ---------------------------------------------------------------------------

MAX_FIT_BYTES = 256 * 1024
# Midpoints of the Coggan power zones (% FTP) for steps that target "power zone N".
POWER_ZONE_MID = {1: 0.45, 2: 0.65, 3: 0.83, 4: 0.98, 5: 1.13, 6: 1.35, 7: 1.6}
# Rough %FTP for heart-rate zone targets, so HR-based workouts still chart sensibly.
HR_ZONE_POWER = {1: 0.5, 2: 0.65, 3: 0.8, 4: 0.92, 5: 1.05}
_FIT_SPORTS = {0: "cycling", 1: "running", 2: "cycling"}  # generic counts as cycling
# Rough fraction of threshold speed for running steps that target "speed zone N" or a heart-rate zone.
RUN_ZONE_SPEED = {1: 0.75, 2: 0.84, 3: 0.91, 4: 0.98, 5: 1.06}
_INTENSITY_KIND = {0: "steady", 1: "rest", 2: "warmup", 3: "cooldown", 4: "rest", 5: "steady"}


def from_fit(data: bytes, ftp: int | None, fallback_name: str = "Imported workout",
             threshold_pace_s_per_km: int | None = None) -> dict:
    """Read a Garmin workout .fit file (as copied from the watch or exported from apps)."""
    from fit_tool.fit_file import FitFile
    from fit_tool.profile.messages.workout_message import WorkoutMessage
    from fit_tool.profile.messages.workout_step_message import WorkoutStepMessage

    if len(data) > MAX_FIT_BYTES:
        raise ImportError_("That file is too large to be a Garmin workout.")
    try:
        records = [r.message for r in FitFile.from_bytes(data).records]
    except Exception:
        raise ImportError_("That file isn't a valid .fit file.")
    header = next((m for m in records if isinstance(m, WorkoutMessage)), None)
    fit_steps = [m for m in records if isinstance(m, WorkoutStepMessage)]
    if header is None or not fit_steps:
        raise ImportError_("This .fit file isn't a workout. It may be a recorded ride; only planned workouts can be imported.")

    sport = _FIT_SPORTS.get(header.sport if header.sport is not None else 2)
    if sport not in ENABLED_SPORTS:
        raise ImportError_("Only cycling and running workouts can be imported.")

    fit_steps.sort(key=lambda m: m.message_index if m.message_index is not None else 0)
    steps: list[dict] = []
    index_map: dict[int, int] = {}  # FIT step index -> position in `steps`
    for i, m in enumerate(fit_steps):
        index_map[i] = len(steps)
        if m.duration_type == 6:  # repeat until steps complete
            start = index_map.get(m.duration_step)
            count = m.target_repeat_steps or 1
            if start is None or any(s["type"] == "repeat" for s in steps[start:]):
                raise ImportError_("This workout has nested repeats, which the app doesn't support yet.")
            block = {"type": "repeat", "count": count, "steps": steps[start:]}
            del steps[start:]
            steps.append(block)
        else:
            steps.append(_fit_step(m, ftp) if sport == "cycling" else _fit_run_step(m, threshold_pace_s_per_km))

    workout = {
        "name": (header.workout_name or fallback_name).strip(),
        "sport": sport,
        "description": "Imported from a Garmin workout file.",
        "steps": steps,
    }
    try:
        return validate(workout)
    except WorkoutError as e:
        raise ImportError_(f"Couldn't read this workout: {e}")


def _fit_step(m, ftp: int | None) -> dict:
    kind = _INTENSITY_KIND.get(m.intensity if m.intensity is not None else 0, "steady")
    name = (m.workout_step_name or "").strip()
    cadence = None
    if " @" in name:  # our own exports put the cadence in the name, e.g. "K3 @50"
        base, _, cad = name.rpartition(" @")
        if cad.isdigit():
            name, cadence = base, _cadence(cad)

    if m.duration_type == 0:
        duration = int(round(m.duration_time or 0))
    elif m.duration_type == 5:  # open: until lap button
        duration, name = 300, name or "Until lap button"
    elif m.duration_type == 1:
        raise ImportError_("This workout has distance-based steps, which the app only supports for running.")
    else:
        raise ImportError_("This workout uses a step length the app doesn't support (only time-based steps).")

    lo = hi = None
    label = None
    if m.target_type == 4:  # power
        if m.target_value:  # power zone
            lo = hi = POWER_ZONE_MID.get(m.target_value, 0.65)
            label = f"Power zone {m.target_value}"
        else:
            lo, hi = (_fit_power(v, ftp) for v in (m.custom_target_power_low, m.custom_target_power_high))
    elif m.target_type == 1:  # heart rate: approximate as power so it still shows on the chart
        zone = m.target_value or 2
        lo = hi = HR_ZONE_POWER.get(zone, 0.65)
        label = f"HR zone {zone}"

    if lo is None or hi is None:
        step = {"type": "free", "duration_s": duration}
    elif kind in ("warmup", "cooldown") and lo != hi:
        start, end = (lo, hi) if kind == "warmup" else (hi, lo)
        step = {"type": kind, "duration_s": duration, "intensity": start, "intensity_end": end}
    else:
        step = {"type": kind if kind in ("steady", "rest") else "steady", "duration_s": duration,
                "intensity": round((lo + hi) / 2, 3)}
    if name and name.lower() not in ("warmup", "cooldown", "steady", "rest", "free"):
        step["label"] = name
    elif label:
        step["label"] = label
    if cadence and step["type"] != "free":
        step["cadence"] = cadence
    return step


def _durations_to_distances(steps: list) -> None:
    """In a distance-based Zwift run the Duration attributes are metres."""
    for s in steps:
        if s["type"] == "repeat":
            _durations_to_distances(s["steps"])
        elif "duration_s" in s:
            s["distance_m"] = s.pop("duration_s")


def _fit_run_step(m, pace: int | None) -> dict:
    """A running step: speed targets become fractions of threshold speed; distance steps stay distances."""
    kind = _INTENSITY_KIND.get(m.intensity if m.intensity is not None else 0, "steady")
    name = (m.workout_step_name or "").strip()
    if m.duration_type == 0:
        length = {"duration_s": int(round(m.duration_time or 0))}
    elif m.duration_type == 1:
        length = {"distance_m": int(round(m.duration_distance or 0))}
    elif m.duration_type == 5:
        length, name = {"duration_s": 300}, name or "Until lap button"
    else:
        raise ImportError_("This workout uses a step length the app doesn't support (only time or distance).")

    lo = hi = None
    label = None
    if m.target_type == 0:  # speed
        if m.target_value:  # speed zone
            lo = hi = RUN_ZONE_SPEED.get(m.target_value, 0.84)
            label = f"Speed zone {m.target_value}"
        elif m.custom_target_speed_low or m.custom_target_speed_high:
            if not pace:
                raise ImportError_("This running workout uses pace targets. Set your threshold pace in Settings first, "
                                   "then import it again.")
            threshold = 1000 / pace  # m/s
            lo, hi = (round((v or 0) / threshold, 3) for v in (m.custom_target_speed_low, m.custom_target_speed_high))
    elif m.target_type == 1:  # heart rate zone, approximated as speed so it still charts
        zone = m.target_value or 2
        lo = hi = RUN_ZONE_SPEED.get(zone, 0.84)
        label = f"HR zone {zone}"

    if lo is None or hi is None:
        step = {"type": "free", **length}
    elif kind in ("warmup", "cooldown") and lo != hi:
        start, end = (lo, hi) if kind == "warmup" else (hi, lo)
        step = {"type": kind, **length, "intensity": start, "intensity_end": end}
    else:
        step = {"type": kind if kind in ("steady", "rest") else "steady", **length, "intensity": round((lo + hi) / 2, 3)}
    if name and name.lower() not in ("warmup", "cooldown", "steady", "rest", "free"):
        step["label"] = name
    elif label:
        step["label"] = label
    return step


def _fit_power(value, ftp: int | None) -> float | None:
    """FIT power targets: 0-1000 is % of FTP, above 1000 is watts + 1000."""
    if value is None:
        return None
    if value <= 1000:
        return round(value / 100, 3)
    if not ftp:
        raise ImportError_("This workout uses watt targets. Set your FTP in Settings first, then import it again.")
    return round((value - 1000) / ftp, 3)


def _tag(el) -> str:
    return el.tag.lower()


def _child(el, name: str):
    return next((c for c in el if _tag(c) == name.lower()), None)


def _text(el, name: str) -> str | None:
    c = _child(el, name)
    return c.text if c is not None else None


def _attr(el, name: str) -> str | None:
    # Zwift is inconsistent about attribute case, so match case-insensitively.
    for k, v in el.attrib.items():
        if k.lower() == name.lower():
            return v
    return None


def _cadence(v) -> int | None:
    try:
        c = int(float(v))
    except (TypeError, ValueError):
        return None
    return c if 30 <= c <= 220 else None


def _events(el) -> list[dict]:
    out = []
    for e in el:
        if _tag(e) == "textevent":
            msg = (_attr(e, "message") or "").strip()
            if msg:
                out.append({"offset_s": int(float(_attr(e, "timeoffset") or 0)), "message": msg})
    return out[:50]
