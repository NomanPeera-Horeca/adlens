# AdLens — creative intelligence for Meta ads

A multi-tenant SaaS skeleton: users sign in with Facebook, AdLens pulls their
ad creatives from the Meta Marketing API, scores each one (**Scale / Watch /
Kill**), and flags broken conversion tracking. Built on the same stack you
already run — **FastAPI + SQLModel + Postgres + a static frontend, deployed on
Render** — so it drops straight into your world.

This is a *real product skeleton*, not a toy. The parts that separate a
sellable SaaS from a browser tool are here: server-side OAuth, encrypted token
storage, multi-tenant user model, plan gating, billing hook, and Meta's
required compliance callbacks.

---

## What's built vs. what's left

**Built and working**
- Facebook Login (server-side OAuth: code → short-lived → long-lived token).
- Tokens encrypted at rest with Fernet; the app secret never reaches the browser.
- Multi-tenant `User` model — each user only sees their own ad data.
- `/api/accounts` and `/api/insights` (paginated pull + creative thumbnails).
- Server-side verdict scoring (ROAS mode, with CTR/CPC fallback when tracking is broken).
- Plan gating example (free = 30-day window, pro = full history).
- Stripe checkout + webhook (flips `user.plan` to `pro` on subscription; downgrades on cancel).
- Insights caching via `SyncRun` table (hourly for free, 15 min for pro; Refresh forces a new pull).
- Meta `/deauthorize` + `/data-deletion` callbacks with signed_request handling.

**Left to do before charging money**
1. **Meta App Review — this is the gate.** To let *other* businesses log in,
   request **Advanced Access** for `ads_read`. Until then the app works only
   for you and testers you add (Development mode). Review needs a screencast,
   a privacy policy URL, and your data-deletion URL (already scaffolded).
2. **Stripe** — add live keys + a price ID, then point Stripe webhooks at
   `https://adlens.onrender.com/api/billing/webhook`.
3. **Background sync** — optional scheduled refresh (e.g. Render cron) so data
   is warm before users open the dashboard.
4. **Teams** — add an Organization table so one company = multiple seats.
5. **Hardening** — token-refresh job, error/retry on Meta calls, logging,
   rate limiting, a real privacy policy + terms.

---

## Run locally

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill it in (see below)
uvicorn app.main:app --reload
# open http://localhost:8000
```

Fill `.env`:
- `FERNET_KEY` — `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- `META_APP_ID` / `META_APP_SECRET` — from developers.facebook.com → create a
  "Business" app → add the **Facebook Login** product.
- In that app's Facebook Login settings, whitelist the redirect URI exactly:
  `http://localhost:8000/auth/facebook/callback`
- Add yourself as a tester so you can log in while the app is in Development mode.

## Deploy (Render)

Production: **https://adlens.onrender.com**

Repo: **https://github.com/NomanPeera-Horeca/adlens**

Push to GitHub; Render auto-deploys from `main`. `render.yaml` provisions the web
service + Postgres. After deploy, set the `sync:false` env vars in the Render
dashboard (Meta keys + Fernet key), and whitelist this redirect URI in the Meta
app: `https://adlens.onrender.com/auth/facebook/callback`.

---

## Architecture (one line)

`Browser (static SPA) → FastAPI → Meta Graph API`, with Postgres holding users +
encrypted tokens. No token ever lives in the browser; all Meta calls are made
server-side with the user's stored token.

```
backend/app/
  config.py    env-driven settings
  db.py        engine + session
  models.py    User (multi-tenant, encrypted token), SyncRun (cached insights)
  crypto.py    Fernet encrypt/decrypt
  meta.py      Graph API client + normalizer
  scoring.py   the verdict engine (your opinion layer)
  sync.py      cache layer for insights pulls
  auth.py      Facebook OAuth routes
  main.py      API, billing, compliance, serves frontend
frontend/
  index.html   the dashboard (Connect Facebook → accounts → scored creatives)
```

---

## The business, briefly

The moat isn't the dashboard — Meta's API is public and anyone can render a
table. The defensible layer is the **opinion**: the verdict engine in
`scoring.py`, the tracking-gap detection, and (next) creative-pattern analysis
("hook + offer-first banner + bold-claim headline → your best CTR"). That's the
thing worth paying for, and it's the thing competitors can't copy by reading
your API docs.

Natural first wedge given your world: **restaurant / local-SMB advertisers and
the agencies that serve them** — people who run real ad budgets but can't
justify $250/mo for Motion. Land them, learn the patterns specific to that
niche, then widen. Pricing that undercuts Motion while still being healthy SaaS
margin: ~$49–79/mo solo, ~$149+ for agencies managing multiple accounts.
