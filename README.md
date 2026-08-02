# AgentMediaBox — Performance Calculator (MVP v1)

A free tool for French real estate agents: paste a listing URL, get an
instant performance score (photos, video, description, DPE, address
precision) modeled on SeLoger's own published quality-score mechanics,
with each weak point mapped to an upsell (photo shoot, virtual staging,
photo-to-video, copywriting).

## How it works

1. Agent pastes a listing URL on the landing page.
2. The backend headless-browses the page with Playwright, takes a
   screenshot, and pulls structural signals from the rendered DOM:
   photo count, description length, presence of video/virtual tour,
   DPE mention, address precision.
3. `scoring.py` scores those signals against a rubric (110 pts total)
   and produces a 0–100% score + letter grade (A–E).
4. Results page shows the breakdown, the screenshot, and a "fix this"
   chip next to every signal that's costing points.
5. If the site blocks automated browsing (SeLoger, Bien'ici, Leboncoin
   and Figaro all run bot-detection and may block this), the tool
   fails gracefully and offers a manual-paste fallback instead of
   showing a broken or wrong score.

## Run it locally

```bash
pip install -r requirements.txt
playwright install chromium   # skip if already installed on your machine
python3 app.py
# open http://localhost:5050
```

## What's real vs. what's a stub (be honest with yourself before demoing this to anyone)

- **Real and tested**: end-to-end flow, scoring logic, screenshot capture,
  graceful failure + manual fallback, responsive landing/results pages.
  Verified against two synthetic listings (`samples/good_listing.html`
  and `samples/bad_listing.html`) — 86%/Grade A vs 15%/Grade E, with
  upsell chips appearing exactly where points are lost.
- **Not yet real**: photo *quality* (blur, lighting, composition, "is
  the first photo actually an exterior shot") — v1 only knows whether
  a hero shot check is possible, it can't yet look at the image itself.
  That's a vision-model pass for v2, and it's the single highest-leverage
  next feature since it's 10 of the 110 possible points and currently
  the analyzer just shrugs on it.
- **Untested against the real portals.** I validated the pipeline against
  local synthetic pages, not live SeLoger/Bien'ici/Leboncoin/Figaro URLs
  (those are anti-bot protected and I didn't want to hammer a real listing
  during dev). Before you show this to anyone, test it against 5–10 real
  listing URLs across each portal + a couple of independent agency sites,
  and see how often it hits the manual-fallback path. That failure rate
  is the actual thing that determines whether "paste a link" is a viable
  v1 UX or whether you need paste+upload as the primary path instead.
- **No hosting yet.** This runs locally. Next step for going live is
  picking a host (Render/Fly.io/a small VPS all work fine for a Flask +
  Playwright app) and pointing a real domain at it.
- **No analytics/lead capture beyond a mailto: link.** The "Être
  recontacté" CTA is a placeholder — swap for a real form once you know
  what you want to capture (name, phone, which upsell they clicked).

## Files

- `app.py` — Flask routes (`/` landing, `/analyze` POST)
- `analyzer.py` — Playwright-based page analysis
- `scoring.py` — scoring rubric, documented inline with the SeLoger
  data points it's modeled on
- `templates/` — landing + results pages (French copy, no JS framework,
  loads fast — good for SEO)
- `static/style.css` — all styling
- `samples/` — two synthetic listing pages used to verify scoring works
  end-to-end without depending on a live portal
