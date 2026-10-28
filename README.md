# VibeGrind

Describe a cycling workout in plain language. Claude turns it into a structured workout, the app draws it as a chart, and you send it to Zwift or download it for Zwift (`.zwo`) or Garmin (`.fit`).

## Run it

```bash
cd workout-builder
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env   # or export it in your shell
.venv/bin/uvicorn app.main:app --reload
```

Open http://localhost:8000 and create an account.

To try the UI without an API key, run `.venv/bin/python scripts/seed_demo.py` and sign in with the demo account defined in that script. It comes with two sample workouts.

Tests: `.venv/bin/pytest`

### Settings (`.env` locally, environment variables when hosted)

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Required for the chat |
| `CLAUDE_MODEL` | Model for the chat. Defaults to `claude-opus-5-5`; `claude-haiku-5-5` is much cheaper |
| `APP_MODE` | `local` (default) or `hosted`. Hosted turns off everything that touches the Zwift folder, requires invites and HTTPS-only cookies |
| `ADMIN_EMAILS` | Comma-separated. These accounts can sign up without an invite and create invite codes (in Settings) |
| `REQUIRE_INVITE` | `1`/`0`; defaults to on when hosted |
| `DAILY_MESSAGE_LIMIT` | Chat messages per user per day (default 30; admins are unlimited) |
| `MONTHLY_BUDGET_USD` | The chat pauses for everyone once the month's estimated Claude cost reaches this (default 10; 0 = off) |
| `WORKOUT_DB` | Path to the SQLite database (default `data/app.db`) |
| `SESSION_SECRET` | Signs login cookies; set it when hosted |
| `ZWIFT_WORKOUTS_DIR` | Zwift folder to use if auto-detection doesn't find it (local mode) |

## Accounts, data and backups

- **Download my data / Delete my account:** in Settings, for every user. Deleting needs the password and
  removes the account with its workouts, chats, usage counters and Garmin data.
- **Retention:** usage counters older than 13 months and spent or expired password-reset links are deleted
  automatically (on start and every 15 minutes on hosted servers).
- **Password reset by email:** "Forgot password?" on the sign-in page appears once email is configured.
  Links are single-use and expire after one hour; only a hash of the token is stored.
- **Encrypted off-site backups:** once a day the database is snapshotted, gzip-compressed and encrypted
  (Fernet) *on the server*, then uploaded to S3-compatible storage; backups older than `BACKUP_KEEP_DAYS`
  are deleted. Admins see the status under Settings → Backup. To get one back:
  `.venv/bin/python scripts/fetch_backup.py latest`, then upload the file under Settings → Restore on a fresh
  server (with `ALLOW_RESTORE=1`).

| Variable | Purpose |
|---|---|
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD` | Any SMTP provider; port 587 = STARTTLS, 465 = TLS |
| `MAIL_FROM` | e.g. `VibeGrind <hello@vibegrind.app>`; must be an address the provider lets you send from |
| `BACKUP_S3_ENDPOINT`, `BACKUP_S3_BUCKET` | S3-compatible storage, e.g. Cloudflare R2 with EU jurisdiction |
| `BACKUP_S3_ACCESS_KEY_ID`, `BACKUP_S3_SECRET_ACCESS_KEY`, `BACKUP_S3_REGION` | Credentials (region defaults to `auto`) |
| `BACKUP_ENCRYPTION_KEY` | Create with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. **Keep a copy somewhere safe: without it the backups can't be read** |
| `BACKUP_KEEP_DAYS` | Default 14 |
| `MAINTENANCE` | Background jobs; on by default when `APP_MODE=hosted` |

## Hosting on Render

`render.yaml` describes the service: a Python web service named `vibegrind` in Render's Frankfurt (EU) region, on the Starter plan with a 1 GB disk at `/var/data` for the database.

1. Push this folder to a **private** GitHub repository. `.env` and `data/` are git-ignored, so your key and database stay local.
2. In Render: **New → Blueprint**, pick the repository. When asked, fill in `ANTHROPIC_API_KEY` (create a new key just for this) and `ADMIN_EMAILS` (your email).
3. Open the `….onrender.com` URL, create your account with your admin email, then create invites under **Settings → Invites**.
4. In the Anthropic Console, set a monthly spend limit as a second safety net.

Forgotten password: in Render, open the service's **Shell** and run `python scripts/reset_password.py their@email`.

The online version can't reach anyone's Zwift folder: people download `.zwo`/`.fit` files and upload `.zwo`/`.fit` files to import. Run the app locally (`APP_MODE=local`) to keep the Zwift folder buttons.

## How it works

```
chat message ──► Claude calls the set_workout tool
                     │
                     ▼
             app/workout.py validates the workout (errors go back to Claude to fix)
                     │
        ┌────────────┼──────────────┐
        ▼            ▼              ▼
   SVG chart     .zwo export     .fit export
```

- **Workout format** (`app/workout.py`): steps of type warmup / cooldown / steady / ramp / rest / free, plus `repeat` blocks. Intensity is a fraction of FTP.
- **Accounts** (`app/auth.py`, `app/db.py`): email and password (scrypt-hashed), signed session cookie, SQLite in `data/app.db`. Each user's profile holds FTP and max HR.
- **Chat history** is stored per workout and only ever appended to, so follow-up edits ("make the intervals longer") keep their context.

## Exports

| | Zwift `.zwo` | Garmin `.fit` |
|---|---|---|
| Format | % FTP; simple on/off repeats become `IntervalsT` | % FTP power ranges (FIT values 0–1000 = % FTP) |
| Install | **Send to Zwift**, or copy to `Documents/Zwift/Workouts/<your Zwift ID>/` | Copy to `GARMIN/NewFiles/` on the watch over USB |

Garmin has no ramps, so ramps and warm-ups become a single low–high range.

## Importing and editing Zwift workouts

**Import workout** (sidebar) lists the `.zwo` files in your Zwift folder, or takes an uploaded Zwift `.zwo` or Garmin `.fit` workout file (e.g. from the watch's `GARMIN/Workouts` folder). Garmin watt targets need your FTP; power-zone and heart-rate targets are converted to approximate % FTP. The imported workout opens in the chat, where you can ask Claude for changes; it keeps Zwift's on-screen messages and tags.

For a workout imported from the Zwift folder, **Update in Zwift** overwrites that file. Before it does, the old version is copied to `data/zwift-backups/`. To undo, copy a backup back into the Zwift folder and rename it.

Zwift-only display attributes (`pace`, `OverUnder`, `FlatRoad`) aren't carried over.

### Send to Zwift from the online app

The hosted server can't reach anyone's computer, but in **Chrome, Edge, Brave and Opera on desktop** the
browser can: the user picks their Zwift folder once (`Documents/Zwift/Workouts/<number>`; picking `Zwift` or
`Workouts` works too when there's one account), and **Send to Zwift** then saves into it directly
(`static/zwift-folder.js`, File System Access API). The folder handle stays in the browser's IndexedDB; the
server never sees it. Earlier exports of ours are replaced, other files never are (they get `_1`, `_2`…).
**Import workout** also lists the workouts in that folder. In Safari and Firefox, and on phones and tablets,
the app shows device-specific instructions for the downloaded files instead.

## Readiness dashboard (Garmin)

The **Readiness** tab shows your Garmin data: overnight HRV against your baseline, resting heart rate, sleep, Body Battery, and training load from your activities, plus 30-day trends and recent activities.

It uses the [Garmin Connect Personal API](https://developer.garmin.com/gc-developer-program/personal/personal-api), which is for **one person's own data** (Garmin: not for apps serving multiple users). So exactly one account sees it:

| Variable | Purpose |
|---|---|
| `GARMIN_PERSONAL_API_KEY` | Your key from Garmin Connect → Settings → Personal API Key. It expires (30 days to 1 year); Garmin emails you before it does |
| `GARMIN_OWNER_EMAIL` | The app account that sees the dashboard. Defaults to the first `ADMIN_EMAILS` address |

The app exchanges the key for a 24-hour token (Garmin's official `garmin-connect-personal-api-python` client), backfills 60 days on the first visit, and then re-syncs at most every 3 hours (or with **Refresh from Garmin**).

**The readiness score is the app's own estimate** (`app/readiness.py`), because the Personal API doesn't include Garmin's Training Readiness or training status. Weights: HRV 25 %, sleep 20 %, Garmin's recovery time 20 %, resting heart rate 10 %, Body Battery 15 %, training load 10 % (7-day vs 28-day load). Recovery time scores 100 at 0 h, 75 at 15 h, 40 at 36 h and 0 at 72 h; while it's over 36 h the whole score is capped at 54. Each part shows how it was scored.

**Recovery time is Garmin's own.** The API's activity summaries don't include it, but the watch or Edge writes
it into each original activity file (an undocumented summary message; `app/fitmeta.py`). On each sync the app
downloads the files of the last 5 days' activities once, keeps only that number and when it starts, and
discards the file. The value is the device's total recovery time at the end of the activity, so it already
includes earlier sessions; the app shows the one that ends last, counted down to now. Traffic light: green
under 15 h, yellow 15–36 h, red over 36 h. The device may still adjust it later (e.g. after sleep); the app
only sees the value at the end of each activity. Today's score uses the hours left right now; past days use the value at about 7:00, and leave recovery out where the activity files weren't read. If Garmin changes the file format, the card disappears
rather than showing a wrong number.

## Not done yet (v2)

- **Running.** It's switched off with `ENABLED_SPORTS` in `app/workout.py`. The running format, the `.zwo`/`.fit` running exports and their tests are still in the code. Running workouts created earlier stay in the database but aren't listed. To bring running back, add it to `ENABLED_SPORTS`, restore the threshold-pace setting and the running parts of the prompt, and add running examples to the UI.
- Push to Zwift or Garmin through a connector (for example intervals.icu)
- Streaming chat replies
- Password reset / email verification
- Heart-rate–based targets
