import os
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Read KEY=value lines from .env without overriding variables already set."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


# Loaded before any other module reads settings (e.g. the database path).
_load_dotenv(Path(__file__).resolve().parent.parent / ".env")
