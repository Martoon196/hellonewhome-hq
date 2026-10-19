# Hello New Home HQ

One application that runs the public website, the partner portal and the head-office admin panel.

| Part | URL | Who |
|---|---|---|
| Public site | `/`, `/areas/{slug}`, `/areas/{slug}/lettings`, `/areas/{slug}/short-lets`, every brand page | Everyone |
| Partner portal | `/partners` (login at `/partners/login`) | Local Partners |
| Admin panel | `/admin` (login at `/admin/login`) | Head office |
| Lead API | `POST /api/lead`, `GET /api/locate?postcode=` | The site's own forms |

Stack: Python 3.11+, FastAPI, SQLAlchemy 2, Jinja2, SQLite locally / Postgres in production (optional). No build step, no Node.

## Run locally

```
cd hq
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
ADMIN_PASSWORD=choose-one ./run.sh          # http://127.0.0.1:8010
```

First boot creates the database in `hq/data/`, seeds the four areas (Swansea active, Durham/Andover/London "opening soon"), placeholder partners, the resource library index and the admin account (`ADMIN_EMAIL`, default `admin@hellonewhome.co.uk`). If `ADMIN_PASSWORD` is not set, a password is generated and printed once in the console and the admin must replace it on first login; on Render always set `ADMIN_PASSWORD`, so nothing is ever written to the retained service logs.

Tests: `.venv/bin/python -m pytest -q`

## Deploy to Render (recommended)

1. Push the `hq/` folder to a GitHub repo (private is fine).
2. Render → New → Blueprint → pick the repo. `render.yaml` defines the service (Starter plan, 1 GB persistent disk for the SQLite database and uploads).
3. Set `ADMIN_PASSWORD` when prompted. Everything else is in the blueprint.
4. Render → Settings → Custom domains → add `hellonewhome.co.uk` and `www.hellonewhome.co.uk`. Render shows a CNAME/A record.
5. IONOS → Domains → DNS for hellonewhome.co.uk → add the records Render gave you. SSL is automatic.
6. Open `https://hellonewhome.co.uk/admin`, log in, change the password.

Optional: set `DATABASE_URL` to a Render Postgres instance for a managed database (the app switches automatically). Keep the disk for uploads either way.

Optional email notifications for leads: set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `LEAD_NOTIFY_TO`. Without them, leads still land in the admin panel and the partner portal; nothing is lost.

## Day-one checklist in the admin panel

1. **Settings**: licence numbers, client money and redress schemes, ICO number, registered office. The footer and fees page read these live.
2. **Partners**: add each mentee, attach them to their area, click "Create login". Copy the one-time password to them on WhatsApp. They are forced to set their own password on first login.
3. **Areas**: check the postcode districts (the territory checker flags overlaps), set the partner, switch status from "opening soon" to "active". The area pages, the postcode finder, the footer and the sitemap update instantly.
4. **Resources**: replace the placeholder items with your real contracts, software guides, scripts and training (upload files or paste links). Restrict a resource to an area or a country where it only applies there.
5. **Announcements**: the first message partners see on their dashboard.

## How a partner uses it

Dashboard (leads, SLA breaches, announcements) → Leads (WhatsApp the lead in one tap, update status, notes) → Marketing kit (flyers and letters rendered with their name and number, print to PDF; Facebook group posts for their patch with copy buttons; square, story and cover graphics as PNG; Google Business Profile copy for their area) → Resources → My area (postcodes, live pages, request a change) → Profile (bio, photo, WhatsApp; this is what landlords see on the area page).

## Content files

- `content/areas-seed.json`: the first four areas (used only on first boot; edit areas in the admin afterwards).
- `content/areas/{slug}.json`: bespoke SEO copy per area (hub, lettings, short-lets, neighbourhoods, FAQs). Loaded into the area on first seed; editable in the admin as JSON.
- `content/area-content-template.json`: tokenised fallback copy so a brand-new area has sensible pages before bespoke copy is written.
- `content/marketing-templates.json`: flyers, Facebook posts, graphics, Google Business Profile copy, letters and scripts with `{{tokens}}`; rendered per partner in the portal. Edit the file and redeploy, or call `services.content.reload()`.
- `content/resources-seed.json`: the resource library index seeded on first boot.

## Security notes

- Passwords: PBKDF2-SHA256, 200k iterations. Sessions: signed, HttpOnly, SameSite=Lax cookies bound to the current password hash, so a password change or reset ends every other session; `SECURE_COOKIES=1` in production. Login attempts are throttled per address and email (and per address overall); `render.yaml` starts uvicorn with `--proxy-headers` so the address is the visitor's, not the proxy's. A paused or departed partner is locked out everywhere at once, not only at the login form.
- Login throttling: 8 attempts per 15 minutes per IP+email.
- CSRF: every state-changing request on `/admin`, `/partners` and `/api/lead` must come from our own origin. `/api/lead` also caps the body at 32 KB, trims every stored field, only accepts the public enquiry kinds and allows 30 leads an hour per address.
- Uploads: extension allowlist, size cap (`MAX_UPLOAD_MB`), sanitised names, served only through controlled routes; resource files require login and respect visibility.
- Partners only ever see leads, resources and areas attached to their own partner record.
