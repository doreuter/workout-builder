"""Background housekeeping: data retention clean-up and the nightly off-site backup.

A single daemon thread checks every 15 minutes what's due. It runs on hosted servers (or with
MAINTENANCE=1), so local development and tests stay quiet. Job state lives in the `jobs` table,
so restarts don't cause duplicate backups.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from . import config, db, offsite

logger = logging.getLogger("vibegrind.maintenance")

USAGE_RETENTION = timedelta(days=396)  # 13 months, as stated in the privacy policy
BACKUP_EVERY = timedelta(hours=24)
CHECK_EVERY_S = 15 * 60

_started = False


def enabled() -> bool:
    return os.environ.get("MAINTENANCE", "1" if config.hosted() else "0") == "1"


def purge(now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    db.purge_old_data(
        usage_before=(now - USAGE_RETENTION).strftime("%Y-%m-%d"),
        resets_before=now.isoformat(),
    )


def backup_if_due(now: datetime | None = None) -> str | None:
    """Run the off-site backup when the last successful one is older than a day."""
    if not offsite.configured():
        return None
    now = now or datetime.now(timezone.utc)
    state = db.job_state("offsite_backup")
    if state and state["last_ok_at"] and now - datetime.fromisoformat(state["last_ok_at"]) < BACKUP_EVERY:
        return None
    try:
        key = offsite.run(now)
    except offsite.OffsiteError as e:
        db.set_job_state("offsite_backup", now.isoformat(), ok=False, error=str(e))
        logger.error("Off-site backup failed: %s", e)
        return None
    db.set_job_state("offsite_backup", now.isoformat(), ok=True, error=None)
    logger.info("Off-site backup stored as %s", key)
    return key


def tick() -> None:
    try:
        purge()
        backup_if_due()
    except Exception:  # never let housekeeping crash the thread
        logger.exception("Maintenance run failed")


def start() -> None:
    global _started
    if _started or not enabled():
        return
    _started = True

    def loop():
        stop = threading.Event()
        while True:
            tick()
            stop.wait(CHECK_EVERY_S)

    threading.Thread(target=loop, name="maintenance", daemon=True).start()
