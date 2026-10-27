"""Download and decrypt an off-site backup, e.g. to restore after losing the server.

    .venv/bin/python scripts/fetch_backup.py            # list backups
    .venv/bin/python scripts/fetch_backup.py latest     # download + decrypt the newest
    .venv/bin/python scripts/fetch_backup.py <key>      # a specific one

Needs the same BACKUP_* settings as the server (in .env or the environment). The result is a
.sqlite3 file you can upload under Settings → Backup → Restore on a fresh server.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import offsite  # noqa: E402  (importing app also loads .env)


def main():
    if not offsite.configured():
        sys.exit("Set BACKUP_S3_ENDPOINT, BACKUP_S3_BUCKET, BACKUP_S3_ACCESS_KEY_ID, "
                 "BACKUP_S3_SECRET_ACCESS_KEY and BACKUP_ENCRYPTION_KEY first.")
    client = offsite._client()
    keys = offsite.list_backups(client)
    if len(sys.argv) == 1:
        print("\n".join(keys) or "No backups yet.")
        return
    key = keys[-1] if sys.argv[1] == "latest" and keys else sys.argv[1]
    import os
    blob = client.get_object(Bucket=os.environ["BACKUP_S3_BUCKET"], Key=key)["Body"].read()
    out = Path(key.split("/")[-1].replace(offsite.SUFFIX, ".sqlite3"))
    out.write_bytes(offsite.decrypt(blob))
    out.chmod(0o600)
    print(f"Saved {out} ({out.stat().st_size // 1024} KB). Keep it private; delete it after restoring.")


if __name__ == "__main__":
    main()
