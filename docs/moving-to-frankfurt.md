# Moving VibeGrind to Render's Frankfurt region

The current service (`workout-builder`, USA) keeps running until the new one (`vibegrind`, Frankfurt) has
all the data. Users only notice that they have to sign in again.

## A. Take the backup (old service)

1. In Render, check that the deploy "Admin database backup download and guarded one-time restore" is **live**
   on `workout-builder`.
2. Open https://workout-builder-pimy.onrender.com/app, sign in with your admin account.
3. **Settings → Backup → Download backup.** You get `vibegrind-backup-<date>.sqlite3`.
   It contains everyone's accounts (password hashes), workouts and your Garmin data: keep it private.

From here on, changes made on the old service aren't in the backup. Tell your beta users to pause for an hour.

## B. Create the Frankfurt service

4. Claude pushes the current code (it includes `render.yaml` with `name: vibegrind`, `region: frankfurt`).
5. In Render: open your Blueprint and click **Sync**, or **New → Blueprint** and pick the repository again.
   Render creates the service **vibegrind** in Frankfurt and asks for:
   - `ANTHROPIC_API_KEY`: your hosted-app key
   - `ADMIN_EMAILS`: your sign-in email
   - `GARMIN_PERSONAL_API_KEY`: your Garmin key (a fresh one, ideally)
6. In **vibegrind → Environment**, add `ALLOW_RESTORE` = `1` and save.
7. Wait until the deploy is **live**, then open the new `vibegrind-….onrender.com/app` address.

## C. Restore (new service)

8. **Create one** with your admin email and any temporary password (no invite code needed).
9. **Settings → Backup → Restore from backup**: choose the backup file, type `RESTORE`, click **Restore**.
   The app checks the file, copies everything over and signs you out.
10. Sign in with your **old** password. Check your workouts, invites and the Readiness tab.
11. In **Environment**, **delete `ALLOW_RESTORE`** and save. The restore box disappears.

## D. Finish

12. Attach `vibegrind.app` to the **new** service: **vibegrind → Settings → Custom Domains**.
13. Delete the old **workout-builder** service in Render (this also deletes its US disk), so no copy of the data
    stays in the USA and you don't pay twice.
14. Delete the downloaded backup file from your computer (or keep it encrypted, e.g. in an encrypted disk image).
