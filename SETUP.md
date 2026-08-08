[SETUP.md](https://github.com/user-attachments/files/30862849/SETUP.md)
# Auto-Sync Setup — Silah Site Performance Report

One-time setup (~10 minutes). After this, the report updates itself monthly forever.

## How it works
- `index.html` renders instantly with built-in fallback numbers, then fetches `data.json` and re-renders with the latest data.
- A GitHub Action runs on the **15th of every month** (when Google publishes the previous month's CrUX data), calls the **CrUX History API** + **PageSpeed Insights API**, rewrites `data.json`, and commits. GitHub Pages / Vercel redeploy automatically.
- **Two separate per-page systems** — don't confuse them:
  - **Per-page performance** (`pageRegistry` in `index.html`) — mobile/desktop Lighthouse scores per page. Still **manual**: edit `index.html` directly (see the runbook comment inside `<script>`, item 4).
  - **Per-page health** (`pageHealth` in `data.json`) — Schema.org validity, image alt-text issues, and unused CSS/JS per page. **Fully automatic** as of August 2026 — no editing needed. Covers the 5 pages listed in `PAGE_LIST` inside `scripts/update_data.py`; add a page there to track it, no other file needs touching.
- **Keywords stay manual** (edit `data.json` directly) — real position tracking needs Google Search Console API access (OAuth), which is a separate, larger integration than the API-key-only calls this script already makes. Not set up yet.

## One-time steps

1. **Get a free Google API key**
   - Go to https://console.cloud.google.com/ → create (or pick) a project.
   - APIs & Services → Library → enable **"Chrome UX Report API"** and **"PageSpeed Insights API"**.
   - APIs & Services → Credentials → **Create credentials → API key**. Copy it.

2. **Add the key as a repo secret**
   - Repo → Settings → Secrets and variables → Actions → **New repository secret**
   - Name: `PSI_API_KEY` — Value: the key from step 1.
   - This is the **same key** used for the new page-health scan too — nothing extra to add here even after the August 2026 update.

3. **Copy these files into the repo root** (keeping the folder structure):
   ```
   index.html
   data.json
   scripts/update_data.py
   .github/workflows/update-report-data.yml
   ```

4. **First run (manual test)**
   - Repo → Actions → "Update report data (CrUX + PSI)" → **Run workflow**.
   - Green check = data.json refreshed and committed. The live site updates on the next deploy (automatic).
   - This first run is worth doing manually rather than waiting for the 15th — it's also how the new "Page Health" table gets its first data. Before this runs, that section just shows an honest "not run yet" note instead of blank/fake numbers.

## Notes
- CrUX data is published by Google with a ~28-day lag — "June data" appearing in July is the freshest that exists anywhere. The report explains this in its footer.
- If the Action fails (red X), nothing is overwritten — the site keeps serving the last good data.
- The page-health scan (schema/alt-text/unused CSS-JS) runs **every time the Action runs**, independent of whether CrUX has new data that month — those two things aren't related, so one doesn't block the other.
- To update keywords manually: edit the `keywords` array in `data.json` and commit.
- To add a page to the automatic health scan: add an entry to `PAGE_LIST` near the top of `scripts/update_data.py`. Takes effect on the next run, no other changes needed.
