"""Export the internal workout format to Zwift (.zwo) and Garmin (.fit)."""

from __future__ import annotations

import datetime
import re
import xml.etree.ElementTree as ET

from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.workout_message import WorkoutMessage
from fit_tool.profile.messages.workout_step_message import WorkoutStepMessage
from fit_tool.profile.profile_type import (
    FileType,
    Intensity,
    Manufacturer,
    Sport,
    WorkoutStepDuration,
    WorkoutStepTarget,
)

from .workout import DEFAULT_THRESHOLD_PACE, _segment


class ExportError(ValueError):
    pass


def safe_filename(name: str, ext: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "workout"
    return f"{stem[:60]}.{ext}"


# --- Zwift ------------------------------------------------------------------

def to_zwo(workout: dict, threshold_pace_s_per_km: int | None = None) -> bytes:
    pace = threshold_pace_s_per_km or DEFAULT_THRESHOLD_PACE
    root = ET.Element("workout_file")
    ET.SubElement(root, "author").text = "Workout Builder"
    ET.SubElement(root, "name").text = workout["name"]
    ET.SubElement(root, "description").text = workout.get("description", "")
    ET.SubElement(root, "sportType").text = "bike" if workout["sport"] == "cycling" else "run"
    tags = ET.SubElement(root, "tags")
    for tag in workout.get("tags", []):
        ET.SubElement(tags, "tag", {"name": tag})
    body = ET.SubElement(root, "workout")

    for block in workout["steps"]:
        if block["type"] == "repeat":
            if _is_simple_interval(block):
                on, off = block["steps"]
                el = ET.SubElement(body, "IntervalsT", {
                    "Repeat": str(block["count"]),
                    "OnDuration": str(on["duration_s"]),
                    "OffDuration": str(off["duration_s"]),
                    "OnPower": _f(on["intensity"]),
                    "OffPower": _f(off["intensity"]),
                })
                if on.get("cadence"):
                    el.set("Cadence", str(on["cadence"]))
                if off.get("cadence"):
                    el.set("CadenceResting", str(off["cadence"]))
                _text_events(el, block.get("text_events") or on.get("text_events"), on.get("label"))
            else:
                first = len(body)
                for _ in range(block["count"]):
                    for step in block["steps"]:
                        _zwo_step(body, step, pace)
                # Messages for the whole block go on its first element.
                _text_events(body[first], block.get("text_events"), None)
        else:
            _zwo_step(body, block, pace)

    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=False)


def _is_simple_interval(block: dict) -> bool:
    steps = block["steps"]
    return (
        len(steps) == 2
        and all(s["type"] in ("steady", "rest") and "duration_s" in s for s in steps)
    )


def _zwo_step(body: ET.Element, step: dict, pace: int) -> None:
    # Zwift only understands time, so distance steps are converted with the threshold pace.
    duration = str(_segment(step, 0, pace)["duration_s"])
    kind = step["type"]
    if kind == "free":
        el = ET.SubElement(body, "FreeRide", {"Duration": duration})
    elif kind in ("warmup", "cooldown", "ramp"):
        tag = {"warmup": "Warmup", "cooldown": "Cooldown", "ramp": "Ramp"}[kind]
        el = ET.SubElement(body, tag, {
            "Duration": duration,
            "PowerLow": _f(step["intensity"]),
            "PowerHigh": _f(step["intensity_end"]),
        })
    else:
        el = ET.SubElement(body, "SteadyState", {"Duration": duration, "Power": _f(step["intensity"])})
    if step.get("cadence"):
        el.set("Cadence", str(step["cadence"]))
    _text_events(el, step.get("text_events"), step.get("label"))


def _text_events(el: ET.Element, events: list | None, label: str | None) -> None:
    """Write the step's messages; fall back to showing its label at the start."""
    if events:
        for e in events:
            ET.SubElement(el, "textevent", {"timeoffset": str(e["offset_s"]), "message": e["message"]})
    elif label:
        ET.SubElement(el, "textevent", {"timeoffset": "0", "message": label})


def _f(x: float) -> str:
    return f"{x:.3f}".rstrip("0").rstrip(".")


# --- Garmin -----------------------------------------------------------------

def to_fit(workout: dict, threshold_pace_s_per_km: int | None = None) -> bytes:
    running = workout["sport"] == "running"
    if running and not threshold_pace_s_per_km:
        raise ExportError("Set your threshold pace in Settings to export running workouts to Garmin.")

    steps: list[WorkoutStepMessage] = []
    for block in workout["steps"]:
        if block["type"] == "repeat":
            first = len(steps)
            for step in block["steps"]:
                steps.append(_fit_step(step, running, threshold_pace_s_per_km, in_repeat=True))
            rep = WorkoutStepMessage()
            rep.duration_type = WorkoutStepDuration.REPEAT_UNTIL_STEPS_CMPLT
            rep.duration_step = first
            rep.target_type = WorkoutStepTarget.OPEN
            rep.target_repeat_steps = block["count"]
            steps.append(rep)
        else:
            steps.append(_fit_step(block, running, threshold_pace_s_per_km))
    for i, step in enumerate(steps):
        step.message_index = i

    file_id = FileIdMessage()
    file_id.type = FileType.WORKOUT
    file_id.manufacturer = Manufacturer.DEVELOPMENT.value
    file_id.product = 0
    file_id.time_created = round(datetime.datetime.now().timestamp() * 1000)
    file_id.serial_number = 0x1A2B3C4D

    wkt = WorkoutMessage()
    wkt.workout_name = workout["name"][:40]
    wkt.sport = Sport.RUNNING if running else Sport.CYCLING
    wkt.num_valid_steps = len(steps)

    builder = FitFileBuilder(auto_define=True, min_string_size=50)
    builder.add(file_id)
    builder.add(wkt)
    builder.add_all(steps)
    return builder.build().to_bytes()


_FIT_INTENSITY = {
    "warmup": Intensity.WARMUP,
    "cooldown": Intensity.COOLDOWN,
    "rest": Intensity.REST,
    "free": Intensity.ACTIVE,
    "ramp": Intensity.ACTIVE,
    "steady": Intensity.ACTIVE,
}


def _fit_step(step: dict, running: bool, pace: int | None, in_repeat: bool = False) -> WorkoutStepMessage:
    msg = WorkoutStepMessage()
    name = step.get("label") or step["type"].capitalize()
    if step.get("cadence"):
        name = f"{name} @{step['cadence']}"
    msg.workout_step_name = name[:50]
    intensity = _FIT_INTENSITY[step["type"]]
    if in_repeat and step["type"] == "steady":
        intensity = Intensity.INTERVAL
    msg.intensity = intensity

    if "distance_m" in step:
        msg.duration_type = WorkoutStepDuration.DISTANCE
        msg.duration_distance = float(step["distance_m"])
    else:
        msg.duration_type = WorkoutStepDuration.TIME
        msg.duration_time = float(step["duration_s"])

    if step["type"] == "free" or step.get("intensity") is None:
        msg.target_type = WorkoutStepTarget.OPEN
        msg.target_value = 0
        return msg

    a, b = step["intensity"], step.get("intensity_end", step["intensity"])
    lo, hi = min(a, b), max(a, b)
    if lo == hi:
        lo, hi = lo * 0.98, hi * 1.02
    if running:
        speed = 1000 / pace  # threshold speed in m/s
        msg.target_type = WorkoutStepTarget.SPEED
        msg.target_value = 0
        msg.custom_target_speed_low = round(speed * lo, 3)
        msg.custom_target_speed_high = round(speed * hi, 3)
    else:
        # FIT convention: power values 0-1000 are % of FTP.
        msg.target_type = WorkoutStepTarget.POWER
        msg.target_value = 0
        msg.custom_target_power_low = int(round(lo * 100))
        msg.custom_target_power_high = int(round(hi * 100))
    return msg
