# Hello New Home HQ: moving the database from SQLite to Neon Postgres

The live service keeps its database in one SQLite file, `/var/data/db/hq.db`, on the Render disk. This note is the cutover to a Neon Postgres database, step by step, with who does what. Nothing about the site changes for visitors, partners or head office: the same pages, the same logins, the same leads. What changes is where the rows live.

Revised 20 October 2026: the live service turned out to run in Render's Oregon region, so the switch also moves the app to a new service in Frankfurt (sections 5 to 7). The Oregon service keeps serving the site on its SQLite file until the domain moves, and it is the rollback: nothing on it is changed. The new service gets its own disk, and the uploads (partner photos and resource files) are copied across in step 5.4.

Plan on about an hour, at a quiet time (early morning is best: fewer leads arrive while the data is being copied). Steps 1 to 3 can be done any day beforehand.

Who does what: **Martin** does everything in the Neon console and the Render dashboard, because those accounts are his. **Claude** prepares the code (the `postgres` branch), reads output when asked, and copies any stray rows at the end. Neither of us ever pastes a connection string into the repository, a chat message or a document; it lives in Render and in the Neon console only.

## 1. Create the Neon project (Martin, 5 minutes)

1. Go to https://console.neon.tech and sign in with the same account that holds the Property Predator and Key Properties projects.
2. New project. Name: `hellonewhome-hq`. Postgres version: the default (16 or newer). Region: **AWS Europe (London)**, `aws-eu-west-2`, the same as the other two businesses, unless told otherwise.
3. Leave the database name as `neondb` and the role as the default. Create.
4. Checked by Martin on 20 October 2026: the `hellonewhome-hq` service is in Render's Oregon region (the blueprint set none, so it took Render's default). That is why section 5 creates a new service in Frankfurt instead of pointing the Oregon one at Neon: Oregon to London is about 140 ms a query and a page runs several. The Neon project stays in London so the data stays in the UK; Frankfurt to London is about 10 to 15 ms.

## 2. Copy the two connection strings (Martin, 2 minutes)

1. Neon console → the project → Dashboard → Connect (or "Connection details").
2. Neon shows a **pooled** string (host contains `-pooler`) and a **direct** string (same host without `-pooler`). Use the toggle to see each. Both start `postgresql://` and end `?sslmode=require` (newer consoles add `&channel_binding=require`, which is fine).
3. Copy both into your password manager under "Hello New Home Neon", labelled pooled and direct. They contain the database password; treat them as such.
4. Which one is used where: the **direct** string is used for the copy, for backups, and for `DATABASE_URL` in Render. The pooled string is kept for the day the app runs more than one instance; the HQ app is one process with its own small connection pool and does not need the pooler.

## 3. Deploy the code that knows about Postgres, still on SQLite (Claude, then Martin, 10 minutes)

1. First, Martin: Render → Blueprints → the Hello New Home blueprint → Settings → turn Auto-Sync off [VERIFY the setting's name on the day]. The `postgres` branch changes `render.yaml` to a Frankfurt service with a new name, and with Auto-Sync on the merge in the next step would create that service by itself, before its secrets exist. Code deploys of the Oregon service are a separate setting and keep working.
2. Claude merges the `postgres` branch into `main` after review and tells Martin the commit SHA.
3. Martin: Render → the service → Manual Deploy → Deploy latest commit (or confirm the automatic deploy picked up that SHA under Events).
4. Nothing is different yet: `DATABASE_URL` is not set, so the app boots on the SQLite file exactly as before. This step puts `scripts/copy_sqlite_to_pg.py` and the Alembic files on the live service so the Render shell can run them, and it is also the boot that brings the live file under Alembic. Martin: open Logs for that boot. They must show `[migrations] schema stamped: no revision -> 0001_baseline` (once; later boots print nothing) and must not show `could not create ux_users_email_lower`. If that second line is there, stop and tell Claude: two logins share an address in different cases, and step 4 would be refused until one of them is changed.
5. `DATABASE_URL` is never set on the Oregon service. With Auto-Sync off nothing asks for it; if Render shows it under Environment as waiting for a value, leave it empty: blank means SQLite, the same as no variable at all.
6. Martin notes three numbers from the admin panel for the comparison later: the count on Admin → Leads with no filter, the number of partners on Admin → Partners, and the number of admin users on Admin → Users.

## 4. Copy the data from a Render shell (Martin, with Claude reading the output, 15 minutes)

Do not create the Frankfurt service (section 5) before this step is finished. If the app ever boots against an empty Neon database it will seed it (areas, resource library, admin account), and the copy would then clash with those rows.

The service stays up on SQLite while the copy runs (a Render shell lives on the running instance, so suspending the service would close the shell as well). That leaves a window, from the moment the copy finishes until the domain move in section 7, in which anything written still goes to the SQLite file. New leads in that window are picked up in section 8. Changes to rows that already exist are not: a lead marked contacted, a password change, an edit to a partner's profile, a settings change. So, for the twenty minutes or so between step 4.5 and the domain move in section 7: nobody at head office touches the admin panel, and Ina and any other partner are told beforehand not to use the portal until Martin says so. Whether anything slipped through is checked in section 8.

1. Render → the service → Shell. A terminal opens on the live instance, with the disk mounted, so `/var/data/db/hq.db` is right there.
2. Put the **direct** connection string into a variable for this shell session only. Type `export NEON_DIRECT_URL='` then paste the string, then type `'` and press Enter. It is not saved anywhere; closing the shell forgets it. The script reads the variable by name (`--postgres-env NEON_DIRECT_URL`), so the string never appears in a command line, the process list or the shell history.
3. Dry run first. It reads the SQLite file, connects to Neon, and reports what it would copy per table without writing anything:

   ```
   python scripts/copy_sqlite_to_pg.py --sqlite /var/data/db/hq.db --postgres-env NEON_DIRECT_URL --dry-run
   ```

   If it stops with "No module named ..." the shell started from a build older than the code; run `pip install -r requirements.txt` and try again. Any other error: copy the last twenty lines to Claude and stop here. Nothing has been written.
4. Check the dry-run counts against the numbers from step 3.6. Leads may be one or two higher if someone has enquired in the meantime; partners and users must match exactly.
5. The real copy:

   ```
   python scripts/copy_sqlite_to_pg.py --sqlite /var/data/db/hq.db --postgres-env NEON_DIRECT_URL
   ```

   It creates the tables, copies every row, keeps every id, and resets the Postgres id sequences so the next lead, partner or user gets a fresh number. It does not touch the SQLite file.
6. If it fails part-way, the Neon database is in an unknown state. Start again cleanly: Neon console → the project → SQL Editor → run `DROP SCHEMA public CASCADE; CREATE SCHEMA public;` (this is only safe now, while nothing is using the database), then repeat from step 4.3.
7. Note the time the copy finished and, from the table the script printed, the `source` count and the `highest id` for `leads` and for `audit_log`. Any lead that arrives between now and the domain move lands in SQLite, not Neon; section 8 picks those up. Go straight on to section 5 without closing the shell.

## 5. Create the Frankfurt service, on Neon from its first boot (Martin, 20 minutes; the Oregon service keeps serving the site)

The `postgres` branch carries the Blueprint change: `render.yaml` now names the service `hellonewhome-hq-eu` with `region: frankfurt`. Render cannot change an existing service's region and service names are unique, so the new name is what makes the Blueprint create a second service instead of failing on the first. The Oregon service drops out of the Blueprint when the file no longer lists it; Render does not delete a service that way, it only stops managing it [VERIFY in the sync preview on the day: it must say the Oregon service is no longer managed, never that it will be deleted; if the preview offers to delete it, cancel and tell Claude].

1. Render → Blueprints → the blueprint → Manual Sync. Render lists `hellonewhome-hq-eu` as new and asks for the two `sync: false` values: `DATABASE_URL` = the **direct** Neon string from step 2, and `ADMIN_PASSWORD` = the value the Oregon service has (Render → old service → Environment shows it). Apply. The first deploy starts.
2. Straight away: new service → Environment → `SECRET_KEY`. Replace the value Render generated with the Oregon service's value (copy it from the old service's Environment page). Save; Render redeploys. This is what keeps every partner and admin session valid across the move. Nothing else is copied by hand; the Blueprint set the rest.
3. Logs for that boot: `[migrations] ...` lines are expected; `[seed] Admin account created` must NOT appear, because the copied database already holds the admin. If it appears the string is wrong (another project, or a typo): fix `DATABASE_URL`, then drop and recreate the Neon schema (step 4.6) and copy again, because that boot seeded the wrong database.
4. Copy the uploads from the Oregon disk to the Frankfurt disk. Each service's Shell tab shows its `ssh` command; add the office machine's public key once under Account Settings → SSH Keys. Then, from the office machine, with the two targets as the Shell tabs print them:

   ```
   rsync -avz --progress <oregon ssh target>:/var/data/uploads/ ./hnh-uploads/
   rsync -avz --progress ./hnh-uploads/ <frankfurt ssh target>:/var/data/uploads/
   ```

   [VERIFY: Render documents scp and rsync over its SSH for persistent disks; if rsync is missing on the instance, the same copy is `ssh <oregon> 'tar -C /var/data -cf - uploads' | ssh <frankfurt> 'tar -C /var/data -xf -'`.] Then `find /var/data/uploads -type f | wc -l` in both Shell tabs; the counts must match.

## 6. Verify the Frankfurt service before it gets the domain (Martin, 10 minutes)

Use the new service's `.onrender.com` address (top of its page in Render) in place of hellonewhome.co.uk for every check here. The live site is still the Oregon service and is untouched.

1. `/healthz` shows `{"ok":true}`. This proves the process is up; it deliberately does no database work, so the next checks matter more.
2. `/areas/somerset` loads with Martin's name and WhatsApp number, and `/areas/london` with Ina's. These are read from Neon.
3. `/api/locate?postcode=TA1%201AA` returns `"covered": true` with `"slug": "somerset"`, and `/api/locate?postcode=SE1%207PB` returns London.
4. Admin login at `/admin/login` with the usual password (the users table is the one being read). Admin → Leads, Partners and Users: the counts equal the dry-run numbers from step 4.4.
5. Open a lead from before the move: the same date and time as on the old site (times are stored in UTC and shown in UK time; a wrong offset means the copy changed the timestamps, which is a rollback).
6. Admin → Resources: open a resource file, and Partners → Martin Howard: the photo shows. That proves the uploads copy.
7. A test lead from the new address's home page, named `Neon test lead`, a real Somerset postcode and your own number. Admin → Leads: at the top, routed to Somerset, with an id higher than the last copied lead's id (a duplicate-key error here means the sequences were not reset; stop and tell Claude). Delete nothing; set its status to lost.

## 7. Move the domain (Martin, 10 minutes; the only moment visitors could notice)

1. An hour or more beforehand, wherever DNS for hellonewhome.co.uk lives, lower the TTL on the apex and `www` records to 300.
2. Render → old service → Settings → Custom Domains → remove `hellonewhome.co.uk` and `www.hellonewhome.co.uk`.
3. Render → new service → Settings → Custom Domains → add both. Render shows the records it wants: an A record for the apex (Render's address; if the DNS host already has `216.24.57.1` it is unchanged) and `www` as a CNAME to the new service's `.onrender.com` name. Change the `www` CNAME at the DNS host; leave the apex alone if it already matches what Render shows [VERIFY on the Custom Domains page: the address Render prints on the day wins]. If DNS is on Cloudflare with the orange cloud on, stop and tell Claude before changing anything: the proxy needs SSL/TLS set to Full (strict) and the CNAME handled differently.
4. Wait until Render shows both domains verified and the certificate issued (a few minutes).
5. Repeat checks 6.1 to 6.3 on `https://hellonewhome.co.uk`. The Oregon service is now idle but still running on its SQLite file; leave it alone until section 10.
6. Ask Ina to open `/partners` on her phone: her existing session should still work (step 5.2), and a fresh login should too. Her leads list must show the same leads as before.

## 8. Anything that arrived during the switch (Martin, with Claude, 5 minutes)

The shell from step 4 is still open on the Oregon instance (if it has closed, open a new one on the **old** service and export `NEON_DIRECT_URL` again as in step 4.2; the disk and the SQLite file are still there).

1. Count what the SQLite file holds now against the numbers noted in step 4.7:

   ```
   python -c "import sqlite3; c = sqlite3.connect('file:/var/data/db/hq.db?mode=ro', uri=True); print('leads', c.execute('select count(*), max(id) from leads').fetchone()); print('audit', c.execute('select count(*), max(id) from audit_log').fetchone())"
   ```

2. If the leads count equals the one noted in step 4.7, no enquiry arrived in the window. If it is higher, copy the new rows across, with `N` the `highest id` the script printed for `leads` in step 4.7:

   ```
   python scripts/copy_sqlite_to_pg.py --sqlite /var/data/db/hq.db --postgres-env NEON_DIRECT_URL --only leads --after-id N --append
   ```

   It copies only the leads with an id above `N`, resets the sequence again and prints the counts. They are usually zero or one; head office must respond to them like any other lead.
3. If the audit count is higher than in step 4.7, someone did something in the window despite the request in step 4 (the audit log records every login, status change, edit and password change). Print the new rows so the change can be made again on Neon by hand, then copy the audit entries themselves the same way:

   ```
   python -c "import sqlite3; c = sqlite3.connect('file:/var/data/db/hq.db?mode=ro', uri=True); [print(r) for r in c.execute('select id, user_id, action, target, created_at from audit_log where id > M order by id')]"
   python scripts/copy_sqlite_to_pg.py --sqlite /var/data/db/hq.db --postgres-env NEON_DIRECT_URL --only audit_log --after-id M --append
   ```

   with `M` the `highest id` printed for `audit_log` in step 4.7. A login entry needs nothing; a status change, note or profile edit is repeated in the admin panel; a password change means the person is asked to change it once more (their old password is what Neon holds).

## 9. Rollback (Martin, 5 minutes)

The Oregon service is still running on the SQLite file, which was never modified, so going back is the domain moving back.

1. Render → new service → Custom Domains → remove both names; old service → Custom Domains → add both; at the DNS host point `www` back at the old `.onrender.com` name. Wait for verification.
2. Verify with checks 6.1 to 6.3 on `https://hellonewhome.co.uk`.
3. Any lead or edit made while the domain pointed at Frankfurt is in Neon, not in the file. Tell Claude how long that was; those rows are copied back by hand before a second attempt.
4. The Frankfurt service and the Neon project can stay. A second attempt starts again from step 4.6 (drop and recreate the schema), then the copy, then a Manual Deploy of the Frankfurt service, then sections 6 and 7 again.

## 10. Afterwards (Martin)

1. Render → old service → Settings → Suspend, the day after the move once section 8 is done. Keep it suspended for two weeks (a suspended service is not billed for compute; the disk is [VERIFY on the invoice]). Then Delete it. Its disk goes with it, so the SQLite file is gone at that point; if you want a keepsake, first `scp <oregon ssh target>:/var/data/db/hq.db ./hq-oregon-final.db` from the office machine.
2. Render → Blueprints: after the deletion the blueprint lists only `hellonewhome-hq-eu`, which matches the file. Turn Auto-Sync back on.
3. Neon console → the project → Settings → Compute: leave scale-to-zero on. The first request after an idle spell takes about half a second longer while the compute wakes, which is fine for this site; the health check does not touch the database, so Render never sees the wake-up as a failure.
4. The Frankfurt service's `.onrender.com` name is new; nothing links to the old one, but the `LAUNCH-STEPS.md` example addresses are now historical.

## 11. Weekly backup and a restore drill (Martin, 10 minutes a week)

Neon keeps its own restore window (its length depends on the plan), which covers an accidental delete the same week. A dump you hold yourself is the backup that does not depend on the supplier, and it is what you would use to move again.

1. One-off: install the PostgreSQL client tools, version 16 or newer, on the office machine (Windows: the EnterpriseDB installer with only "Command Line Tools" ticked; Mac: `brew install libpq`). `pg_dump --version` must show a version at least as high as the Neon server's.
2. Weekly (Monday morning), in a terminal, with the **direct** string pasted into `NEON_DIRECT_URL` for that session only:

   ```
   pg_dump "$NEON_DIRECT_URL" --format=custom --no-owner --no-privileges -f hnh-2026-10-27.dump
   ```

   Name the file with the day's date. Put it in the usual backups folder (the one that is synced off the machine). Keep eight weeks of dumps and delete older ones.
3. Restore drill, once a quarter, so the backup is known to work rather than hoped to. Neon console → the project → Branches → create a branch called `restore-drill` from the current state, open its connection string, then:

   ```
   pg_restore --clean --if-exists --no-owner --no-privileges -d "$RESTORE_URL" hnh-2026-10-27.dump
   ```

   Connect to the branch in the Neon SQL Editor and run `select count(*) from leads;` and `select count(*) from partners;`. The numbers must match the dump date's admin panel. Delete the branch when done. Never run the restore against the main branch unless you mean to replace the live data.
4. Automated version: the repository's `.github/workflows/backup-db.yml` (on the `partner-api` branch, merged after this cutover) runs the same dump every Monday to a private Cloudflare R2 bucket and restores it into a throw-away database on the first of each month, driven by repository secrets; the setup is in Group HQ's `docs` folder. Once it is green, the weekly dump by hand is a belt-and-braces extra, not a chore.
5. If the office machine cannot run `pg_dump`, the fallback is the Neon console's own backup (Branches → create a dated branch each week); it stays inside Neon, so it is a second copy, not an off-site one.
