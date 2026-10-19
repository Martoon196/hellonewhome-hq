# Hello New Home: go-live steps

Written 19 October 2026. Each step says who does it and exactly what to do.

## 1. GitHub (you: 1 minute, then me)

1. Go to https://github.com/new (logged in as Martoon196).
2. Repository name: `hellonewhome-hq`. Private. Leave "Add a README" unticked. Create.
3. Tell me "repo's there". I attach it to this session and push the app (the commit is ready: 96 files, tests green).

## 2. Render (you: 10 minutes)

1. https://dashboard.render.com → New → Blueprint.
2. Connect GitHub if asked, pick `Martoon196/hellonewhome-hq`, branch `main`. Render reads `hq/render.yaml`.
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

For each of Durham, Andover and London, I need:

```
Area:         Durham
Name:         
First name:   
Mobile:       (the WhatsApp number)
Email:        (their login)
Two-line bio: (where they live, what they do, why landlords should trust them)
Photo:        (optional now; they can upload it in the portal)
```

What happens next: Admin → Partners → their record → "Create login" → copy the one-time password to them on WhatsApp. They log in at `/partners/login`, set their own password, and their area page, flyers and posts carry their name from that moment.

London is set to Zone 1 South East (SE, BR and Bexley DA postcodes) as a starting point. Tell me if the first London partner lives elsewhere and I'll move the zone.

## 5. Resources (you, in Admin → Resources)

34 placeholder slots are seeded (contracts, software guides, scripts, training, brand, compliance, finance). Click one, upload the file or paste the link, save. The "coming soon" badge disappears and partners can download it. Restrict anything England-only or Wales-only with the Visibility field.

## 6. Settings (you, in Admin → Settings)

Fill in: Rent Smart Wales licence number, client money protection scheme and membership number, redress scheme and number, ICO registration, registered office address. They appear in the site footer, the fees page and the compliance page immediately.

## 7. Fees (you: one decision)

The fee sheet on the site (Full Management 12% capped at £150, Tenant Find £495, set-up £295, rent guarantee add-on £25, HMO 15% and £249 room let-only, SA Launch £495, Full SA 18%, Co-Host 12%, no VAT) is my researched recommendation. Reply "fees confirmed" or send changes. Then tick "Fee sheet confirmed" in Admin → Settings and the draft strip comes off every flyer.

## 8. Two external checks (you: two emails, drafts below)

**To your redress scheme and your client money protection scheme:**

> Subject: Confirmation of coverage for England and self-employed field representatives
>
> Hello New Home 2 Limited (company no. 13752357), trading as Hello New Home, membership [number]. We are a letting and property management agent based in Swansea. We are opening in England (County Durham, Test Valley and South East London) with self-employed local representatives who carry out viewings, inspections and check-ins under our brand. All tenancy agreements, client money, referencing and Right to Rent checks are handled by our head office; representatives never hold client money or sign agreements. Please confirm in writing that (1) our membership covers lettings and management activity in England as well as Wales, and (2) the arrangement with self-employed representatives is within the scope of our membership, and whether each area should be registered as a branch. Thank you.

**To a planning solicitor (London):**

> Subject: Short-let wording for a London lettings page
>
> We manage short lets in Greater London. Our website says: "Whole-home short lets in Greater London are limited to 90 nights a year without planning permission, and any stay under 90 consecutive nights counts towards it. After the 90 nights, the property moves to a let of 90 nights or more, or a tenancy." Please confirm this is an accurate statement of section 25 of the Greater London Council (General Powers) Act 1973 as amended by section 44 of the Deregulation Act 2015, and flag anything we should add or remove before the page goes live.

## After go-live

- Google Search Console: verify the domain, submit `https://hellonewhome.co.uk/sitemap.xml`.
- Google Business Profile per area: `national/MAPS-PACK-PLAYBOOK.md`, and the copy is in each partner's kit.
- Optional lead emails: set `SMTP_*` and `LEAD_NOTIFY_TO` in Render → Environment. Leads land in the admin and portal regardless.
