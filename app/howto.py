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


def goal(sport: str, intensity: float | None) -> str:
    """What the workout is for: the training effect of its main efforts (None: a steady session at
    `intensity` given as its average)."""
    limit = "threshold pace" if sport == "running" else "FTP"
    if intensity is None:
        return "Goal: aerobic base."
    if intensity < 0.76:
        return ("Goal: aerobic endurance. Easy time builds the engine (heart, capillaries, fat burning) "
                "with little fatigue, so you should finish feeling you could do more.")
    if intensity < 0.88:
        return ("Goal: muscular endurance. Long tempo efforts teach you to hold a strong, steady effort for "
                "longer — the pace of long climbs and hard group rides — at a fraction of the cost of threshold work.")
    if intensity < 0.95:
        return (f"Goal: raise your {limit}. Long efforts just under threshold add a lot of training for "
                "manageable fatigue.")
    if intensity < 1.06:
        return (f"Goal: raise your {limit} and how long you can hold it. Expect it to be tough; the last "
                "effort shows how well you paced the first.")
    if intensity < 1.25:
        return ("Goal: raise your VO2max, the ceiling of your aerobic engine. Short, very hard efforts make "
                "threshold feel easier over the following weeks.")
    return ("Goal: anaerobic power and speed: short, maximal efforts for attacks, sprints and steep kicks.")


def describe(workout: dict, ftp: int | None, pace: int | None) -> list:
    """Plain-language lines for doing this workout, anywhere."""
    sport = workout["sport"]
    move = "run" if sport == "running" else "ride"
    shape = structure(workout, pace)
    efs = shape["efforts"]
    if not efs:
        avg = shape.get("avg") or 0
        recovery = sport == "cycling" and avg < 0.56 or sport == "running" and avg < 0.7
        return [("Goal: recovery. Spin the legs and get blood flowing; finish fresher than you started."
                 if recovery else goal(sport, avg)),
                f"One steady {move} of {minutes(shape['total_s'])} at about {target(sport, avg, ftp, pace)}.",
                feel(sport, avg) + " Keep it relaxed the whole way: this is about time, not effort."]
    main_i = max(efs, key=lambda e: e[0])[1]
    lines = [goal(sport, main_i)]
    if shape["warm_s"]:
        line = f"Warm up {minutes(shape['warm_s'])}, starting easy and building gradually"
        if shape["openers"]:
            ops = shape["openers"]
            n, d = len(ops), ops[0][0]
            lo, hi = min(i for _, i in ops), max(i for _, i in ops)
            at = (f"at about {target(sport, lo, ftp, pace)}" if hi - lo < 0.005 else
                  f"building from {target(sport, lo, ftp, pace)} to {target(sport, hi, ftp, pace)}")
            size = f"{n} × {minutes(d)}" if all(abs(o[0] - d) < 1 for o in ops) else str(n)
            line += f", with {size} openers {at} near the end: they wake the legs up for the main set"
        lines.append(line + ".")
    groups: list = []  # (count, length as said, intensity)
    for (d, i, _), m in zip(efs, shape["effort_m"]):
        size = f"{m:,.0f} m".replace(",", "'") if m else minutes(d)
        if groups and groups[-1][1] == size and abs(groups[-1][2] - i) < 0.005:
            groups[-1] = (groups[-1][0] + 1, size, i)
        else:
            groups.append((1, size, i))
    work = "; ".join((f"{n} × " if n > 1 else "") + f"{size} at {target(sport, i, ftp, pace)}" for n, size, i in groups)
    recs = sorted({r for _, _, r in efs[:-1]})
    rest = ""
    if recs:
        rest = minutes(recs[0]) if len(recs) == 1 else f"{minutes(recs[0])}–{minutes(recs[-1])}"
        rest = f", with {rest} easy between"
    lines.append(f"Main set: {work}{rest}.")
    lines.append(feel(sport, main_i))
    if recs:
        lines.append("Between efforts " + ("jog or walk" if sport == "running" else "spin") +
                     " really easy, so the next one starts fresh.")
    if shape["after"]:
        parts = [f"{minutes(d)} easy" if kind == "easy" else f"{minutes(d)} steady at {target(sport, i, ftp, pace)}"
                 for kind, d, i in shape["after"]]
        lines.append("After the main set: " + ", then ".join(parts) + ". Keep it relaxed: this adds "
                     "endurance on top of the work.")
    if shape["cool_s"]:
        lines.append(f"Cool down {minutes(shape['cool_s'])} easy.")
    return lines
