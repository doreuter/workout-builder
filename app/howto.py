"""How to do a workout, in plain words: the warm-up (with any openers), the main efforts with their
target and how they should feel, the recoveries and the cool-down. Shown on every workout; the climb
suggestions (app/climbs.py) add where to do it outside.
"""

from __future__ import annotations

from .workout import HARD_FROM, flatten

OPENER_MAX_S = 60  # short efforts before the main set are openers, part of the warm-up


def structure(workout: dict, pace: int | None = None) -> dict:
    """The workout's shape: warm-up seconds (openers included), openers [(s, intensity)], main efforts
    [(s, intensity, recovery s after)], cool-down seconds. Consecutive hard steps (over-unders) are one
    effort. Short efforts before the main set (up to a minute, and at most half the longest) are openers."""
    hard_from = HARD_FROM[workout["sport"]]
    blocks = []  # [hard?, seconds, intensity x seconds, metres if every step was set as a distance]
    for s in flatten(workout, pace):
        i0 = s["intensity"] or 0
        i1 = s["intensity_end"] if s["intensity_end"] is not None else i0
        hard = max(i0, i1) >= hard_from and s["duration_s"] >= 20
        metres = s.get("distance_m")
        if blocks and blocks[-1][0] == hard:
            blocks[-1][1] += s["duration_s"]
            blocks[-1][2] += (i0 + i1) / 2 * s["duration_s"]
            blocks[-1][3] = blocks[-1][3] + metres if blocks[-1][3] is not None and metres else None
        else:
            blocks.append([hard, s["duration_s"], (i0 + i1) / 2 * s["duration_s"], metres])
    total = sum(b[1] for b in blocks)
    hard_at = [i for i, b in enumerate(blocks) if b[0]]
    if not hard_at:
        avg = sum(b[2] for b in blocks) / total if total else 0
        return {"warm_s": 0, "openers": [], "efforts": [], "cool_s": 0, "total_s": total, "avg": avg}
    longest = max(blocks[i][1] for i in hard_at)
    n_open = 0
    while n_open < len(hard_at) - 1 and blocks[hard_at[n_open]][1] <= min(OPENER_MAX_S, longest / 2):
        n_open += 1
    main = hard_at[n_open:]
    efforts = []
    for k, i in enumerate(main):
        rec = sum(b[1] for b in blocks[i + 1:main[k + 1]]) if k + 1 < len(main) else 0
        efforts.append((blocks[i][1], blocks[i][2] / blocks[i][1], rec))
    return {
        "warm_s": sum(b[1] for b in blocks[:main[0]]),
        "openers": [(blocks[i][1], blocks[i][2] / blocks[i][1]) for i in hard_at[:n_open]],
        "efforts": efforts,
        "cool_s": sum(b[1] for b in blocks[main[-1] + 1:]),
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
    if intensity < 0.88:
        return "Steady and controlled: you could still talk."
    if intensity < 0.95:
        return "Comfortably hard: short sentences only. Keep it even; don't let it creep up."
    if intensity < 1.06:
        return ("Hard but steady, right at your limit for the duration. Start on target, not above: "
                "the last effort should be as strong as the first.")
    if intensity < 1.25:
        return ("Very hard: breathing heavily by the end. Settle onto the target in the first 20 seconds "
                "rather than sprinting, and hold it to the finish.")
    return "All out for its length, with full recovery between."


def describe(workout: dict, ftp: int | None, pace: int | None) -> list:
    """Plain-language lines for doing this workout, anywhere."""
    sport = workout["sport"]
    move = "run" if sport == "running" else "ride"
    shape = structure(workout, pace)
    efs = shape["efforts"]
    if not efs:
        avg = shape.get("avg") or 0
        return [f"One steady {move} of {minutes(shape['total_s'])} at about {target(sport, avg, ftp, pace)}.",
                feel(sport, avg) + " Keep it relaxed the whole way: this is about time, not effort."]
    lines = []
    if shape["warm_s"]:
        line = f"Warm up {minutes(shape['warm_s'])}, starting easy and building gradually"
        if shape["openers"]:
            n, (d, i) = len(shape["openers"]), shape["openers"][0]
            line += (f", with {n} × {minutes(d)} openers at about {target(sport, i, ftp, pace)} near the end: "
                     "they wake the legs up for the main set")
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
    main_i = max(efs, key=lambda e: e[0])[1]
    lines.append(feel(sport, main_i))
    if recs:
        lines.append("Between efforts " + ("jog or walk" if sport == "running" else "spin") +
                     " really easy, so the next one starts fresh.")
    if shape["cool_s"]:
        lines.append(f"Cool down {minutes(shape['cool_s'])} easy.")
    return lines
