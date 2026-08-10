[SETUP (2).md](https://github.com/user-attachments/files/30914583/SETUP.2.md)
[SETUP (1).md](https://github.com/user-attachments/files/30863555/SETUP.1.md)
# Auto-Sync Setup — Silah Site Performance Report

One-time setup (~10 minutes). After this, the report updates itself monthly forever.

## How it works
- `index.html` renders instantly with built-in fallback numbers, then fetches `data.json` and re-renders with the latest data.
- A GitHub Action runs on the **15th of every month** (when Google publishes the previous month's CrUX data), calls the **CrUX History API** + **PageSpeed Insights API**, rewrites `data.json`, and commits. GitHub Pages / Vercel redeploy automatically.
- **Two separate per-page systems** — don't confuse them:
  - **Per-page performance** (`pageRegistry` in `index.html`) — mobile/desktop Lighthouse scores per page. Still **manual**: edit `index.html` directly (see the runbook comment inside `<script>`, item 4).
  - **Per-page health** (`pageHealth` in `data.json`) — Schema.org validity, image alt-text issues, unused CSS/JS, and SEO audit findings per page. **Fully automatic** as of August 2026, including **which pages get scanned** — the script reads the site's own Yoast sitemap every run and discovers pages on its own (capped at 30 per run to keep runtime reasonable). New pages need no code change; add an entry to `KNOWN_PAGE_NAMES` in `scripts/update_data.py` only if you want a specific bilingual name instead of the auto-extracted `<title>`.
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
- The page-health scan (schema/alt-text/unused CSS-JS/SEO) runs **every time the Action runs**, independent of whether CrUX has new data that month — those two things aren't related, so one doesn't block the other.
- Pages for the health scan are **discovered automatically from the site's sitemap** — nothing to maintain by hand. If the sitemap can't be reached that run, it falls back to a fixed 5-page list rather than scanning nothing. Capped at 30 pages per run (`MAX_AUTO_PAGES` in the script) to keep run time reasonable — raise that constant if the real page count grows past it.
- To update keywords manually: edit the `keywords` array in `data.json` and commit.
- To give a specific page a proper bilingual name (instead of its auto-extracted `<title>`) or a specific expected Schema type: add it to `KNOWN_PAGE_NAMES` near the top of `scripts/update_data.py`.

## SEO report PDF (added Aug 2026)
"Download SEO Report (PDF)" button lives in the SEO section. On click it builds the report from the same in-memory `pageHealthData`/`keywords`/`computePriorities()` the live tables already use — not a separate fetch — so it's always exactly what's on screen at that moment, never stale. Renders into an off-screen template, rasterizes with html2canvas, paginates across A4 pages with jsPDF (same library versions already in use on silah.com.sa's own cost-calculator PDF button). Text logic is covered by a Node test against real `data.json` (checks every page/keyword name appears, no `undefined`/`NaN` leaks, balanced HTML). The actual visual PDF output — Arabic text shaping, page breaks, spacing — has **not** been checked in a real browser and should be before relying on it; open the live site, click the button, and look at the file.
