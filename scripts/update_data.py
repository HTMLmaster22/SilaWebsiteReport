#!/usr/bin/env python3
"""
Monthly data refresher for the Silah site performance report.

Pulls:
  1. CrUX History API   -> real-visitor monthly series (LCP, INP, CLS, FCP, TTFB) for the origin
  2. PageSpeed Insights -> Lighthouse performance scores (mobile + desktop) for the homepage,
                            plus a per-page health scan (see below)
  3. Per-page health scan -> pages are now auto-discovered from the site's own
                            Yoast sitemap every run (see discover_pages_from_sitemap()),
                            not a hand-maintained list. For each discovered URL: fetches
                            the live HTML directly and checks Schema.org structured data
                            + image alt text, and pulls unused-CSS / unused-JS byte
                            estimates plus failing SEO-category audits from the same PSI
                            call already being made for that page's performance score.

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
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

ORIGIN = "https://www.silah.com.sa"
DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data.json")

# Safety cap on how many pages get the full scan (HTML fetch + a PSI/Lighthouse
# run each) in a single execution. PSI calls typically take 5-15s apiece, so at
# ~30 pages this run stays in the few-minutes range instead of risking a very
# long or rate-limited Action run. Raise this if the real sitemap needs more —
# the discovery logic itself has no built-in limit, this is deliberate.
MAX_AUTO_PAGES = 30

# Seconds to wait between each page's direct HTML fetch in run_page_health_scan().
# Added Aug 2026 after the Aug 15 run showed only page 1 (home) getting real
# schema/alt-text data and the other 29 coming back null — a same-day repro
# of two unrelated pages returning HTTP 429 pointed at the site's own
# rate-limiting/WAF reacting to a burst of same-IP requests, not a code bug
# (the Aug 9 run, same code, same 30 URLs, succeeded 30/30). PSI-based fields
# (mobileScore, unusedCssKb/JsKb, seoIssues) are unaffected either way since
# those come from Google's PSI servers hitting the site, not this fetch.
PAGE_FETCH_DELAY_SECONDS = 2
# Extra wait before a single retry if a fetch still fails — separate from the
# steady per-page delay above, since a failure is a stronger signal to back
# off further than the routine gap between pages.
PAGE_FETCH_RETRY_BACKOFF_SECONDS = 5

# Manually-curated bilingual names + expected Schema type for pages already
# worked on directly (Aug 2026 SEO pass). Anything the sitemap discovers that
# ISN'T listed here still gets scanned — it just falls back to (a) the page's
# own <title> tag for a name (Arabic only; this site's titles are Arabic-first
# and there's no reliable way to auto-translate, so until someone adds a real
# translation here the EN view will show the same Arabic text), and (b) "does
# this page have ANY valid schema at all" instead of checking for one specific
# type, since we don't know in advance what type an arbitrary new page should have.
KNOWN_PAGE_NAMES = {
    "otj-training-services": {
        "nameAr": "التوطين عبر معاهد الشراكات الاستراتيجية",
        "nameEn": "OTJ Training via Strategic Partnerships", "expectSchemaType": "Service"},
    "engineering-technician-center": {
        "nameAr": "خدمات توطين المهن الفنية الهندسية",
        "nameEn": "Engineering Technician Localization", "expectSchemaType": "Service"},
    "training-disclosure-services": {
        "nameAr": "بناء وتنفيذ خطة الإفصاح التدريبي",
        "nameEn": "Training Disclosure Plan", "expectSchemaType": "Service"},
    "outsourcing-services": {
        "nameAr": "خدمات تعهيد الأعمال",
        "nameEn": "Business Outsourcing Services", "expectSchemaType": "Service"},
}
# The Saudi-hiring page's slug is fully Arabic and WordPress stores it
# percent-encoded internally — matching by substring in the URL instead of
# an exact slug comparison, same workaround needed for the Code Snippets
# is_page() check earlier today (dashes vs. spaces caused a silent mismatch
# there; percent-encoding could do the same here, so URL-substring is safer
# than an exact-match on a decoded slug).
KNOWN_PAGE_NAMES_BY_URL_SUBSTRING = {
    "%d8%aa%d9%88%d8%b8%d9%8a%d9%81-%d8%a7%d9%84%d8%b3%d8%b9%d9%88%d8%af%d9%8a%d9%8a%d9%86": {
        "nameAr": "خدمات توظيف السعوديين", "nameEn": "Saudi Hiring Services", "expectSchemaType": "Service"},
}

# Used only if sitemap discovery fails outright (network error, unexpected
# site structure, etc.) — a sitemap hiccup should never mean "scan zero pages
# this month." This is exactly the fixed 5-page list from before auto-discovery.
PAGE_LIST_FALLBACK = [
    {"id": "otj_training", "url": f"{ORIGIN}/otj-training-services/", **KNOWN_PAGE_NAMES["otj-training-services"]},
    {"id": "engineering_center", "url": f"{ORIGIN}/engineering-technician-center/", **KNOWN_PAGE_NAMES["engineering-technician-center"]},
    {"id": "saudi_hiring", "url": f"{ORIGIN}/%D8%AE%D8%AF%D9%85%D8%A7%D8%AA-%D8%AA%D9%88%D8%B8%D9%8A%D9%81-%D8%A7%D9%84%D8%B3%D8%B9%D9%88%D8%AF%D9%8A%D9%8A%D9%86/",
     "nameAr": "خدمات توظيف السعوديين", "nameEn": "Saudi Hiring Services", "expectSchemaType": "Service"},
    {"id": "training_disclosure", "url": f"{ORIGIN}/training-disclosure-services/", **KNOWN_PAGE_NAMES["training-disclosure-services"]},
    {"id": "outsourcing", "url": f"{ORIGIN}/outsourcing-services/", **KNOWN_PAGE_NAMES["outsourcing-services"]},
]

SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# alt="" or alt attribute missing entirely, or a generic single-word
# placeholder that isn't real descriptive text (case-insensitive).
GENERIC_ALT_VALUES = {"icon", "image", "img", "photo", "logo", ""}


def slug_from_url(url):
    """Path-based slug, not domain-based — url.rsplit("/") alone breaks on
    the homepage URL itself (the // after https: is also a "/", so naive
    splitting returns the domain name instead of a real slug). Caught this
    with a homepage-URL test case; urlparse's .path avoids the whole class
    of bug by only ever looking at the path component."""
    path = urllib.parse.urlparse(url).path.strip("/")
    return path.rsplit("/", 1)[-1] if path else "home"


def discover_pages_from_sitemap():
    """Auto-discovers every WordPress 'Page' URL from this site's Yoast-
    generated sitemap instead of relying on a hand-maintained list. Standard
    Yoast structure (confirmed installed on this site — Yoast SEO v27.9):
    sitemap_index.xml lists one sub-sitemap per post type, one of which is
    page-sitemap.xml (or page-sitemap1.xml, page-sitemap2.xml, ... if there
    are enough pages that Yoast splits them — it paginates at 200 URLs per
    file, hence matching by substring below rather than an exact filename).
    Returns a list of URLs (capped at MAX_AUTO_PAGES, with a warning logged
    if the real count is higher), or None — not an empty list — on any
    failure, so the caller can tell "genuinely zero pages" apart from
    "something went wrong" and fall back to PAGE_LIST_FALLBACK accordingly."""
    index_xml = fetch_html(f"{ORIGIN}/sitemap_index.xml")
    if not index_xml:
        print("WARNING: could not fetch sitemap_index.xml", file=sys.stderr)
        return None
    try:
        root = ET.fromstring(index_xml)
    except ET.ParseError as e:
        print(f"WARNING: sitemap_index.xml did not parse as XML: {e}", file=sys.stderr)
        return None

    sub_sitemaps = [loc.text.strip() for loc in root.findall(".//sm:loc", SITEMAP_NS) if loc.text]
    page_sitemaps = [s for s in sub_sitemaps if "page-sitemap" in s]
    if not page_sitemaps:
        print("WARNING: no page-sitemap*.xml listed in sitemap_index.xml — "
              "site's sitemap structure may not match the expected Yoast layout.",
              file=sys.stderr)
        return None

    urls = []
    for sm_url in page_sitemaps:
        sm_xml = fetch_html(sm_url)
        if not sm_xml:
            print(f"WARNING: could not fetch {sm_url}, skipping it", file=sys.stderr)
            continue
        try:
            sm_root = ET.fromstring(sm_xml)
        except ET.ParseError as e:
            print(f"WARNING: {sm_url} did not parse as XML: {e}", file=sys.stderr)
            continue
        urls.extend(loc.text.strip() for loc in sm_root.findall(".//sm:loc", SITEMAP_NS) if loc.text)

    if not urls:
        return None
    if len(urls) > MAX_AUTO_PAGES:
        print(f"WARNING: sitemap has {len(urls)} pages — scanning the first "
              f"{MAX_AUTO_PAGES} this run (raise MAX_AUTO_PAGES for more).",
              file=sys.stderr)
    return urls[:MAX_AUTO_PAGES]


def build_page_list():
    """Combines automatic sitemap discovery with the known bilingual names
    above. Falls back to PAGE_LIST_FALLBACK if discovery fails for any reason."""
    discovered = discover_pages_from_sitemap()
    if not discovered:
        print("Sitemap discovery unavailable this run — using the fixed 5-page fallback list.")
        return PAGE_LIST_FALLBACK

    pages = []
    for url in discovered:
        slug = slug_from_url(url)
        known = KNOWN_PAGE_NAMES.get(slug)
        if not known:
            url_lower = url.lower()
            known = next((v for k, v in KNOWN_PAGE_NAMES_BY_URL_SUBSTRING.items() if k in url_lower), None)
        pages.append({
            "id": slug,
            "url": url,
            "nameAr": known["nameAr"] if known else None,   # filled from <title> below if still None
            "nameEn": known["nameEn"] if known else None,
            "expectSchemaType": known.get("expectSchemaType") if known else None,
        })
    return pages


def extract_title(html):
    """Pulls the page's own <title> text as a fallback display name for
    pages without a curated entry in KNOWN_PAGE_NAMES. WordPress/Yoast titles
    are usually "Page Name | Site Name" — trims that suffix so the report
    shows just the page-specific part, not the same site name on every row."""
    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    title = re.sub(r'\s+', ' ', m.group(1)).strip()
    for sep in (" | ", " – ", " - "):
        if sep in title:
            title = title.split(sep)[0].strip()
            break
    return title or None

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


def http_json(url, payload=None, extra_headers=None):
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
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
    """Homepage PSI run for the given strategy. Returns all four Lighthouse
    category scores from one call -- Performance, SEO, Accessibility, and
    Best Practices are all computed together by Lighthouse regardless of
    which categories you ask for in scoring terms; requesting them
    explicitly just makes the API return them. Previously this only asked
    for (and returned) performance, so the SEO/Best Practices/Accessibility
    score cards on the site were hand-typed once and never updated by any
    automated process since -- correct fix is to actually read the numbers
    already present in the response we were already making, not add a
    second request."""
    url = ("https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
           f"?url={urllib.parse.quote(ORIGIN + '/', safe='')}"
           f"&strategy={strategy}&category=performance&category=seo"
           f"&category=accessibility&category=best-practices&key={API_KEY}")
    resp = http_json(url)
    cats = resp["lighthouseResult"]["categories"]

    def pct(key):
        c = cats.get(key, {})
        s = c.get("score")
        return int(round(s * 100)) if isinstance(s, (int, float)) else None

    return {
        "performance": pct("performance"),
        "seo": pct("seo"),
        "accessibility": pct("accessibility"),
        "bestPractices": pct("best-practices"),
    }


SKIP_SEO_AUDITS = {"image-alt"}  # redundant with check_alt_text() below, which is more
                                  # precise (names the actual image, Lighthouse just says yes/no)


def get_gsc_access_token():
    """Loads the service account from the GSC_SERVICE_ACCOUNT_JSON secret and
    exchanges it for a short-lived access token. Returns None (not an
    exception) if the secret isn't set yet, or if auth fails for any
    reason - GSC data is a "nice to have on top of" the rest of this
    script, not something that should take down a run that would
    otherwise succeed. Requires google-auth (added to the workflow's pip
    install step alongside this function - unlike everything else in this
    file, correctly signing a service-account JWT isn't something worth
    hand-rolling against stdlib; this is exactly the kind of auth-critical
    code where the well-audited official library is the right call).

    Read-only scope on purpose - this integration only ever needs to query
    existing Search Analytics data, never modify anything about the
    property."""
    raw = os.environ.get("GSC_SERVICE_ACCOUNT_JSON")
    if not raw:
        print("  GSC_SERVICE_ACCOUNT_JSON not set - skipping GSC, keywords stay as-is this run.")
        return None
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request as GoogleAuthRequest
        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/webmasters.readonly"]
        )
        creds.refresh(GoogleAuthRequest())
        return creds.token
    except Exception as e:
        print(f"  WARNING: GSC auth failed, keywords stay as-is this run: {e}", file=sys.stderr)
        return None


# Silah's GSC property was verified via DNS TXT record (see project history),
# which is the verification method specific to Domain properties, not
# URL-prefix ones - so sc-domain: is the expected format. Falls back to the
# URL-prefix format automatically if that guess is wrong, rather than just
# failing outright on a property-type mismatch we can recover from.
GSC_SITE_URL_CANDIDATES = ["sc-domain:silah.com.sa", "https://www.silah.com.sa/"]


def fetch_gsc_position(access_token, keyword_query, days=28):
    """Average position/clicks/impressions over the trailing `days` for
    everything Search Console logged containing `keyword_query` - a
    "contains" match rather than exact, since real searches rarely match a
    tracked phrase word-for-word, and this is meant to track how the TOPIC
    is doing, not one exact string. Returns None if there's no data for
    this phrase in the window (genuinely not appearing in any real search,
    as opposed to appearing but ranking poorly - those are different
    findings and shouldn't be conflated)."""
    from datetime import timedelta
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    payload = {
        "startDate": start.isoformat(), "endDate": end.isoformat(),
        "dimensions": ["query"], "rowLimit": 1,
        "dimensionFilterGroups": [{"filters": [
            {"dimension": "query", "operator": "contains", "expression": keyword_query}
        ]}],
    }
    headers = {"Authorization": f"Bearer {access_token}"}
    last_err = None
    for site_url in GSC_SITE_URL_CANDIDATES:
        endpoint = ("https://searchconsole.googleapis.com/webmasters/v3/sites/"
                    f"{urllib.parse.quote(site_url, safe='')}/searchAnalytics/query")
        try:
            resp = http_json(endpoint, payload=payload, extra_headers=headers)
            rows = resp.get("rows", [])
            if not rows:
                return None
            r = rows[0]
            return {"position": r["position"], "clicks": r["clicks"], "impressions": r["impressions"]}
        except urllib.error.HTTPError as e:
            last_err = e
            continue  # try the next site_url candidate - likely a property-type mismatch
    if last_err:
        print(f"  WARNING: GSC query failed for '{keyword_query}' against both site URL formats: {last_err}", file=sys.stderr)
    return None


def update_keywords_with_gsc(keywords, access_token):
    """Updates each tracked keyword's pos/page/tier IN PLACE from real GSC
    data. position is GSC's average over the window as a float (e.g. 6.8);
    converted here to the report's existing page/pos pair the same way
    Google's own results pages are numbered - 10 results per page, so
    overall rank 15 is page 2, position 5 on that page. A keyword with no
    GSC rows this window keeps its previous manually-recorded value rather
    than being overwritten with a false null - going from "we measured
    this once" to "we have no idea" isn't right either. Only ever called
    with a real access_token; caller skips this entirely when auth failed,
    so keywords silently keep their last-known values on any GSC outage."""
    import math
    for kw in keywords:
        result = fetch_gsc_position(access_token, kw["ar"])
        if result is None:
            continue
        overall_rank = round(result["position"])
        page = max(1, math.ceil(overall_rank / 10))
        pos = overall_rank - (page - 1) * 10
        kw["pos"] = pos
        kw["page"] = page
        kw["tier"] = "strong" if page == 1 else "weak"
        kw["gscClicks"] = result["clicks"]
        kw["gscImpressions"] = round(result["impressions"])


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


def fetch_html(page_url, retry=True):
    """Fetch a page's rendered-server HTML. Returns None on any failure —
    callers treat a missing fetch as 'skip this page this run', matching the
    existing best-effort pattern used for CrUX above (a page hiccup shouldn't
    fail the whole monthly run).

    Retries once after PAGE_FETCH_RETRY_BACKOFF_SECONDS on failure (still a
    single extra attempt, not a loop) — cheap insurance against a transient
    rate-limit/WAF response on an otherwise-fine page, without turning one
    stuck page into a long hang."""
    try:
        req = urllib.request.Request(page_url, headers={"User-Agent": "Mozilla/5.0 (compatible; SilahReportBot/1.0)"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        if retry:
            print(f"  WARNING: fetch failed for {page_url}, retrying once in "
                  f"{PAGE_FETCH_RETRY_BACKOFF_SECONDS}s: {e}", file=sys.stderr)
            time.sleep(PAGE_FETCH_RETRY_BACKOFF_SECONDS)
            return fetch_html(page_url, retry=False)
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
            issues.append({"file": resolved.rsplit("/", 1)[-1], "url": resolved})
    # "examples" used to be capped at issues[:5] — kept only a sample, so the
    # report could show a count but never the full picture. Now that the report
    # has a click-to-expand detail view (Aug 9 2026), it needs every flagged
    # filename, not a truncated sample, so the list here is complete.
    # Each example carries the full resolved `url` alongside `file` (Aug 2026)
    # so the report can link straight to the image instead of showing a bare
    # filename with nothing to click - `resolved` was already being computed
    # above, it just wasn't being kept past the rsplit that trims it to a
    # display name.
    return {"totalImages": checkable, "issueCount": len(issues), "examples": issues}


def run_page_health_scan():
    """One pass over the auto-discovered page list: HTML-based schema/alt-text
    checks + a PSI run per page for score, unused CSS/JS, and SEO issues. Any
    single page failing doesn't stop the others — each result just gets marked
    unavailable for this run."""
    page_list = build_page_list()
    print(f"  Page list: {len(page_list)} page(s) to scan this run.")
    results = []
    for i, page in enumerate(page_list):
        if i > 0:
            time.sleep(PAGE_FETCH_DELAY_SECONDS)
        print(f"  Scanning {page['id']} ({page['url']}) ...")
        entry = {"id": page["id"], "nameAr": page["nameAr"], "nameEn": page["nameEn"],
                  "url": page["url"], "checkedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
        if not entry["nameAr"]:
            # Baseline fallback before we even try the fetch: the slug, so a
            # failed fetch below still leaves a real (if unpolished) name
            # instead of null. Upgraded to the page's actual <title> further
            # down if the fetch succeeds and a nicer name is available.
            entry["nameAr"] = page["id"]
            entry["nameEn"] = page["id"]

        html = fetch_html(page["url"])
        if html is not None:
            if not page.get("nameAr"):
                # No curated name for this one — use the page's own <title>
                # for both languages (see extract_title()'s docstring for why
                # there's no separate EN version: no reliable auto-translation).
                extracted = extract_title(html) or page["id"]
                entry["nameAr"] = extracted
                entry["nameEn"] = extracted
            schema = check_schema(html)
            expected = page.get("expectSchemaType")
            has_expected = (expected in schema["types"]) if expected else (len(schema["types"]) > 0)
            entry["schema"] = {"hasExpectedType": has_expected, "typesFound": schema["types"]}
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


# Slugs excluded from the SEO score: checkout/cart/account funnel pages
# (never meant to be indexed or ranked - noindex is the real fix for these,
# not a meta description), one leftover test page, and expired trade-show
# landing pages. Matched against the URL-decoded slug so the Arabic ones
# are readable here instead of raw percent-encoding. Excluded pages still
# get scanned normally and still appear in the table - only the aggregate
# score skips them, since averaging in "does the shopping cart page have a
# meta description" would punish the score for something that was never a
# real SEO target. Aug 2026: see the pageHealth review that flagged these.
SCORE_EXCLUDED_SLUGS = {
    "طلب-باقة", "عربة-التسوق", "الدفع",                                # checkout/cart funnel
    "user-account", "user-public-account", "wishlist", "thank-you",       # account funnel
    "normal-form-test",                                                  # leftover test page
    "gitex2022", "gitex2023", "gitex2023_en", "gitex-form",              # expired trade-show pages
}


def is_excluded_from_score(page_id):
    return urllib.parse.unquote(page_id) in SCORE_EXCLUDED_SLUGS


def compute_page_seo_score(entry):
    """Weighted 0-100 SEO score for one pageHealth entry, built entirely from
    checks already being collected here - nothing new to fetch. Weights are
    loosely modeled on common external audit frameworks (technical/schema,
    on-page, performance, and images all contribute); this scanner doesn't
    check content-quality/E-E-A-T or AI-search-readiness, so those aren't
    part of this score.

    Each component is (points_earned, points_possible). A component whose
    underlying check is unavailable this run (null, same reasons as
    elsewhere in this file) is left out of BOTH the numerator and the
    denominator, so a page isn't punished for a check that didn't run -
    same graceful-degradation principle used throughout this script.
    Returns None if every component is unavailable."""
    parts = []

    if entry.get("schema") is not None:
        if entry["schema"]["hasExpectedType"]:
            pts = 20
        elif entry["schema"]["typesFound"]:
            pts = 10  # has *some* schema, just not the expected type
        else:
            pts = 0
        parts.append((pts, 20))

    if entry.get("altText") is not None:
        total = entry["altText"]["totalImages"]
        issues = entry["altText"]["issueCount"]
        ratio = 1.0 if total == 0 else max(0.0, (total - issues) / total)
        parts.append((round(20 * ratio), 20))

    if entry.get("seoIssues") is not None:
        parts.append((max(0, 20 - len(entry["seoIssues"]) * 10), 20))

    if entry.get("mobileScore") is not None:
        parts.append((round(entry["mobileScore"] / 100 * 25), 25))

    if entry.get("unusedCssKb") is not None and entry.get("unusedJsKb") is not None:
        # Full 15 pts under ~20KB unused CSS / ~50KB unused JS, tapering to
        # 0 at roughly 3x those thresholds. Bounded by whichever is worse.
        css_frac = max(0.0, 1 - max(0, entry["unusedCssKb"] - 20) / 40)
        js_frac = max(0.0, 1 - max(0, entry["unusedJsKb"] - 50) / 100)
        parts.append((round(15 * min(css_frac, js_frac)), 15))

    if not parts:
        return None
    earned = sum(p[0] for p in parts)
    possible = sum(p[1] for p in parts)
    return round(earned / possible * 100)


def compute_site_seo_score(page_health):
    """Adds `seoScore` (0-100, or null if nothing to score) and
    `excludedFromScore` (bool) onto each pageHealth entry IN PLACE, and
    returns the overall site score - a plain average across included pages
    that have at least one scoreable component - or None if nothing on the
    whole list could be scored (e.g. every fetch failed this run)."""
    scored = []
    for entry in page_health:
        excluded = is_excluded_from_score(entry["id"])
        entry["excludedFromScore"] = excluded
        if excluded:
            entry["seoScore"] = None
            continue
        s = compute_page_seo_score(entry)
        entry["seoScore"] = s
        if s is not None:
            scored.append(s)
    return round(sum(scored) / len(scored)) if scored else None


# AI crawlers worth checking explicitly for AI-search visibility (ChatGPT,
# Claude, Perplexity) as distinct from Google-Extended, which governs
# Gemini/AI Overviews grounding specifically. CCBot (Common Crawl) is
# checked too but not flagged as a problem if blocked - many sites block it
# on purpose since it's training data, not a live-answer crawler, and
# blocking it doesn't affect whether Silah gets cited in an answer.
# Source for which of these matter and why: Google's own AI optimization
# guide plus the crawler-purpose table this was cross-checked against
# (Aug 2026 SEO review) - Google-Agent/ChatGPT-User/Google-NotebookLM are
# deliberately left out of this list since those are user-triggered
# fetchers that ignore robots.txt by design, so checking them here would
# always show "allowed" regardless of what the file says and just add
# noise.
AI_CRAWLERS_TO_CHECK = [
    {"agent": "GPTBot", "owner": "OpenAI", "purpose": "ChatGPT web search", "flagIfBlocked": True},
    {"agent": "OAI-SearchBot", "owner": "OpenAI", "purpose": "OpenAI search features", "flagIfBlocked": True},
    {"agent": "ClaudeBot", "owner": "Anthropic", "purpose": "Claude web features", "flagIfBlocked": True},
    {"agent": "PerplexityBot", "owner": "Perplexity", "purpose": "Perplexity AI search", "flagIfBlocked": True},
    {"agent": "Google-Extended", "owner": "Google", "purpose": "Gemini / AI Overviews grounding", "flagIfBlocked": True},
    {"agent": "anthropic-ai", "owner": "Anthropic", "purpose": "Claude training", "flagIfBlocked": False},
    {"agent": "CCBot", "owner": "Common Crawl", "purpose": "Training-data crawl (often blocked on purpose)", "flagIfBlocked": False},
]


def check_ai_search_readiness():
    """Checks whether the site's actual robots.txt allows the AI crawlers
    that power ChatGPT/Claude/Perplexity/Google-AI-Overviews answers, plus
    whether /llms.txt exists. Uses urllib.robotparser (stdlib) rather than
    hand-rolled parsing, since robots.txt group-matching (which User-agent
    block applies, wildcard fallback, etc.) has enough edge cases that a
    battle-tested parser is worth it over a regex.

    llms.txt is checked for presence only, not scored as pass/fail - as of
    Google's 2026-06-29 AI optimization guide update, Google Search
    (including its AI features) explicitly ignores llms.txt entirely, so
    treating its absence as a problem would be actively misleading. It's
    reported here only because it may help non-Google AI crawlers, which
    is a real but smaller benefit than the robots.txt access itself.

    Returns None (not a dict of failures) if robots.txt itself couldn't be
    read at all, so the caller can tell "checked, and X is blocked" apart
    from "couldn't check this run" - same distinction made everywhere else
    in this file for a failed fetch."""
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(f"{ORIGIN}/robots.txt")
    try:
        rp.read()
    except Exception as e:
        print(f"  WARNING: could not read robots.txt: {e}", file=sys.stderr)
        return None

    crawlers = []
    for c in AI_CRAWLERS_TO_CHECK:
        allowed = rp.can_fetch(c["agent"], ORIGIN + "/")
        crawlers.append({**c, "allowed": allowed})

    llms_txt_present = fetch_html(f"{ORIGIN}/llms.txt") is not None

    return {"crawlers": crawlers, "llmsTxtPresent": llms_txt_present}


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
            print(f"WARNING: CrUX request failed (HTTP {e.code}) — {e}. "
                  "Skipping real-user chart update this run; PSI/Lighthouse "
                  "scores will still refresh below.", file=sys.stderr)
    except Exception as e:
        # Catch-all is deliberate: this module's whole design promise (see
        # docstring) is that a CrUX hiccup of ANY kind never fails the run.
        # Before this, only HTTPError-404 was treated as "skip gracefully" —
        # a 400 (e.g. both TTFB metric names rejected -> RuntimeError from
        # fetch_crux_history's for/else), a 403/429, or a malformed response
        # (KeyError) all fell through and crashed the whole Action red.
        print(f"WARNING: CrUX history fetch failed unexpectedly ({type(e).__name__}: {e}). "
              "Skipping real-user chart update this run; PSI/Lighthouse "
              "scores will still refresh below.", file=sys.stderr)

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

    # PSI/Lighthouse category scores (Performance, SEO, Accessibility, Best
    # Practices) now run every invocation, NOT gated on whether CrUX itself
    # advanced to a new month. These are lab scores from PSI's own crawl —
    # there's no real coupling to CrUX's real-user publish cadence, same
    # reasoning already applied to the page-health scan (see note above).
    # Previously this whole fetch sat inside the homepage_updated branch
    # below: since reportMonth had already been sitting at the same value
    # CrUX kept reporting, homepage_updated was False on every run so far
    # this month, so seoScore/bestPracticesScore/a11yScore* never got set
    # even once, despite the frontend cards for them already being live and
    # waiting on real data. Moving the fetch out fixes that; the CrUX-tied
    # fields below (mobilePerfNow, reportMonth, monthLabels, etc.) still
    # only update when homepage_updated is actually True.
    print("Fetching PageSpeed Insights scores ...")
    mobile_psi = fetch_psi_score("mobile")
    desktop_psi = fetch_psi_score("desktop")
    mobile_score = mobile_psi["performance"]
    desktop_score = desktop_psi["performance"]
    print("PSI mobile:", mobile_score, "| desktop:", desktop_score)

    # Real SEO/Best Practices/Accessibility scores from the PSI runs above --
    # previously these three were hand-typed once into index.html
    # (100 / 100 / "93-100") and never touched again by any automated
    # process. Mobile score used for the single-number cards (SEO and Best
    # Practices don't meaningfully differ by device in Lighthouse);
    # Accessibility kept as a mobile-desktop range since the existing
    # card's own label already implied that was the intent.
    data["seoScore"] = mobile_psi["seo"]
    data["bestPracticesScore"] = mobile_psi["bestPractices"]
    data["a11yScoreMobile"] = mobile_psi["accessibility"]
    data["a11yScoreDesktop"] = desktop_psi["accessibility"]
    data["lighthouseScoresCheckedMonth"] = new_month

    if not homepage_updated:
        print("CrUX/homepage data already at", new_month, "— skipping that part, still running page-health scan below.")
    else:
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
    # NOT the same thing as data["seoScore"] above, which is Lighthouse's
    # own homepage-only SEO category audit (viewport tag, valid hreflang,
    # descriptive link text, etc.). This one is a composite built from the
    # per-page pageHealth data itself -- schema, alt-text, meta description,
    # performance, and CSS/JS bloat -- averaged across every scanned page
    # (not just the homepage), which is why it needed its own field name.
    data["pageHealthScore"] = compute_site_seo_score(data["pageHealth"])
    data["pageHealthScoreCheckedMonth"] = new_month
    scored_count = sum(1 for e in data["pageHealth"] if not e["excludedFromScore"])
    print(f"  Page Health score: {data['pageHealthScore']} (averaged over {scored_count} pages, "
          f"{len(data['pageHealth']) - scored_count} excluded as non-content pages)")

    print("Checking AI crawler access (robots.txt) and llms.txt ...")
    data["aiSearchReadiness"] = check_ai_search_readiness()
    data["aiSearchReadinessCheckedMonth"] = new_month
    if data["aiSearchReadiness"]:
        blocked = [c["agent"] for c in data["aiSearchReadiness"]["crawlers"] if c["flagIfBlocked"] and not c["allowed"]]
        print(f"  AI crawlers blocked: {blocked or 'none'} | llms.txt present: {data['aiSearchReadiness']['llmsTxtPresent']}")

    print("Checking Google Search Console for real keyword rankings ...")
    gsc_token = get_gsc_access_token()
    if gsc_token and data.get("keywords"):
        update_keywords_with_gsc(data["keywords"], gsc_token)
        data["keywordsSource"] = "gsc"
        data["keywordsCheckedMonth"] = new_month
        print(f"  Updated {len(data['keywords'])} tracked keywords from real Search Console data.")
    else:
        # Not an error - just means the keywords array keeps whatever it
        # already had (manually entered, or from the last successful GSC
        # run). keywordsSource stays whatever it already was, so a report
        # that's never had GSC connected still correctly says "manual"
        # instead of silently claiming a source it doesn't have.
        data.setdefault("keywordsSource", "manual")
        print("  Skipped - keywords unchanged this run (see warning above if this is unexpected).")

    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print("data.json updated —", "homepage+" if homepage_updated else "", "page-health refreshed for", new_month)
    return 0


if __name__ == "__main__":
    sys.exit(main())
