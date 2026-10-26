"""Set a new password for an account (for when someone forgets theirs).

Locally:      .venv/bin/python scripts/reset_password.py someone@example.com
On Render:    open the service's Shell tab and run  python scripts/reset_password.py someone@example.com
"""

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.auth import hash_password  # noqa: E402


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    user = db.get_user_by_email(sys.argv[1].strip().lower())
    if user is None:
        sys.exit(f"No account with the email {sys.argv[1]}")
    password = getpass.getpass("New password (min. 8 characters): ")
    if len(password) < 8:
        sys.exit("Password too short.")
    if getpass.getpass("Repeat it: ") != password:
        sys.exit("The passwords don't match.")
    db.set_password(user["id"], hash_password(password))
    print(f"Password updated for {user['email']}. Send it to them privately.")


if __name__ == "__main__":
    main()
