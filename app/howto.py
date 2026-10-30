"""How to do a workout, in plain words: the warm-up (with any openers), the main efforts with their
target and how they should feel, the recoveries and the cool-down. Shown on every workout; the climb
suggestions (app/climbs.py) add where to do it outside.
"""

from __future__ import annotations

from .workout import HARD_FROM, flatten

OPENER_MAX_S = 60  # short efforts before the main set are openers, part of the warm-up
# Main efforts start at tempo (judged on a step's average, so a warm-up ramp isn't one); a step that
# touches the guideline's hard zone counts too. Running intensities are fractions of threshold pace.
WORK_FROM = {"cycling": 0.76, "running": 0.85}
# Steady riding after the main set (an endurance block) is its own part, not cool-down.
STEADY_FROM = {"cycling": 0.6, "running": 0.75}
STEADY_MIN_S = 300


def structure(workout: dict, pace: int | None = None) -> dict:
    """The workout's shape: warm-up seconds (openers included), openers [(s, intensity)], main efforts
    [(s, intensity, recovery s after)], what follows the main set [("easy"|"steady", s, intensity)] and the
    cool-down seconds at the end. Consecutive work steps (over-unders) are one effort. Short efforts before
    the main set (up to a minute, and at most half the longest) are openers."""
    sport = workout["sport"]
    hard_from, work_from = HARD_FROM[sport], WORK_FROM[sport]
    blocks = []  # [work?, seconds, intensity x seconds, metres if every step was set as a distance, steps]
    for s in flatten(workout, pace):
        i0 = s["intensity"] or 0
        i1 = s["intensity_end"] if s["intensity_end"] is not None else i0
        work = s["duration_s"] >= 20 and ((i0 + i1) / 2 >= work_from or max(i0, i1) >= hard_from)
        metres = s.get("distance_m")
        if blocks and blocks[-1][0] == work:
            blocks[-1][1] += s["duration_s"]
            blocks[-1][2] += (i0 + i1) / 2 * s["duration_s"]
            blocks[-1][3] = blocks[-1][3] + metres if blocks[-1][3] is not None and metres else None
            blocks[-1][4].append(s)
        else:
            blocks.append([work, s["duration_s"], (i0 + i1) / 2 * s["duration_s"], metres, [s]])
    total = sum(b[1] for b in blocks)
    hard_at = [i for i, b in enumerate(blocks) if b[0]]
    if not hard_at:
        avg = sum(b[2] for b in blocks) / total if total else 0
        return {"warm_s": 0, "openers": [], "efforts": [], "after": [], "cool_s": 0, "total_s": total,
                "avg": avg}
    longest = max(blocks[i][1] for i in hard_at)
    n_open = 0
    while n_open < len(hard_at) - 1 and blocks[hard_at[n_open]][1] <= min(OPENER_MAX_S, longest / 2):
        n_open += 1
    main = hard_at[n_open:]
    efforts = []
    for k, i in enumerate(main):
        rec = sum(b[1] for b in blocks[i + 1:main[k + 1]]) if k + 1 < len(main) else 0
        efforts.append((blocks[i][1], blocks[i][2] / blocks[i][1], rec))
    tail = []  # after the main set: easy and steady parts; the easy part at the very end is the cool-down
    for s in (s for b in blocks[main[-1] + 1:] for s in b[4]):
        i0 = s["intensity"] or 0
        i1 = s["intensity_end"] if s["intensity_end"] is not None else i0
        kind = "steady" if (s["duration_s"] >= STEADY_MIN_S and i0 == i1 and i0 >= STEADY_FROM[sport]
                            and s["type"] != "cooldown") else "easy"
        if tail and tail[-1][0] == kind and (kind == "easy" or abs(tail[-1][2] - i0) < 0.005):
            tail[-1] = (kind, tail[-1][1] + s["duration_s"], i0)
        else:
            tail.append((kind, s["duration_s"], i0))
    cool_s = tail.pop()[1] if tail and tail[-1][0] == "easy" else 0
    return {
        "warm_s": sum(b[1] for b in blocks[:main[0]]),
        "openers": [(blocks[i][1], blocks[i][2] / blocks[i][1]) for i in hard_at[:n_open]],
        "efforts": efforts,
        "after": tail,
        "cool_s": cool_s,
        "total_s": total,
        "effort_m": [blocks[i][3] for i in main],  # metres per effort when set as distances (runs), else None
    }


def minutes(seconds: float) -> str:
    """'8 min', '3½ min', '30 s'."""
    m = round(seconds / 30) / 2
    if m < 1:
        return f"{round(seconds)} s"
    whole = int(m)
    return f"{whole}½ min" if m - whole else f"{whole} min"


def target(sport: str, intensity: float, ftp: int | None, pace: int | None) -> str:
    """'242 W (97 % FTP)' / '97 % FTP' / '4:28 /km' / '105 % of threshold pace'."""
    if sport == "running":
        if pace:
            s = round(pace / intensity)
            return f"{s // 60}:{s % 60:02d} /km"
        return f"{round(intensity * 100)} % of threshold pace"
    return f"{round(intensity * ftp)} W ({round(intensity * 100)} % FTP)" if ftp else f"{round(intensity * 100)} % FTP"


def feel(sport: str, intensity: float) -> str:
    """How an effort at this intensity should feel, and how to pace it."""
    if intensity < 0.76:
        return "Steady and controlled: you could still talk."
    if intensity < 0.88:
        return ("Tempo: moderately hard but sustainable, talking in short phrases. Settle into a smooth "
                "rhythm and hold it evenly to the end of each effort.")
    if intensity < 0.95:
        return "Comfortably hard: short sentences only. Keep it even; don't let it creep up."
    if intensity < 1.06:
        return ("Hard but steady, right at your limit for the duration. Start on target, not above: "
                "the last effort should be as strong as the first.")
    if intensity < 1.25:
        return ("Very hard: breathing heavily by the end. Settle onto the target in the first 20 seconds "
                "rather than sprinting, and hold it to the finish.")
    return "All out for its length, with full recovery between."


def goal(sport: str, intensity: float, steady: bool = False) -> dict:
    """What the workout is for: {"label", "text"}. The text says it plainly first, then the physiology
    (zone, threshold, adaptation) for those who know it. `intensity` is the main efforts' (or, for a
    steady session, the average)."""
    run = sport == "running"
    of = "of threshold pace" if run else "FTP"
    if steady and intensity < (0.7 if run else 0.56):
        return {"label": "Recovery", "text": (
            "Easy movement that helps you recover faster than resting: blood flow without new training "
            f"stress. Zone 1, well below your first lactate threshold (under {'70 %' if run else '55 %'} {of}).")}
    if intensity < WORK_FROM[sport]:
        return {"label": "Aerobic endurance", "text": (
            "Builds your base so you can go longer before tiring, with little fatigue. Zone 2, below your "
            "first lactate threshold (LT1): more mitochondria and capillaries in the working muscles and "
            "better fat oxidation, which spares glycogen.")}
    if intensity < 0.88:
        where = "long climbs and the steady middle of a race" if run else "long climbs and hard group rides"
        return {"label": "Muscular endurance", "text": (
            f"Teaches you to hold a strong, steady effort for longer, like {where}. Tempo (zone 3, "
            f"{round(WORK_FROM[sport] * 100)}–87 % {of}) sits between your two lactate thresholds: it improves fatigue resistance and "
            "lactate clearance at a moderate cost in recovery.")}
    if intensity < 0.95:
        return {"label": "Sweet spot", "text": (
            f"Raises your {'threshold pace' if run else 'FTP'} efficiently. Long efforts at 88–94 % {of}, "
            "just below your lactate threshold (LT2), give a large aerobic stimulus for fatigue you can "
            "recover from in a day or two.")}
    if intensity < 1.06:
        return {"label": "Threshold", "text": (
            "Raises the effort you can hold for about an hour, and how long you can hold it. Efforts at "
            f"95–105 % {of}, around your maximal lactate steady state (MLSS), where lactate production "
            "and clearance just balance.")}
    if intensity < 1.25:
        return {"label": "VO2max", "text": (
            "Raises the ceiling of your aerobic engine, so threshold efforts feel easier over the coming "
            f"weeks. Efforts at 106–120 % {of} drive heart rate and oxygen uptake close to their maximum "
            "(VO2max), training stroke volume and oxygen delivery.")}
    what = "surges, sprints and steep kicks" if run else "attacks, sprints and steep kicks"
    return {"label": "Anaerobic power", "text": (
        f"Sharpens your punch for {what}. Short, near-maximal efforts above 120 % {of} train anaerobic "
        "capacity (the glycolytic system) and neuromuscular power; full recoveries keep the quality high.")}


def describe(workout: dict, ftp: int | None, pace: int | None) -> dict:
    """How to do this workout, anywhere: {"goal": {"label", "text"}, "phases": [{"name", "what",
    "note"}]}, phases in order (warm up, main set, after, cool down)."""
    sport = workout["sport"]
    shape = structure(workout, pace)
    efs = shape["efforts"]
    if not efs:
        avg = shape.get("avg") or 0
        return {"goal": goal(sport, avg, steady=True), "phases": [{
            "name": "Run" if sport == "running" else "Ride",
            "what": f"{minutes(shape['total_s'])} steady at about {target(sport, avg, ftp, pace)}",
            "note": feel(sport, avg) + " Keep it relaxed the whole way: this is about time, not effort."}]}
    main_i = max(efs, key=lambda e: e[0])[1]
    phases = []
    if shape["warm_s"]:
        note = None
        if shape["openers"]:
            ops = shape["openers"]
            n, d = len(ops), ops[0][0]
            lo, hi = min(i for _, i in ops), max(i for _, i in ops)
            at = (f"at about {target(sport, lo, ftp, pace)}" if hi - lo < 0.005 else
                  f"building from {target(sport, lo, ftp, pace)} to {target(sport, hi, ftp, pace)}")
            size = f"{n} × {minutes(d)}" if all(abs(o[0] - d) < 1 for o in ops) else str(n)
            note = f"{size} openers near the end, {at}: they wake the legs up for the main set."
        phases.append({"name": "Warm up", "what": f"{minutes(shape['warm_s'])} easy, building gradually",
                       "note": note})
    groups: list = []  # (count, length as said, intensity)
    for (d, i, _), m in zip(efs, shape["effort_m"]):
        size = f"{m:,.0f} m".replace(",", "'") if m else minutes(d)
        if groups and groups[-1][1] == size and abs(groups[-1][2] - i) < 0.005:
            groups[-1] = (groups[-1][0] + 1, size, i)
        else:
            groups.append((1, size, i))
    work = "; ".join((f"{n} × " if n > 1 else "") + f"{size} at {target(sport, i, ftp, pace)}" for n, size, i in groups)
    recs = sorted({r for _, _, r in efs[:-1]})
    note = feel(sport, main_i)
    if recs:
        rest = minutes(recs[0]) if len(recs) == 1 else f"{minutes(recs[0])}–{minutes(recs[-1])}"
        work += f", {rest} easy between"
        note += " Between efforts " + ("jog or walk" if sport == "running" else "spin") + " really easy."
    phases.append({"name": "Main set", "what": work, "note": note})
    if shape["after"]:
        parts = [f"{minutes(d)} easy" if kind == "easy" else f"{minutes(d)} steady at {target(sport, i, ftp, pace)}"
                 for kind, d, i in shape["after"]]
        steady = any(kind == "steady" for kind, _, _ in shape["after"])
        phases.append({"name": "After", "what": ", then ".join(parts),
                       "note": "Relaxed: this adds endurance on top of the work." if steady else None})
    if shape["cool_s"]:
        phases.append({"name": "Cool down", "what": f"{minutes(shape['cool_s'])} easy", "note": None})
    return {"goal": goal(sport, main_i), "phases": phases}
