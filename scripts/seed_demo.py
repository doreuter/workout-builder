"""Create a local demo account with two sample workouts (no Claude API key needed).

    .venv/bin/python scripts/seed_demo.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.workout import validate  # noqa: E402

DEMO_EMAIL = "demo@example.com"
DEMO_PASSWORD = "demo-password"

WORKOUTS = [
    {
        "name": "4×8 Threshold",
        "sport": "cycling",
        "description": "Four 8-minute threshold efforts with 3 minutes easy between.",
        "steps": [
            {"type": "warmup", "duration_s": 900, "intensity": 0.5, "intensity_end": 0.75},
            {"type": "steady", "duration_s": 60, "intensity": 1.1, "label": "Opener"},
            {"type": "rest", "duration_s": 240, "intensity": 0.55},
            {"type": "repeat", "count": 4, "steps": [
                {"type": "steady", "duration_s": 480, "intensity": 0.98, "cadence": 90, "label": "Threshold"},
                {"type": "rest", "duration_s": 180, "intensity": 0.55},
            ]},
            {"type": "cooldown", "duration_s": 600, "intensity": 0.6, "intensity_end": 0.4},
        ],
    },
    {
        "name": "5×4 VO2 max",
        "sport": "cycling",
        "description": "Five 4-minute VO2 max efforts with equal recovery.",
        "steps": [
            {"type": "warmup", "duration_s": 900, "intensity": 0.5, "intensity_end": 0.75},
            {"type": "repeat", "count": 5, "steps": [
                {"type": "steady", "duration_s": 240, "intensity": 1.15, "label": "VO2 max"},
                {"type": "rest", "duration_s": 240, "intensity": 0.5},
            ]},
            {"type": "cooldown", "duration_s": 600, "intensity": 0.6, "intensity_end": 0.4},
        ],
    },
]


def main():
    db.init_db()
    user = db.get_user_by_email(DEMO_EMAIL)
    user_id = user["id"] if user else db.create_user(DEMO_EMAIL, hash_password(DEMO_PASSWORD))
    db.update_profile(user_id, 250, None, 188)
    for w in WORKOUTS:
        wid = db.create_workout(user_id)
        db.save_workout(user_id, wid, validate(w), [])
    print(f"Seeded {len(WORKOUTS)} workouts for {DEMO_EMAIL}")


if __name__ == "__main__":
    main()
