#!/usr/bin/env python3
"""
Monthly data refresher for the Silah site performance report.

Pulls:
  1. CrUX History API   -> real-visitor monthly series (LCP, INP, CLS, FCP, TTFB) for the origin
  2. PageSpeed Insights -> Lighthouse performance scores (mobile + desktop) for the homepage,
                            plus a per-page health scan (see below)
  3. Per-page health scan -> for each URL in PAGE_LIST: fetches the live HTML directly and
                            checks Schema.org structured data + image alt text, and pulls
                            unused-CSS / unused-JS byte estimates from the same PSI call
                            already being made for that page's performance score.

Writes the results into data.json (which the report reads at load time).

Keyword RANKINGS are intentionally NOT touched — real position tracking needs
Google Search Console API access (a separate, larger integration), so that
part stays manual until that's set up. Everything else below is automatic.

CrUX real-user data is best-effort: lower-traffic origins/pages often don't
have enough anonymized Chrome samples yet for Google to publish a record (a
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
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ORIGIN = "https://www.silah.com.sa"
DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data.json")

# Pages checked for Schema/alt-text/unused-CSS-JS on every run, independent of
# the CrUX-based homepage chart above. Add a page here and it's covered next run —
# no other code changes needed. `expectSchemaType` is just what we expect to see
# (used to flag it in the report as missing); it doesn't restrict what's detected.
PAGE_LIST = [
    {"id": "otj_training", "url": f"{ORIGIN}/otj-training-services/",
     "nameAr": "التوطين عبر معاهد الشراكات الاستراتيجية", "nameEn": "OTJ Training via Strategic Partnerships",
     "expectSchemaType": "Service"},
    {"id": "engineering_center", "url": f"{ORIGIN}/engineering-technician-center/",
     "nameAr": "خدمات توطين المهن الفنية الهندسية", "nameEn": "Engineering Technician Localization",
     "expectSchemaType": "Service"},
    {"id": "saudi_hiring", "url": f"{ORIGIN}/%D8%AE%D8%AF%D9%85%D8%A7%D8%AA-%D8%AA%D9%88%D8%B8%D9%8A%D9%81-%D8%A7%D9%84%D8%B3%D8%B9%D9%88%D8%AF%D9%8A%D9%8A%D9%86/",
     "nameAr": "خدمات توظيف السعوديين", "nameEn": "Saudi Hiring Services",
     "expectSchemaType": "Service"},
    {"id": "training_disclosure", "url": f"{ORIGIN}/training-disclosure-services/",
     "nameAr": "بناء وتنفيذ خطة الإفصاح التدريبي", "nameEn": "Training Disclosure Plan",
     "expectSchemaType": "Service"},
    {"id": "outsourcing", "url": f"{ORIGIN}/outsourcing-services/",
     "nameAr": "خدمات تعهيد الأعمال", "nameEn": "Business Outsourcing Services",
     "expectSchemaType": "Service"},
]

# alt="" or alt attribute missing entirely, or a generic single-word
# placeholder that isn't real descriptive text (case-insensitive).
GENERIC_ALT_VALUES = {"icon", "image", "img", "photo", "logo", ""}

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
            body = {"origin": ORIGIN, "metrics": mset}
            resp = http_json(url, body)
            break
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 400:
                continue
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


SKIP_SEO_AUDITS = {"image-alt"}  # redundant with check_alt_text() below, which is more
                                  # precise (names the actual image, Lighthouse just says yes/no)


def fetch_psi_full(page_url, strategy):
    """Like fetch_psi_score, but for an arbitrary URL and returns the extra
    diagnostics — unused CSS/JS byte estimates AND failing SEO-category audits
    — from the same Lighthouse run. No separate API call for either; Lighthouse
    already computes an "seo" category alongside "performance" on every run,
    we just weren't reading it before."""
    url = ("https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
           f"?url={urllib.parse.quote(page_url, safe='')}"
           f"&strategy={strategy}&category=performance&category=seo&key={API_KEY}")
    resp = http_json(url)
    lh = resp.get("lighthouseResult", {})
    audits = lh.get("audits", {})
    score = lh["categories"]["performance"]["score"]

    def savings_kb(audit_id):
        bytes_ = audits.get(audit_id, {}).get("details", {}).get("overallSavingsBytes")
        return round(bytes_ / 1024) if isinstance(bytes_, (int, float)) else None

    # Only binary/numeric-scored SEO audits that actually failed (score != 1).
    # Skips manual-only checks Lighthouse can't auto-verify (e.g. structured-data
    # has scoreDisplayMode="manual" and is excluded by the filter below on its own).
    seo_issues = []
    seo_cat = lh.get("categories", {}).get("seo", {})
    for ref in seo_cat.get("auditRefs", []):
        aid = ref.get("id")
        if aid in SKIP_SEO_AUDITS:
            continue
        a = audits.get(aid, {})
        if a.get("scoreDisplayMode") not in ("binary", "numeric"):
            continue
        a_score = a.get("score")
        if a_score is None or a_score >= 1:
            continue
        seo_issues.append({"id": aid, "title": a.get("title", aid)})

    return {
        "score": int(round(score * 100)),
        "unusedCssKb": savings_kb("unused-css-rules"),
        "unusedJsKb": savings_kb("unused-javascript"),
        "seoIssues": seo_issues,
    }


def fetch_html(page_url):
    """Fetch a page's rendered-server HTML. Returns None on any failure —
    callers treat a missing fetch as 'skip this page this run', matching the
    existing best-effort pattern used for CrUX above (a page hiccup shouldn't
    fail the whole monthly run)."""
    try:
        req = urllib.request.Request(page_url, headers={"User-Agent": "Mozilla/5.0 (compatible; SilahReportBot/1.0)"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  WARNING: could not fetch {page_url}: {e}", file=sys.stderr)
        return None


def check_schema(html):
    """Pulls every <script type="application/ld+json"> block out of the page
    and reports which @type values are present. Regex-based on purpose (no
    extra pip installs / no HTML parser dependency for the Action to manage) —
    fine here because we're only looking for a well-formed <script> tag, not
    parsing arbitrary HTML structure."""
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    )
    types_found = []
    for b in blocks:
        try:
            parsed = json.loads(b.strip())
        except json.JSONDecodeError:
            continue
        candidates = parsed if isinstance(parsed, list) else [parsed]
        for c in candidates:
            if isinstance(c, dict) and "@type" in c:
                t = c["@type"]
                types_found.extend(t if isinstance(t, list) else [t])
            # some sites nest an @graph array (e.g. Yoast) — check inside it too
            for g in (c.get("@graph") if isinstance(c, dict) else None) or []:
                if isinstance(g, dict) and "@type" in g:
                    t = g["@type"]
                    types_found.extend(t if isinstance(t, list) else [t])
    return {"blockCount": len(blocks), "types": sorted(set(types_found))}


def check_alt_text(html, page_url):
    """Flags <img> tags with a missing, empty, or generic-placeholder alt
    attribute — but only among real, same-origin content images. Two kinds
    of noise deliberately excluded, found by inspecting the first real scan's
    output on Aug 8 2026:
      - data: URIs (inline SVGs/icons) — not a checkable "image with a src",
        and rsplit("/")-ing the encoded data itself produced garbage filenames.
      - third-party images (chat widgets, tracking pixels, embeds) — these
        load from a different domain than the page, which is a reliable general
        signal without having to hardcode specific vendor patterns. Not ours
        to add alt text to even if flagged.
    `totalImages` counts only these checkable images too, so the ratio in the
    report (e.g. "1 of 6") means what it looks like it means."""
    imgs = re.findall(r'<img\b[^>]*>', html, re.IGNORECASE)
    checkable = 0
    issues = []
    for tag in imgs:
        src_match = re.search(r'src=["\']([^"\']*)["\']', tag, re.IGNORECASE)
        src = src_match.group(1).strip() if src_match else ""
        if not src or src.startswith("data:"):
            continue
        resolved = urllib.parse.urljoin(page_url, src)
        if not resolved.startswith(ORIGIN):
            continue
        checkable += 1
        alt_match = re.search(r'alt=["\']([^"\']*)["\']', tag, re.IGNORECASE)
        alt_val = alt_match.group(1).strip() if alt_match else None
        is_bad = alt_val is None or alt_val.lower() in GENERIC_ALT_VALUES
        if is_bad:
            issues.append(resolved.rsplit("/", 1)[-1])
    return {"totalImages": checkable, "issueCount": len(issues), "examples": issues[:5]}


def run_page_health_scan():
    """One pass over PAGE_LIST: HTML-based schema/alt-text checks + a PSI run
    per page for score + unused CSS/JS. Any single page failing doesn't stop
    the others — each result just gets marked unavailable for this run."""
    results = []
    for page in PAGE_LIST:
        print(f"  Scanning {page['id']} ({page['url']}) ...")
        entry = {"id": page["id"], "nameAr": page["nameAr"], "nameEn": page["nameEn"],
                  "url": page["url"], "checkedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d")}

        html = fetch_html(page["url"])
        if html is not None:
            schema = check_schema(html)
            entry["schema"] = {
                "hasExpectedType": page["expectSchemaType"] in schema["types"],
                "typesFound": schema["types"],
            }
            entry["altText"] = check_alt_text(html, page["url"])
        else:
            entry["schema"] = None
            entry["altText"] = None

        try:
            psi = fetch_psi_full(page["url"], "mobile")
            entry["mobileScore"] = psi["score"]
            entry["unusedCssKb"] = psi["unusedCssKb"]
            entry["unusedJsKb"] = psi["unusedJsKb"]
            entry["seoIssues"] = psi["seoIssues"]
        except Exception as e:
            print(f"  WARNING: PSI failed for {page['id']}: {e}", file=sys.stderr)
            entry["mobileScore"] = None
            entry["unusedCssKb"] = None
            entry["unusedJsKb"] = None
            entry["seoIssues"] = None

        results.append(entry)
    return results


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

    # NOTE: the CrUX/PSI-homepage block below is gated on new_month actually
    # advancing (Google only publishes new CrUX data periodically). The
    # page-health scan further down is NOT gated on that — schema/alt-text/
    # unused-CSS-JS can change on the site at any time and have nothing to do
    # with CrUX's publish cadence, so it always runs once per invocation
    # (i.e. every scheduled month, or on demand via workflow_dispatch).
    # An earlier version of this script returned early here before the page
    # scan ever ran, which meant page-health silently never updated on any
    # month CrUX didn't advance — that's fixed by not early-returning.
    homepage_updated = data.get("reportMonth") != new_month

    if not homepage_updated:
        print("CrUX/homepage data already at", new_month, "— skipping that part, still running page-health scan below.")
    else:
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

    print("Scanning per-page health (schema, alt text, unused CSS/JS) ...")
    data["pageHealth"] = run_page_health_scan()
    data["pageHealthCheckedMonth"] = new_month

    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print("data.json updated —", "homepage+" if homepage_updated else "", "page-health refreshed for", new_month)
    return 0


if __name__ == "__main__":
    sys.exit(main())
