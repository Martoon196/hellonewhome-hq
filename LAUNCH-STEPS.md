# Hello New Home: go-live steps

Written 19 October 2026, updated the same evening. Each step says who does it and exactly what to do. Steps 1 to 3 and 7 are done; the site is live at https://hellonewhome.co.uk.

## 1. GitHub (you: 1 minute, then me)

1. Go to https://github.com/new (logged in as Martoon196).
2. Repository name: `hellonewhome-hq`. Private. Leave "Add a README" unticked. Create.
3. Tell me "repo's there". I attach it to this session and push the app (the commit is ready: 96 files, tests green).

## 2. Render (you: 10 minutes)

1. https://dashboard.render.com → New → Blueprint.
2. Connect GitHub if asked, pick `Martoon196/hellonewhome-hq`, branch `main`. Render reads `render.yaml` at the repository root.
3. It asks for `ADMIN_PASSWORD`: choose a strong one and keep it (this is the head-office admin login).
4. Apply. First deploy takes 3 to 5 minutes. The service URL looks like `https://hellonewhome-hq.onrender.com`.
5. Open `https://hellonewhome-hq.onrender.com/admin`, log in as `lettings@hellonewhome.co.uk` with that password. You will be asked to change it once.

Cost: Starter web service plus a 1 GB disk, the same plan PropertyPredator runs on.

## 3. Domain (you: 5 minutes at Render, 5 at IONOS)

1. Render → your service → Settings → Custom Domains → Add `hellonewhome.co.uk` and `www.hellonewhome.co.uk`. Render shows the records it wants (an A record or ALIAS for the root, a CNAME for www).
2. IONOS → Domains & SSL → hellonewhome.co.uk → DNS → add those records, delete any old A/CNAME that point at the old WordPress webspace.
3. Wait for Render to show "Verified" (usually under an hour). SSL is automatic.
4. Render → Environment → confirm `BASE_URL` is `https://hellonewhome.co.uk` (it is in the blueprint).

## 4. Partners (you: send me the details, or type them into Admin → Partners)

Done so far (both seeded as active Local Partners; the live site picks them up on the next deploy):

| Area | Partner | Covers | Status |
|---|---|---|---|
| Somerset | Martin Howard, 07510 243096, martin.howard1984@gmail.com | TA and BA postcodes plus the Somerset BS districts (BS20 to BS29, BS39 to BS41, BS48, BS49) | Active |
| London | Ina Gutowska, 07775 801284, gutowskaewelina@gmail.com | Every London postcode (E, EC, N, NW, SE, SW, W, WC, BR, CR, HA, IG, RM, SM, UB and the London DA, EN, KT and TW districts) | Active, all of London until a second London partner arrives |

Still needed, for Durham and Andover:

```
Area:         Durham
Name:         
First name:   
Mobile:       (the WhatsApp number)
Email:        (their login)
Two-line bio: (where they live, what they do, why landlords should trust them)
Photo:        (optional now; they can upload it in the portal)
```

What happens next, for every partner including Martin and Ina: Admin → Partners → their record → "Create login" → copy the one-time password to them on WhatsApp. They log in at `/partners/login`, set their own password, and their area page, flyers and posts carry their name from that moment.

When a second London partner arrives, narrow Ina's zone in Admin → Areas → London (postcodes field) and add the new area; the territory checker flags any overlap.

## 5. Resources (you, in Admin → Resources)

34 placeholder slots are seeded (contracts, software guides, scripts, training, brand, compliance, finance). Click one, upload the file or paste the link, save. The "coming soon" badge disappears and partners can download it. Restrict anything England-only or Wales-only with the Visibility field.

## 6. Settings (you, in Admin → Settings)

Fill in: Rent Smart Wales licence number, client money protection scheme and membership number, redress scheme and number, ICO registration, registered office address. They appear in the site footer, the fees page and the compliance page immediately.

## 7. Fees (you: one decision)

Confirmed 19 October 2026: Full Management 12% capped at £150, Tenant Find £495, set-up £295, rent guarantee add-on £25, HMO 15% and £249 room let-only, SA Launch £495, Full SA 18%, Co-Host 12%, no VAT. `FEES_CONFIRMED=1` in `render.yaml` marks the sheet confirmed on every boot, so the draft strip is off every flyer and post without an admin click. To put the hold notice back (a fee change in progress), remove that variable in Render → Environment and untick "Fee sheet confirmed" in Admin → Settings.

Commercial, approved 22 October 2026: letting 10% of the first year's rent (minimum £1,500), management 8% of rent collected, lease renewal or rent review £750, schedule of condition £350, every figure quoted plus VAT. The first boot of that release ticks "Commercial fee sheet confirmed" once; untick it in Admin → Settings to take the figures off the public pages again. Two switches sit beside it in Admin → Settings: **VAT** ("Hello New Home 2 Ltd is VAT-registered", off) changes every "no VAT" claim on the site and in the marketing kit to "VAT included" the moment it is ticked, so review the residential figures and give landlords 30 days' notice first; **Systems** ("Send instructed properties to Ordris", on) holds instructed properties in the outbox while unticked. Which services each partner offers (residential, HMO, short-let and commercial management, and commercial letting) is ticked on their page under Admin → Partners.

## 8. Ordris (your agency management system)

Contracts, tenancies, inspections and statements stay in Ordris; this platform holds the website, leads, area pages, partner logins and the marketing kit. Partners get an "Ordris" link in their portal sidebar, an "Open Ordris" panel on their dashboard and an Ordris entry in Resources, all pointing at the link in Admin → Settings → Systems (set to https://ordris.io; change it to your account's login page if Ordris gives you one). Create each partner's Ordris user inside Ordris itself and send them that login separately; Ordris has no public API, so the two logins are not merged. Add Ordris how-to guides as links in Admin → Resources so the portal is the one place partners start from.

## 9. One external check (you: one email, draft below)

**To your redress scheme and your client money protection scheme:**

> Subject: Confirmation of coverage for England and self-employed field representatives
>
> Hello New Home 2 Limited (company no. 13752357), trading as Hello New Home, membership [number]. We are a letting and property management agent based in Swansea. We are opening in England (County Durham, Test Valley and South East London) with self-employed local representatives who carry out viewings, inspections and check-ins under our brand. All tenancy agreements, client money, referencing and Right to Rent checks are handled by our head office; representatives never hold client money or sign agreements. Please confirm in writing that (1) our membership covers lettings and management activity in England as well as Wales, and (2) the arrangement with self-employed representatives is within the scope of our membership, and whether each area should be registered as a branch. Thank you.

The planning-solicitor check is no longer needed: the website and the partner kit do not discuss London's short-let planning rule (your decision, 19 October 2026).

## After go-live

- Google Search Console: verify the domain, submit `https://hellonewhome.co.uk/sitemap.xml`.
- Google Business Profile per area: `national/MAPS-PACK-PLAYBOOK.md`, and the copy is in each partner's kit.
- Optional lead emails: set `SMTP_*` and `LEAD_NOTIFY_TO` in Render → Environment. Leads land in the admin and portal regardless.
