"""Read Garmin's recovery time from an original activity file (.fit, or the .zip Garmin wraps it in).

Garmin devices write a summary message at the end of each activity that the public FIT profile doesn't
document (global message 140). Field 4 is the aerobic training effect x10 and field 20 the anaerobic one,
which match Garmin Connect exactly; field 9 is the recovery time in minutes, counted from the message's
timestamp (field 253). Checked against a watch and an Edge: counting down from it gives the number the
device shows. Because Garmin can change undocumented fields, everything here fails soft: anything
unexpected returns None and the app simply doesn't show a recovery time.
"""

from __future__ import annotations

import io
import struct
import zipfile
from datetime import datetime, timedelta, timezone

SUMMARY_MESSAGE = 140
RECOVERY_FIELD = 9
TIMESTAMP_FIELD = 253
FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)
MAX_RECOVERY_MIN = 7 * 24 * 60  # Garmin caps it at 4 days; anything far above is a misread


def _fit_bytes(data: bytes) -> bytes | None:
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".fit")]
            return z.read(names[0]) if names else None
    return data


def _summary_fields(fit: bytes) -> dict[int, int] | None:
    """The integer fields of the first message 140, or None."""
    if len(fit) < 12 or fit[8:12] != b".FIT":
        return None
    header = fit[0]
    end = min(len(fit), header + struct.unpack("<I", fit[4:8])[0])
    pos, defs = header, {}
    while pos < end:
        h = fit[pos]
        pos += 1
        if h & 0x80:  # compressed-timestamp data message
            local = (h >> 5) & 0x3
        elif h & 0x40:  # definition message
            local, has_dev = h & 0xF, h & 0x20
            endian = ">" if fit[pos + 1] else "<"
            gnum = struct.unpack(endian + "H", fit[pos + 2:pos + 4])[0]
            count = fit[pos + 4]
            pos += 5
            fields = [(fit[pos + 3 * i], fit[pos + 3 * i + 1]) for i in range(count)]
            pos += 3 * count
            dev_size = 0
            if has_dev:
                n = fit[pos]
                pos += 1
                dev_size = sum(fit[pos + 3 * i + 1] for i in range(n))
                pos += 3 * n
            defs[local] = (gnum, endian, fields, dev_size)
            continue
        else:
            local = h & 0xF
        gnum, endian, fields, dev_size = defs[local]
        if gnum != SUMMARY_MESSAGE:
            pos += sum(size for _, size in fields) + dev_size
            continue
        values = {}
        for num, size in fields:
            raw = fit[pos:pos + size]
            pos += size
            if size in (2, 4):
                values[num] = struct.unpack(endian + ("H" if size == 2 else "I"), raw)[0]
        return values
    return None


def recovery(data: bytes) -> tuple[int, datetime] | None:
    """(recovery minutes, when the countdown starts, UTC) from an activity file, or None."""
    try:
        fit = _fit_bytes(data)
        fields = _summary_fields(fit) if fit else None
    except (KeyError, IndexError, struct.error, zipfile.BadZipFile):
        return None
    if not fields:
        return None
    minutes, stamp = fields.get(RECOVERY_FIELD), fields.get(TIMESTAMP_FIELD)
    if minutes is None or stamp is None or minutes > MAX_RECOVERY_MIN:  # 0xFFFF = no value
        return None
    return minutes, FIT_EPOCH + timedelta(seconds=stamp)
