# Hello New Home HQ

One application that runs the public website, the partner portal and the head-office admin panel.

| Part | URL | Who |
|---|---|---|
| Public site | `/`, `/areas/{slug}`, `/areas/{slug}/lettings`, `/areas/{slug}/short-lets`, `/commercial`, `/areas/{slug}/commercial`, every brand page | Everyone |
| Partner portal | `/partners` (login at `/partners/login`) | Local Partners |
| Admin panel | `/admin` (login at `/admin/login`) | Head office |
| Lead API | `POST /api/lead`, `GET /api/locate?postcode=` | The site's own forms |
| Partner API | `/api/partner/partners`, `/api/partner/properties` (see below) | Group HQ, Property Predator, Key Properties; server to server |

Stack: Python 3.11+, FastAPI, SQLAlchemy 2, Jinja2, Alembic. SQLite for local development and the tests; Postgres (Neon) in production. No build step, no Node.

## Run locally

```
cd hq
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
ADMIN_PASSWORD=choose-one ./run.sh          # http://127.0.0.1:8010
```

First boot creates the database in `hq/data/`, seeds the five areas (Swansea, Somerset and London active with their partners; Durham and Andover "opening soon" with placeholder partners), the resource library index and the admin account (`ADMIN_EMAIL`, default `admin@hellonewhome.co.uk`). If `ADMIN_PASSWORD` is not set, a password is generated and printed once in the console and the admin must replace it on first login; on Render always set `ADMIN_PASSWORD`, so nothing is ever written to the retained service logs.

Tests: `.venv/bin/python -m pytest -q`

## Deploy to Render (recommended)

1. The repo is `Martoon196/hellonewhome-hq` (this folder is its root).
2. Render → New → Blueprint → pick the repo. `render.yaml` defines the service (Starter plan, 1 GB persistent disk for uploads and the fallback SQLite file).
3. Set `ADMIN_PASSWORD` and `DATABASE_URL` when prompted (both are `sync: false`, so they are never written into the blueprint). Everything else is in the blueprint.
4. Render → Settings → Custom domains → add `hellonewhome.co.uk` and `www.hellonewhome.co.uk`. Render shows a CNAME/A record.
5. IONOS → Domains → DNS for hellonewhome.co.uk → add the records Render gave you. SSL is automatic.
6. Open `https://hellonewhome.co.uk/admin`, log in, change the password.

## Database

- `DATABASE_URL` unset or blank: the app uses the SQLite file `hq.db` under `HNH_DATA_DIR` (`hq/data/` locally, `/var/data/db` on Render). This is how local development and the tests run; the test suite drops `DATABASE_URL` so it can never reach a real database. To run the suite against a throwaway Postgres database, set `HNH_TEST_DATABASE_URL` instead (the run drops and rebuilds that database's app tables).
- `DATABASE_URL` set: the app uses that Postgres database. Production runs on Neon; the connection string is the `postgresql://` form with `?sslmode=require` and lives only in Render → Environment. The SQLite file is then only the fallback for a rollback and nothing reads it.
- The move from the live SQLite file to Neon, the checks afterwards, the rollback and the weekly backup are all in `DEPLOY-NEON.md`.
- Schema changes go through Alembic: change the model in `app/models.py` and add a migration under `alembic/versions/`. The app applies any pending migrations when it starts (`app.migrations.run_migrations`, called from the seed), so a deploy brings Neon up to date; `alembic upgrade head` does the same by hand. Nothing adds a column to an existing table by itself.
- Writing a migration: `alembic revision --autogenerate -m "describe the change"` compares the models with a database, so point it at one that `alembic upgrade head` built (delete `data/hq.db` first, or set `DATABASE_URL` to an empty local Postgres). A SQLite file the old release built and the app then adopted by stamping keeps its original column widths, which SQLite ignores, and `alembic check` is told to ignore String length differences there; against Postgres every difference counts. Read the generated script before committing it.
- Startup refuses two states rather than guessing: a database with only some of the app's tables, and a Postgres database that has the tables but no Alembic revision (one an older release built with `create_all`, which would keep narrower columns if stamped). The message says what to do.
- Account emails (admin users and partner logins) are stored and looked up trimmed and lower-cased, so an address typed with capitals or spaces finds the same login on SQLite and Postgres alike.

`FEES_CONFIRMED=1` (set in `render.yaml`) marks the fee sheet as confirmed on every boot, which clears the draft strip and hold notice on the partners' marketing kit; remove it to control the flag from Admin → Settings alone.

## Commercial lettings

Commercial property (shops, offices, workshops, units on a business lease) is its own service line. `/commercial` and `/areas/{slug}/commercial` explain what is different from a home letting (no deposit scheme, no Renters' Rights Act, no Right to Rent; security of tenure under the Landlord and Tenant Act 1954 and contracting out; MEES for non-domestic property; business rates and empty-rates relief; the VAT option to tax; the Code for Leasing Business Premises 2020) and carry the commercial appraisal form, which posts to `POST /api/lead` as kind `commercial_appraisal` with `business_name`, `property_use` (office, retail, industrial, leisure, mixed, other), `size_sqft`, `lease_status` (vacant, let, lease_ending) and `asking_rent`, all kept in the lead's payload. Both pages are in `sitemap.xml`; `areas.js` lists each area's `pages`.

Routing is one rule in `app/services/routing.py`: a commercial lead, and a commercial instruction from the Partner API (`property.service_line: commercial`), goes to head office (partner none, area kept) unless the area's partner has **Can take commercial instructions** ticked on their admin page (`partners.commercial_enabled`, Alembic revision 0004; every change is in the audit log). The notification email names the kind as commercial and never the landlord; the area's partner is copied in only when the enquiry routes to them. A partner without the flag sees no commercial lead or instruction in the portal, even in their own area; with it they also get a Commercial tab in the marketing kit (the appraisal checklist from `app/services/kit.py`). Admin → Instructions filters by service line.

The commercial fee sheet (`fees_commercial` in Admin → Settings: letting 10% of the first year's rent with a £1,500 minimum, management 8% of rent collected, lease renewal or rent review £750, schedule of condition £350, plus VAT only if Hello New Home 2 Ltd registers for VAT) is a proposal. Until **Commercial fee sheet confirmed** is ticked (or `FEES_COMMERCIAL_CONFIRMED=1` is set, which `render.yaml` deliberately leaves out) the public pages show only "Fees are quoted after the appraisal" and the settings page badges the group `[CONFIRM]`. The training page "Commercial lettings: what is different" (`content/resources/commercial-lettings.html`) is seeded into Resources by the `key`/`revision` entry in `content/resources-seed.json`: a live database gains it on the next boot, and bumping `revision` refreshes it in place, as area rows are refreshed.

Optional email notifications for leads and property instructions: set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `LEAD_NOTIFY_TO` (`app/services/mail.py` is the one path both use). Without them, leads and instructions still land in the admin panel and the partner portal; nothing is lost.

## Day-one checklist in the admin panel

1. **Settings**: licence numbers, client money and redress schemes, ICO number, registered office. The footer and fees page read these live. "Systems" holds the Ordris link shown to partners.
2. **Partners**: add each mentee (Martin Howard for Somerset and Ina Gutowska for London are seeded), attach them to their area, click "Create login". Copy the one-time password to them on WhatsApp. They are forced to set their own password on first login.
3. **Areas**: check the postcode districts (the territory checker flags overlaps), set the partner, switch status from "opening soon" to "active". The area pages, the postcode finder, the footer and the sitemap update instantly.
4. **Resources**: replace the placeholder items with your real contracts, software guides, scripts and training (upload files or paste links). Restrict a resource to an area or a country where it only applies there.
5. **Announcements**: the first message partners see on their dashboard.

## How a partner uses it

Dashboard (leads, SLA breaches, announcements, the Ordris link) → Leads (WhatsApp the lead in one tap, update status, notes) → Marketing kit (flyers and letters rendered with their name and number, print to PDF; Facebook group posts for their patch with copy buttons; square, story and cover graphics as PNG; Google Business Profile copy for their area) → Resources → My area (postcodes, live pages, request a change) → Profile (bio, photo, WhatsApp; this is what landlords see on the area page).

## Content files

- `content/areas-seed.json`: the seeded areas and their partners. New slugs are inserted on boot. An existing row is refreshed in place only when its `revision` number is higher than the one recorded in the `seed_revisions` setting: the area's facts, status, bespoke content and, if the live partner is still the seeded placeholder (or the same email address), the partner's details. Bump `revision` to ship a change to a live area; leave it alone and head-office edits in the admin survive every deploy.
- `content/areas/{slug}.json`: bespoke SEO copy per area (hub, lettings, short-lets, neighbourhoods, FAQs). Loaded into the area on first seed; editable in the admin as JSON.
- `content/area-content-template.json`: tokenised fallback copy so a brand-new area has sensible pages before bespoke copy is written.
- `content/marketing-templates.json`: flyers, Facebook posts, graphics, Google Business Profile copy, letters and scripts with `{{tokens}}`; rendered per partner in the portal. Edit the file and redeploy, or call `services.content.reload()`.
- `content/resources-seed.json`: the resource library index seeded on first boot.

## Partner API

Group HQ seats mentees as partners without anyone logging in here. Three routes, all server to server, in `app/routers/partner_api.py`:

| Route | Body | Does |
|---|---|---|
| `POST /api/partner/partners` | `{email, name, tier: introducer\|area, status: pending\|active\|paused, first_name?, phone?, whatsapp?, whatsapp_display?, create_login?, franchise?: {fee_waived, reason}, area_slug? \| new_area?: {slug?, name, region?, postcodes[], covers?} \| postcodes?[]}` | Creates the partner, or updates the one with that email (name, contact details, tier, status and the franchise note; bio, photo, notes and units managed stay head office's). 201 when created, 200 when updated. |
| `POST /api/partner/partners/{id}/status` | `{status, reason?}` | Pauses or reactivates; the partner, the login and the area stay on record. A paused partner is locked out of the portal at once. |
| `GET /api/partner/partners?email=` | | The partner with its areas and login state; `{"ok": true, "found": false, "partner": null}` when there is none. |

Areas: `area_slug` assigns an existing area (a seeded "opening soon" area's placeholder partner becomes the person; an area held by a real partner answers 409 `area_assigned`; a head-office area is refused). `new_area` creates one through the same validation and postcode-clash checks as the admin form; it goes live with the partner when `status` is `active`, otherwise it starts as "opening soon". When the slug already exists it is assigned only if its postcodes are the ones sent (a repeated call); with different postcodes the call is refused with 409 `slug_taken` naming the postcodes head office holds, so a changed territory is never dropped without a word. Bare `postcodes` creates an area named after its districts (`LL11 and LL12`) that always starts as "opening soon" until head office names it. `create_login: true` makes the portal login with a one-time password that is returned once and must be changed on first use; a repeated call never creates a second login or resets the password, which is what makes the same call safe to send again. `franchise` is recorded on the partner (`franchise_fee_waived`, `franchise_note`) and shown read-only on the admin partner page.

Authentication: the caller sends `X-Partner-Client: grouphq` and its secret in `X-Service-Token` (an `Authorization: Bearer` header is accepted as well). The database holds only the SHA-256 of each secret (`api_clients`: name, hash, scopes, active, last used), compared in constant time; an unknown or disabled client is 401, a missing scope (`partners:write` for the writes, `partners:read` or `partners:write` for the lookup) is 403, and any request carrying an `Origin` header is 403 whatever else it carries, so no browser can reach these routes. With no client row the API refuses everything. Errors are `{"ok": false, "reason": "<code>", "message": "<sentence>"}`; the codes are `missing_token`, `missing_client`, `invalid_client`, `client_inactive`, `too_many_attempts` (429: thirty refused credentials from one address within an hour, whatever it sends next), `origin_not_allowed`, `scope_missing`, `invalid_json`, `invalid_request`, `request_too_large`, `no_partner`, `email_in_use`, `area_assigned`, `postcode_clash` and `slug_taken`. Every call is written to the audit log as `partner_api.*` with the client's name; the secret is never logged, stored or echoed.

Creating a client, either way:

- `HNH_PARTNER_TOKEN_GROUPHQ` (Render → Environment, `sync: false` in the blueprint): set it to the same value as Group HQ's `HNH_PARTNER_TOKEN`, at least 32 characters, and every boot makes sure the `grouphq` client with the `partners:write` scope carries its hash. A new value rotates the secret on the next boot. A client disabled with the script stays disabled until it is enabled again, whatever the variable says.
- `.venv/bin/python scripts/api_client.py create grouphq partners:write` prints a 48-character secret once (also `rotate`, `disable`, `enable`, `list`). On Render, run it from the service shell.

### Property instructions: "Let this property with Hello New Home"

Property Predator and the Key Properties portal send a landlord's property to be let; the public site will do the same. Two routes, behind the same authentication with the `properties:write` scope (`properties:read` for the lookup); the clients are `propertypredator` and `keyproperties`, each bootstrapped by its own variable (`HNH_PARTNER_TOKEN_PROPERTYPREDATOR`, `HNH_PARTNER_TOKEN_KEYPROPERTIES`) exactly as `grouphq` is, or created with `scripts/api_client.py create propertypredator properties:write`.

| Route | Body | Does |
|---|---|---|
| `POST /api/partner/properties` | `{external_ref, origin_system?: pp\|kp\|web, origin_url?, introducer?: {person_key, pp_account_id, kp_user_id}, landlord: {name, email?, phone?}, consent: {given: true, at?, text_version}, property: {address, postcode, bedrooms?, property_type?, furnished?: furnished\|unfurnished\|part, rent_expectation?, available_from?, service_line?: residential\|hmo\|sa\|commercial}, notes?}` | Records the instruction as `HNH-000123`, routes it by postcode to the area and its partner exactly as a lead is (an uncovered postcode is kept for head office to place), emails head office and the area's active partner, and answers 201 with the reference and the status record. The same `external_ref` sent again answers 200 with the existing reference and writes nothing; the same reference from another system is 409 `external_ref_taken`. Each client may create 60 instructions a day (429 `daily_limit`); resends do not count. |
| `GET /api/partner/properties/{external_ref}` | | `{ok, found, ref, instruction: {ref, external_ref, origin_system, service_line, status, status_history, area, partner_first, covered, ordris: {landlord_client_id, property_id, tenancy_id}, created_at, updated_at}}`. No landlord details and no fee figures: the sender holds its own copy of what it sent. |

Consent is required: `consent.given` must be `true` and `consent.text_version` must name the wording the landlord saw, or the call is 422 `consent_required` and nothing is stored. The transfer is between two data controllers (Hello New Home 2 Ltd and MH196 2026 Ltd), so the version and the time of consent are kept on every instruction. Head office works instructions under Admin → Instructions (filters by status, area and origin; status, notes and area on the detail page; the raw body the sender posted). Partners see the ones routed to their areas under Instructions in the portal: the landlord's contact details, the property and the status, never the raw payload, the introducer's account ids or a fee figure.

Marking an instruction **instructed** (in the admin or the portal) sends the landlord and the property to Ordris: `POST {ORDRIS_BASE_URL}/api/partner/hnh/instructions` with `Authorization: Bearer ORDRIS_PARTNER_TOKEN_HNH` (and `X-Service-Token` with the same value), `X-Partner-Client: hnh` and the body `{external_ref: "HNH-000123", landlord: {name, email, phone}, property: {name, address, postcode, bedrooms, property_type}, tenancy: null}`; the ids Ordris answers with are stored on the instruction and shown to the sender on the lookup. The message is written to the `outbox` table first (`app/services/ordris_sync.py`), so a failed send keeps its error and is retried by `.venv/bin/python scripts/outbox_retry.py` (`--list` to look, `--all` to include rows past eight attempts) or by the Resend button on the admin page; "Send to Ordris" queues an instructed property that has no message yet. While `ORDRIS_BASE_URL` or `ORDRIS_PARTNER_TOKEN_HNH` is unset nothing is attempted and the row waits as pending. The token is never stored, logged or echoed; an outbox row records only the status code and Ordris's short reason.

**Homes to rent on the area pages** (plan, Phase 6 C5). `/areas/{slug}/lettings` reads Ordris's listings feed with the same two variables: `GET {ORDRIS_BASE_URL}/api/partner/hnh/listings?outward=SA1,SA2` with `Authorization: Bearer ORDRIS_PARTNER_TOKEN_HNH` (sent once, as the only header Ordris reads), `X-Partner-Client: hnh` and a five-second timeout, asking for the area's outward codes (a whole postcode area such as `TA` or `SE` in an area's list is left out, since the feed is by district). When the answer has something in it the page shows a "Homes to rent in <area>" block under the fee section: outward code, bedrooms, type, rent, availability and the first photo with its alt text, with a link to the rent review form and nothing else. The feed carries nothing personal, and `app/services/ordris_listings.py` keeps only the seven fields it promises (`id, outward, bedrooms, property_type, rent_pcm, available_from, photos`) before anything is cached or rendered, so an address or a name the feed might one day carry never reaches a page. Answers are cached in the process for an hour per outward set (one uvicorn worker, nothing shared through the database); when a refresh fails the last good answer is kept for up to 24 hours, a warning is logged and the next attempt waits five minutes, so a page view never waits on Ordris more than once every few minutes while it is down. A malformed answer is an empty one. While `ORDRIS_BASE_URL` or `ORDRIS_PARTNER_TOKEN_HNH` is unset nothing is asked for and the page is exactly as it was. Admin → Instructions carries a status line saying when this instance last refreshed the listings, how many area sets it holds and the last problem.

## Security notes

- Passwords: PBKDF2-SHA256, 200k iterations. Sessions: signed, HttpOnly, SameSite=Lax cookies bound to the current password hash, so a password change or reset ends every other session; `SECURE_COOKIES=1` in production. Login attempts are throttled per address and email (and per address overall); `render.yaml` starts uvicorn with `--proxy-headers` so the address is the visitor's, not the proxy's. A paused or departed partner is locked out everywhere at once, not only at the login form.
- Login throttling: 8 attempts per 15 minutes per IP+email.
- CSRF: every state-changing request on `/admin`, `/partners` and `/api/lead` must come from our own origin. `/api/lead` also caps the body at 32 KB, trims every stored field, only accepts the public enquiry kinds and allows 30 leads an hour per address.
- Uploads: extension allowlist, size cap (`MAX_UPLOAD_MB`), sanitised names, served only through controlled routes; resource files require login and respect visibility.
- Partners only ever see leads, resources and areas attached to their own partner record.
- The Partner API (above) is server to server only: hashed per-client secrets compared in constant time, a required client name, scopes, and a flat refusal of any request that carries an `Origin` header.
