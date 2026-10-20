# Hello New Home HQ: moving the database from SQLite to Neon Postgres

The live service keeps its database in one SQLite file, `/var/data/db/hq.db`, on the Render disk. This note is the cutover to a Neon Postgres database, step by step, with who does what. Nothing about the site changes for visitors, partners or head office: the same pages, the same logins, the same leads. What changes is where the rows live.

What stays on the Render disk: uploads (partner photos and resource files) under `/var/data/uploads`, and the SQLite file itself, which is left untouched as the rollback copy.

Plan on about an hour, at a quiet time (early morning is best: fewer leads arrive while the data is being copied). Steps 1 to 3 can be done any day beforehand.

Who does what: **Martin** does everything in the Neon console and the Render dashboard, because those accounts are his. **Claude** prepares the code (the `postgres` branch), reads output when asked, and copies any stray rows at the end. Neither of us ever pastes a connection string into the repository, a chat message or a document; it lives in Render and in the Neon console only.

## 1. Create the Neon project (Martin, 5 minutes)

1. Go to https://console.neon.tech and sign in with the same account that holds the Property Predator and Key Properties projects.
2. New project. Name: `hellonewhome-hq`. Postgres version: the default (16 or newer). Region: **AWS Europe (London)**, `aws-eu-west-2`, the same as the other two businesses, unless told otherwise.
3. Leave the database name as `neondb` and the role as the default. Create.
4. One check while you are in Render: open the `hellonewhome-hq` service → Settings → Region. The blueprint does not set a region, so it is whatever was chosen when the service was created (Render's default is Oregon). If it says Oregon, stop and tell Claude before the cutover: every page would then make a round trip across the Atlantic for each database query, and the right fix is a new Render service in Frankfurt, which is a separate decision. Frankfurt or any European region is fine to carry on.

## 2. Copy the two connection strings (Martin, 2 minutes)

1. Neon console → the project → Dashboard → Connect (or "Connection details").
2. Neon shows a **pooled** string (host contains `-pooler`) and a **direct** string (same host without `-pooler`). Use the toggle to see each. Both start `postgresql://` and end `?sslmode=require` (newer consoles add `&channel_binding=require`, which is fine).
3. Copy both into your password manager under "Hello New Home Neon", labelled pooled and direct. They contain the database password; treat them as such.
4. Which one is used where: the **direct** string is used for the copy, for backups, and for `DATABASE_URL` in Render. The pooled string is kept for the day the app runs more than one instance; the HQ app is one process with its own small connection pool and does not need the pooler.

## 3. Deploy the code that knows about Postgres, still on SQLite (Claude, then Martin, 10 minutes)

1. Claude merges the `postgres` branch into `main` after review and tells Martin the commit SHA.
2. Martin: Render → the service → Manual Deploy → Deploy latest commit (or confirm the automatic deploy picked up that SHA under Events).
3. Nothing is different yet: `DATABASE_URL` is not set, so the app boots on the SQLite file exactly as before. This step puts `scripts/copy_sqlite_to_pg.py` and the Alembic files on the live service so the Render shell can run them, and it is also the boot that brings the live file under Alembic. Martin: open Logs for that boot. They must show `[migrations] schema stamped: no revision -> 0001_baseline` (once; later boots print nothing) and must not show `could not create ux_users_email_lower`. If that second line is there, stop and tell Claude: two logins share an address in different cases, and step 4 would be refused until one of them is changed.
4. The blueprint now lists `DATABASE_URL` as `sync: false`, so Render may show it under Environment as a variable waiting for a value, and a Blueprint sync may ask for it. Leave it unset until step 5. If a sync will not finish without a value, save it empty: a blank value means SQLite, the same as no variable at all.
5. Martin notes three numbers from the admin panel for the comparison later: the count on Admin → Leads with no filter, the number of partners on Admin → Partners, and the number of admin users on Admin → Users.

## 4. Copy the data from a Render shell (Martin, with Claude reading the output, 15 minutes)

Do not set `DATABASE_URL` before this step is finished. If the app ever boots against an empty Neon database it will seed it (areas, resource library, admin account), and the copy would then clash with those rows.

The service stays up on SQLite while the copy runs (a Render shell lives on the running instance, so suspending the service would close the shell as well). That leaves a window, from the moment the copy finishes until the switch in step 5 takes effect, in which anything written still goes to the SQLite file. New leads in that window are picked up in step 7. Changes to rows that already exist are not: a lead marked contacted, a password change, an edit to a partner's profile, a settings change. So, for the ten minutes or so between step 4.5 and step 5.3: nobody at head office touches the admin panel, and Ina and any other partner are told beforehand not to use the portal until Martin says so. Whether anything slipped through is checked in step 7.

1. Render → the service → Shell. A terminal opens on the live instance, with the disk mounted, so `/var/data/db/hq.db` is right there.
2. Put the **direct** connection string into a variable for this shell session only. Type `export NEON_DIRECT_URL='` then paste the string, then type `'` and press Enter. It is not saved anywhere; closing the shell forgets it. The script reads the variable by name (`--postgres-env NEON_DIRECT_URL`), so the string never appears in a command line, the process list or the shell history.
3. Dry run first. It reads the SQLite file, connects to Neon, and reports what it would copy per table without writing anything:

   ```
   python scripts/copy_sqlite_to_pg.py --sqlite /var/data/db/hq.db --postgres-env NEON_DIRECT_URL --dry-run
   ```

   If it stops with "No module named ..." the shell started from a build older than the code; run `pip install -r requirements.txt` and try again. Any other error: copy the last twenty lines to Claude and stop here. Nothing has been written.
4. Check the dry-run counts against the numbers from step 3.5. Leads may be one or two higher if someone has enquired in the meantime; partners and users must match exactly.
5. The real copy:

   ```
   python scripts/copy_sqlite_to_pg.py --sqlite /var/data/db/hq.db --postgres-env NEON_DIRECT_URL
   ```

   It creates the tables, copies every row, keeps every id, and resets the Postgres id sequences so the next lead, partner or user gets a fresh number. It does not touch the SQLite file.
6. If it fails part-way, the Neon database is in an unknown state. Start again cleanly: Neon console → the project → SQL Editor → run `DROP SCHEMA public CASCADE; CREATE SCHEMA public;` (this is only safe now, while nothing is using the database), then repeat from step 4.3.
7. Note the time the copy finished and, from the table the script printed, the `source` count and the `highest id` for `leads` and for `audit_log`. Any lead that arrives between now and the switch in step 5 lands in SQLite, not Neon; step 7 picks those up. Go straight on to step 5 without closing the shell.

## 5. Switch the app to Neon (Martin, 5 minutes)

1. Render → the service → Environment → Add Environment Variable. Key `DATABASE_URL`, value: the **direct** connection string, pasted exactly. Save changes.
2. Render redeploys the service when a variable is saved. If no deploy starts within a minute, Manual Deploy → Deploy latest commit.
3. Watch Logs while it boots. The boot lines must not include `[seed] Admin account created`; that line means the app found an empty database, which means the string is wrong (a different project, or a typo). If you see it, go straight to the rollback in step 8 and tell Claude. A boot that stops with `the postgresql database has the app's tables but no Alembic revision` means the string points at a database an older release built for itself; the app refuses to adopt it rather than keep its narrower columns. Roll back, then drop and recreate the schema (step 4.6) and copy again.
4. `SECRET_KEY` is not touched at any point. It signs every session cookie; leaving it alone is what keeps every partner and admin logged in across the move.

## 6. Verify (Martin, 10 minutes)

Each check uses the live site. Do them in order.

1. `https://hellonewhome.co.uk/healthz` shows `{"ok":true}`. This proves the process is up; it deliberately does no database work, so the next checks matter more.
2. An area page: `https://hellonewhome.co.uk/areas/somerset` loads with Martin's name and WhatsApp number, and `https://hellonewhome.co.uk/areas/london` with Ina's. These are read from the database.
3. The postcode finder: `https://hellonewhome.co.uk/api/locate?postcode=TA1%201AA` returns `"covered": true` with `"slug": "somerset"`, and `https://hellonewhome.co.uk/api/locate?postcode=SE1%207PB` returns London.
4. A test lead: on the home page, send an enquiry with the name `Neon test lead`, a real postcode in Somerset and your own number. Then Admin → Leads: it is at the top, routed to Somerset, with an id higher than the last copied lead's id (that proves the id sequences were reset; a duplicate-key error on this step means they were not, and step 8 applies). Delete nothing; set its status to lost.
5. Admin login: log out and in again at `/admin/login`. The users table is the one being read.
6. Partner login: ask Ina to open `/partners` on her phone; her existing session should still work, and a fresh login should too. Her leads list must show the same leads as before.
7. Dates: open any lead from before the move and confirm it shows the same date and time as it did before (times are stored in UTC and shown in UK time; a wrong offset here means the copy changed the timestamps, which is a rollback).
8. Counts: Admin → Leads, Partners and Users again. Leads equal the dry-run count plus the test lead plus anything from step 7; partners and users unchanged.

## 7. Anything that arrived during the switch (Martin, with Claude, 5 minutes)

The shell from step 4 is still open on the old instance (if it has closed, open a new one and export `NEON_DIRECT_URL` again as in step 4.2; the disk and the SQLite file are still there).

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

## 8. Rollback (Martin, 5 minutes)

The SQLite file was never modified, so going back is one variable.

1. Render → the service → Environment → delete `DATABASE_URL` (or save it blank; both mean SQLite). Save. The service redeploys and boots on `/var/data/db/hq.db`, exactly the state it was in before step 5.
2. Verify with steps 6.1 to 6.3.
3. Any lead or edit made while the app was on Neon is in Neon, not in the file. Tell Claude how long the app ran on Neon; those rows are copied back by hand before a second attempt.
4. The Neon project can stay; a second attempt starts again from step 4.6 (drop and recreate the schema) so the copy is clean.

## 9. Weekly backup and a restore drill (Martin, 10 minutes a week)

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
4. If the office machine cannot run `pg_dump`, the fallback is the Neon console's own backup (Branches → create a dated branch each week); it stays inside Neon, so it is a second copy, not an off-site one.

## 10. Afterwards

1. Neon console → the project → Settings → Compute: leave scale-to-zero on. The first request after an idle spell takes about half a second longer while the compute wakes, which is fine for this site; the health check does not touch the database, so Render never sees the wake-up as a failure.
2. Leave `HNH_DATA_DIR` and the disk as they are. The SQLite file is the rollback copy; after a month on Neon with no problems it is simply unused and can stay (it is a few megabytes on a 1 GB disk).
3. From now on, a schema change means an Alembic migration in the repository. The app applies pending migrations when it starts, so the deploy that carries the migration upgrades Neon as it boots; `alembic upgrade head` does the same by hand. The README explains the workflow, including the one rule for `alembic revision --autogenerate`: run it against a database that `alembic upgrade head` built, not against an adopted file.
4. Column widths: the baseline migration widened several text columns (for example `settings.key` 64 → 100, `leads.postcode` 12 → 16). SQLite ignores widths, so the live file is unaffected and is adopted as it is. Neon gets the new widths because the copy script builds its schema with the baseline. A Postgres database that the old release had built for itself with `create_all` would still carry the narrower widths, which is why the app refuses to adopt one (step 5.3) rather than stamp it.
5. If the app ever needs a second instance, swap `DATABASE_URL` to the pooled string; nothing else changes.
