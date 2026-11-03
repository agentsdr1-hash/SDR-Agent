# Operation Bosphorus: Umang's Bachelor (Istanbul, Nov 14–22, 2026)

Static site (`index.html`, no build step). Hosted on **Vercel**; votes, points, uploads and logins on **Supabase** (free plan).

| Section | What it does |
|---|---|
| The Mission | Countdown to wheels-down at IST, trip facts, sealed daily dispatches (unseal 06:00 Istanbul time), organizer-only personal messages |
| The Crew | Five profiles: codename, one-liner, photo, flights, nights in Istanbul, live points |
| The Intel | Flights, nightly headcount, base + travel times, weather/prayer times, packing list (per device), shared bookings checklist, items to confirm, rain plan, risks |
| The Itinerary | Day picker (auto-selects today), timeline with map links, full-day Google Maps route, attractions, wine route |
| The Challenges | 20 challenges (daily / dares / competitions) + crew-proposed dares; claim, undo, strike, record winners |
| The Vault | Photo/video uploads tagged by trip day, lightbox, inside-jokes wall |
| The Polls | Live voting; anyone can open a vote; you can only cast your own vote |
| The Leaderboard | Challenges + daily drops (5 pts/day) + groom's decrees; awards unseal Sun Nov 22 06:00; punishment draw |

## Setup (about 20 minutes)

### 1. Supabase (data, photos, login)
1. Create a free project at supabase.com. Region: Frankfurt (closest to Istanbul).
2. Open `supabase/schema.sql`, replace the five `@example.com` emails with the crew's real emails (lowercase), and uncomment the `is_owner` line for the organizer.
3. Supabase → **SQL Editor** → paste the whole file → **Run**.
4. Supabase → **Authentication → Sign In / Providers → Email**: keep Email enabled and turn **off "Confirm email"**. The built-in mailer only sends a few emails per hour, which isn't enough for 5 people on several phones.
5. Supabase → **Project Settings → API**: copy the **Project URL** and the **anon public** key into `config.js`.

### 2. Vercel (hosting)
1. Push this folder to its own **private** GitHub repo (recommended), or keep it here.
2. vercel.com → **Add New → Project** → import the repo.
3. **Root Directory:** `bachelor-party` (or leave blank if it's the repo root). **Framework preset:** Other. No build command.
4. Deploy. You'll get `https://<name>.vercel.app`. Each push to the repo redeploys it.

No GitHub? Netlify Drop (app.netlify.com/drop) accepts this folder dragged in directly.

### 3. Crew onboarding
Send the link. Each person taps **First time? Create account** with the email on the list and picks a password. Emails not on the list are refused.

## How access works
- Every table and the photo bucket are locked by row-level security to emails in `crew_members`.
- The anon key in `config.js` is meant to be public; it grants nothing without a crew login.
- Photos and videos live in a **private** bucket and are shown through signed links that expire after 7 days (renewed automatically).
- Only you can cast your vote, delete your uploads and jokes, or upload into your own folder. Only the groom or organizer can issue point decrees. Only the organizer can write sealed messages.

## Limits and known risks
- **Free projects pause after 7 days with no activity.** If nobody opens the site before Nov 14, restore the project from the Supabase dashboard (one click). Opening the site once a week prevents it.
- Free plan: 1 GB file storage, 50 MB per upload. Photos are resized to about 400 KB, so the limit is video. Keep long videos in a shared album.
- The itinerary text, Airbnb address and sealed dispatches ship in the page code. The sign-in hides them from casual visitors, but someone with the URL could read them in the page source. The URL is excluded from search engines (`noindex`). Real secrets go in the organizer's personal messages, which are stored in Supabase.

## Local preview
Open `index.html` with `config.js` left at its placeholders: the site runs in preview mode and saves to that browser only.
