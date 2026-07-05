# Auto-Sync Setup — Silah Site Performance Report

One-time setup (~10 minutes). After this, the report updates itself monthly forever.

## How it works
- `index.html` renders instantly with built-in fallback numbers, then fetches `data.json` and re-renders with the latest data.
- A GitHub Action runs on the **15th of every month** (when Google publishes the previous month's CrUX data), calls the **CrUX History API** + **PageSpeed Insights API**, rewrites `data.json`, and commits. GitHub Pages / Vercel redeploy automatically.
- **Keywords and non-homepage pages stay manual** (edit `data.json` directly) until Google Search Console API access is completed.

## One-time steps

1. **Get a free Google API key**
   - Go to https://console.cloud.google.com/ → create (or pick) a project.
   - APIs & Services → Library → enable **"Chrome UX Report API"** and **"PageSpeed Insights API"**.
   - APIs & Services → Credentials → **Create credentials → API key**. Copy it.

2. **Add the key as a repo secret**
   - Repo → Settings → Secrets and variables → Actions → **New repository secret**
   - Name: `PSI_API_KEY` — Value: the key from step 1.

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

## Notes
- CrUX data is published by Google with a ~28-day lag — "June data" appearing in July is the freshest that exists anywhere. The report explains this in its footer.
- If the Action fails (red X), nothing is overwritten — the site keeps serving the last good data.
- To update keywords manually: edit the `keywords` array in `data.json` and commit.
