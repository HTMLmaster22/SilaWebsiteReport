#!/usr/bin/env python3
"""
Monthly data refresher for the Silah site performance report.

Pulls:
  1. CrUX History API  -> real-visitor monthly series (LCP, INP, CLS, FCP, TTFB) for the origin
  2. PageSpeed Insights -> Lighthouse performance scores (mobile + desktop) for the homepage

Writes the results into data.json (which the report reads at load time).

Keywords and non-homepage pages are intentionally NOT touched — they stay
manual until Google Search Console API access is set up.

CrUX real-user data is best-effort: lower-traffic origins often don't have
enough anonymized Chrome samples yet for Google to publish a record (a
documented 404/NOT_FOUND response, not an auth or config problem). When that
happens this script skips the real-user chart update for this run but still
refreshes the PSI/Lighthouse scores, so the report never goes stale just
because CrUX has nothing yet.

Env:
  PSI_API_KEY  (required) — Google API key with "Chrome UX Report API" and
                "PageSpeed Insights API" enabled.

Exit codes: 0 = updated (or already current), 1 = hard failure (Action goes red).
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

ORIGIN = "https://silah.com.sa"
DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data.json")

AR_MONTHS = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
             "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]
EN_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
EN_MONTHS_FULL = ["January", "February", "March", "April", "May", "June",
                  "July", "August", "September", "October", "November", "December"]

CRUX_METRIC_MAP = {
    "largest_contentful_paint": "lcp",
    "interaction_to_next_paint": "inp",
    "cumulative_layout_shift": "cls",
    "first_contentful_paint": "fcp",
    "experimental_time_to_first_byte": "ttfb",
    "time_to_first_byte": "ttfb",  # newer name, same series
}

API_KEY = os.environ.get("PSI_API_KEY", "").strip()


def http_json(url, payload=None):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def fetch_crux_history():
    """Return (months:[(y,m)], series:{key:[p75,...]}) from the CrUX History API.

    Raises urllib.error.HTTPError with code 404 if Google has no CrUX record
    for this origin (insufficient anonymized sample volume) — caller decides
    how to handle that.
    """
    url = f"https://chromeuxreport.googleapis.com/v1/records:queryHistoryRecord?key={API_KEY}"
    metric_sets = [
        ["largest_contentful_paint", "interaction_to_next_paint",
         "cumulative_layout_shift", "first_contentful_paint",
         "experimental_time_to_first_byte"],
        ["largest_contentful_paint", "interaction_to_next_paint",
         "cumulative_layout_shift", "first_contentful_paint",
         "time_to_first_byte"],
    ]
    last_err = None
    for mset in metric_sets:
        try:
            # No "formFactor" filter here on purpose — querying all devices
            # combined needs fewer samples than a phone-only segment, which
            # matters for lower-traffic origins.
            body = {"origin": ORIGIN, "metrics": mset}
            resp = http_json(url, body)
            break
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 400:
                continue  # try the alternate TTFB metric name
            raise
    else:
        raise RuntimeError(f"CrUX request failed with both TTFB metric names: {last_err}")

    record = resp["record"]
    periods = record["collectionPeriods"]
    months = [(p["lastDate"]["year"], p["lastDate"]["month"]) for p in periods]

    series = {}
    for api_name, ts in record["metrics"].items():
        key = CRUX_METRIC_MAP.get(api_name)
        if not key:
            continue
        p75s = ts["percentilesTimeseries"]["p75s"]
        vals = []
        for v in p75s:
            if v is None:
                vals.append(None)
            elif key == "cls":
                vals.append(round(float(v), 2))
            else:
                vals.append(int(round(float(v))))
        series[key] = vals

    for key, vals in series.items():
        prev = None
        for i, v in enumerate(vals):
            if v is None:
                vals[i] = prev if prev is not None else 0
            else:
                prev = vals[i]
    return months, series


def fetch_psi_score(strategy):
    url = ("https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
           f"?url={urllib.parse.quote(ORIGIN + '/', safe='')}"
           f"&strategy={strategy}&category=performance&key={API_KEY}")
    resp = http_json(url)
    score = resp["lighthouseResult"]["categories"]["performance"]["score"]
    return int(round(score * 100))


def main():
    if not API_KEY:
        print("ERROR: PSI_API_KEY env var is missing (set it as a repo secret).", file=sys.stderr)
        return 1

    with open(DATA_PATH, encoding="utf-8") as f:
        data = json.load(f)

    months, series = None, None
    try:
        print("Fetching CrUX history for", ORIGIN, "...")
        months, series = fetch_crux_history()
        y_last, m_last = months[-1]
        print("Latest CrUX month:", f"{y_last:04d}-{m_last:02d}", "| points:", len(months))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print("No CrUX record yet for", ORIGIN, "— likely below Google's minimum "
                  "sample threshold. Skipping real-user chart update this run; "
                  "PSI/Lighthouse scores will still refresh below.")
        else:
            raise

    if months is not None:
        y_last, m_last = months[-1]
        new_month = f"{y_last:04d}-{m_last:02d}"
    else:
        now = datetime.now(timezone.utc)
        new_month = f"{now.year:04d}-{now.month:02d}"

    if data.get("reportMonth") == new_month:
        print("data.json already at", new_month, "— nothing to do.")
        return 0

    print("Fetching PageSpeed Insights scores ...")
    mobile_score = fetch_psi_score("mobile")
    desktop_score = fetch_psi_score("desktop")
    print("PSI mobile:", mobile_score, "| desktop:", desktop_score)

    prev_now = data.get("mobilePerfNow")
    data["mobilePerfPrev"] = prev_now if prev_now is not None else data.get("mobilePerfPrev")
    data["mobilePerfNow"] = mobile_score
    data["desktopPerfScore"] = desktop_score
    data["reportMonth"] = new_month

    if months is not None:
        y0, m0 = months[0]
        data["monthLabels"] = {
            "ar": [AR_MONTHS[m - 1] for (_, m) in months],
            "en": [EN_MONTHS[m - 1] for (_, m) in months],
        }
        data["periodLabel"] = {
            "ar": f"{AR_MONTHS[m0-1]} {y0} – {AR_MONTHS[m_last-1]} {y_last}",
            "en": f"{EN_MONTHS[m0-1]} {y0} – {EN_MONTHS[m_last-1]} {y_last}",
        }
        data["latestShort"] = {"ar": AR_MONTHS[m_last-1], "en": EN_MONTHS_FULL[m_last-1]}
        data["latestMonthLabel"] = {
            "ar": f"{AR_MONTHS[m_last-1]} {y_last}",
            "en": f"{EN_MONTHS_FULL[m_last-1]} {y_last}",
        }

        by_key = {m["key"]: m for m in data.get("metrics", [])}
        for key, vals in series.items():
            if key in by_key:
                by_key[key]["data"] = vals
            else:
                data.setdefault("metrics", []).append({"key": key, "data": vals})

    for p in data.get("pages", []):
        if p.get("id") == "home":
            p.setdefault("monthly", {})[new_month] = {
                "mobile": mobile_score, "desktop": desktop_score,
            }

    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print("data.json updated to", new_month)
    return 0


if __name__ == "__main__":
    import urllib.parse  # used in fetch_psi_score
    sys.exit(main())
