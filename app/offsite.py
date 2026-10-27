"""Encrypted off-site backups to S3-compatible storage (e.g. Cloudflare R2 EU, Infomaniak, Backblaze EU).

Each backup is a consistent database snapshot, gzip-compressed and encrypted with
BACKUP_ENCRYPTION_KEY (Fernet: AES-128 + HMAC) *before* it leaves the server, so the storage
provider only ever sees ciphertext. Backups older than BACKUP_KEEP_DAYS are deleted.

Settings: BACKUP_S3_ENDPOINT, BACKUP_S3_BUCKET, BACKUP_S3_ACCESS_KEY_ID, BACKUP_S3_SECRET_ACCESS_KEY,
BACKUP_S3_REGION (default "auto"), BACKUP_ENCRYPTION_KEY, BACKUP_KEEP_DAYS (default 14).
Create a key with:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

import gzip
import os
from datetime import datetime, timedelta, timezone

from . import backup

PREFIX = "vibegrind/"
SUFFIX = ".sqlite3.gz.enc"


class OffsiteError(RuntimeError):
    pass


def configured() -> bool:
    names = ("BACKUP_S3_ENDPOINT", "BACKUP_S3_BUCKET", "BACKUP_S3_ACCESS_KEY_ID",
             "BACKUP_S3_SECRET_ACCESS_KEY", "BACKUP_ENCRYPTION_KEY")
    return all(os.environ.get(n) for n in names)


def keep_days() -> int:
    return int(os.environ.get("BACKUP_KEEP_DAYS", "14"))


def _fernet():
    from cryptography.fernet import Fernet
    try:
        return Fernet(os.environ["BACKUP_ENCRYPTION_KEY"].encode())
    except (KeyError, ValueError) as e:
        raise OffsiteError("BACKUP_ENCRYPTION_KEY is missing or not a valid key.") from e


def _client():
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=os.environ["BACKUP_S3_ENDPOINT"],
        aws_access_key_id=os.environ["BACKUP_S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["BACKUP_S3_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("BACKUP_S3_REGION", "auto"),
    )


def encrypt(raw: bytes) -> bytes:
    return _fernet().encrypt(gzip.compress(raw))


def decrypt(blob: bytes) -> bytes:
    from cryptography.fernet import InvalidToken
    try:
        return gzip.decompress(_fernet().decrypt(blob))
    except InvalidToken as e:
        raise OffsiteError("Couldn't decrypt the backup: wrong BACKUP_ENCRYPTION_KEY?") from e


def run(now: datetime | None = None, client=None) -> str:
    """Upload one encrypted backup and prune old ones. Returns the object key."""
    if not configured():
        raise OffsiteError("Off-site backups aren't configured.")
    now = now or datetime.now(timezone.utc)
    client = client or _client()
    bucket = os.environ["BACKUP_S3_BUCKET"]
    snap = backup.snapshot()
    try:
        blob = encrypt(snap.read_bytes())
    finally:
        snap.unlink(missing_ok=True)
    key = f"{PREFIX}{now.strftime('%Y-%m-%dT%H%MZ')}{SUFFIX}"
    try:
        client.put_object(Bucket=bucket, Key=key, Body=blob)
        _prune(client, bucket, now - timedelta(days=keep_days()), keep=key)
    except Exception as e:  # botocore raises many types; report them uniformly
        raise OffsiteError(f"Upload to backup storage failed ({type(e).__name__}).") from e
    return key


def list_backups(client=None) -> list[str]:
    client = client or _client()
    resp = client.list_objects_v2(Bucket=os.environ["BACKUP_S3_BUCKET"], Prefix=PREFIX)
    return sorted(o["Key"] for o in resp.get("Contents", []) if o["Key"].endswith(SUFFIX))


def _prune(client, bucket: str, cutoff: datetime, keep: str) -> None:
    for key in list_backups(client):
        stamp = key[len(PREFIX):-len(SUFFIX)]
        try:
            when = datetime.strptime(stamp, "%Y-%m-%dT%H%MZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue  # not ours; leave it alone
        if when < cutoff and key != keep:
            client.delete_object(Bucket=bucket, Key=key)
