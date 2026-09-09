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
                            the live HTML directly and checks Schema.org structured data,
                            image alt text, and on-page basics (canonical, robots/noindex,
                            title, meta description, H1s, internal links), and pulls
                            unused-CSS / unused-JS byte estimates plus failing SEO-category
                            audits from the same PSI call already being made for that
                            page's performance score.

Writes the results into data.json (which the report reads at load time).

Sept 2026 additions (shared IT/Marketing documentation goal):
  - Every finding is now tagged with an OWNER (it / marketing / shared) so each
    team can read its own row without someone translating the report verbally.
  - pageHealthHistory keeps a compact per-month snapshot (rolling 12 months) so
    month-over-month movement is visible instead of being overwritten each run.
  - On-page checks added on HTML this script already fetches — no new requests:
    canonical tag, noindex, title text/length, duplicate titles across pages,
    H1 count, meta description length, and internal-link graph (orphan pages).
  - Sitemap truncation is now surfaced in data.json, not just stderr.

CrUX real-user data is best-effort: lower-traffic origins/pages often don't
have enough anonymized Chrome samples yet for Google to publish a record (a
documented 404/NOT_FOUND response, not an auth or config problem). When that
happens this script skips the real-user chart update for this run but still
refreshes the PSI/Lighthouse scores, so the report never goes stale just
because CrUX has nothing yet.

Env:
  PSI_API_KEY  (required) — Google API key with "Chrome UX Report API" and
                "PageSpeed Insights API" enabled.
  GSC_SERVICE_ACCOUNT_JSON (optional) — service account for real keyword data.

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

from index_cleanup import build_index_cleanup

# ---------------------------------------------------------------------------
# Force IPv4 for every outbound connection this script makes.
# ---------------------------------------------------------------------------
# Sept 9 2026: run #31 failed every single direct fetch to the site with
# "[Errno 101] Network is unreachable" — sitemap_index.xml, robots.txt, all
# five fallback pages, and the WordPress inventory endpoint. Nothing was
# reachable, yet the run still went green: the sitemap fetch fell back to the
# fixed 5-page list, so pageHealth silently dropped from 63 pages to 5 and
# overwrote a good scan with a near-empty one.
#
# Errno 101 is specifically "no route to this address family", not a refusal,
# a timeout, or a firewall block — a WAF returns 403, a WAF under load returns
# 500 or times out. It means the socket layer had nowhere to send the packet
# at all. The site now publishes AAAA (IPv6) records, Python's getaddrinfo
# returns the IPv6 address first, and GitHub-hosted runners have no IPv6
# egress. So every connection died before a single byte left the runner.
#
# Google's APIs were unaffected in the same run (CrUX, PSI and Search Console
# all succeeded), which is the tell: PSI reaches the site from Google's own
# infrastructure, not from here. Only OUR direct fetches were broken.
#
# Restricting getaddrinfo to AF_INET makes every urllib call in this process
# — including index_cleanup's — resolve to IPv4 only. Deliberately global
# rather than per-request: urllib gives no clean per-connection hook, and
# there is no host this script talks to that requires IPv6.
import socket as _socket

_ORIGINAL_GETADDRINFO = _socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _ORIGINAL_GETADDRINFO(host, port, _socket.AF_INET, type, proto, flags)


_socket.getaddrinfo = _ipv4_only_getaddrinfo

ORIGIN = "https://www.silah.com.sa"
DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data.json")

# Safety cap on how many pages get the full scan (HTML fetch + a PSI/Lighthouse
# run each) in a single execution. PSI calls typically take 5-15s apiece, so at
# ~30 pages this run stays in the few-minutes range instead of risking a very
# long or rate-limited Action run.
#
# Sept 2026: raised 30 -> 80. At 30 the scan could silently cover only part of
# the site while the report still read as complete — the warning about it only
# ever went to stderr, where nobody looks. Two changes: the cap is high enough
# to cover the whole sitemap with headroom (the Sept 6 run found 63 pages and
# scanned 60, which also suppressed orphan detection — see analyze_cross_page
# for why a partial scan can't tell a real orphan from an unscanned linker),
# AND the truncation state is now written into data.json (scanCoverage) so the
# report itself can say "scanned 60 of 63" instead of quietly implying full
# coverage.
MAX_AUTO_PAGES = 80

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

# PSI retry policy, added after the Sept 6 2026 run returned HTTP 500 from
# Google's PageSpeed API for ~20 of 60 pages, leaving their mobileScore /
# unusedCss / unusedJs / seoIssues columns blank in the report. Two things
# point at rate-limiting rather than a genuine server fault or a problem with
# the site itself: the same pages' direct HTML fetches succeeded seconds
# earlier in the same run, and the failures clustered in the first block of
# pages, right where a burst of back-to-back PSI requests would hit hardest.
# Google returns a generic 500 here rather than a clean 429, so there's no
# way to distinguish the two from the response — which is exactly why the
# earlier fetch_psi_score() retry deliberately covered only timeouts and not
# error responses.
#
# Retrying a 500 is safe in a way retrying a 4xx is not: PSI is a read-only
# analysis endpoint, so a repeat request can't double-apply anything, and a
# transient 5xx is by definition the class of error where the same request
# may succeed later. Retries are capped and backoff grows between attempts,
# so a genuinely broken page costs three tries and moves on rather than
# stalling the run.
PSI_RETRY_STATUSES = {429, 500, 502, 503, 504}
# Sept 6 2026, second revision. The first version of this (2 retries, 8s then
# 20s, plus a 1s gap between every call) made things worse, not better: run #29
# was cancelled at 52 minutes with the failure rate CLIMBING as it went — 6
# retry lines at 18 min, 10 at 42, 16 at 51. That shape is the signature of
# throttling, not of random transient errors, and this site sits behind a
# Sucuri WAF that sees 63 full Lighthouse page loads from Google's crawlers as
# exactly the sustained burst it exists to slow down. Every retry was another
# request into a WAF that was already throttling.
#
# So: one retry, not two. Attempt 3 almost never rescued a page (whatsapp,
# auto-calls, customer-service-bot, video-library and technology-recruitment-
# solutions all burned it and still failed) while costing 20s each time.
PSI_MAX_RETRIES = 1
PSI_RETRY_BACKOFF_SECONDS = [8]  # waited before the single retry

# Set back to 0. This was added to pace requests at Google's API, on the theory
# that the 500s were Google rate-limiting us. Run #29 disproved that: the
# timeouts are the site's own WAF throttling Google's crawler, so a delay on
# our side buys nothing and just spends a minute per run.
PSI_CALL_DELAY_SECONDS = 0

# THE ACTUAL FIX for run length. PSI is the entire cost of this script: each
# call runs a full simulated-connection Lighthouse audit against a live page,
# takes 5-15s when it works, and is the only part that fails. Everything else
# — schema, alt text, canonical, titles, H1s, the internal-link graph — comes
# from one cheap HTML fetch that essentially never fails.
#
# So the two are decoupled. The HTML pass still covers EVERY page EVERY run,
# because that's where most findings come from and it's nearly free. PSI runs
# over a rotating subset, oldest-measured first, so every page still gets
# performance data — just not every single month. Pages not measured this run
# keep their previous numbers (see carry_forward_psi) rather than going blank,
# with psiCheckedMonth recording when each was last really measured.
#
# At 15 per run every page is re-measured roughly quarterly, which matches how
# fast these numbers actually move, and brings the run back under ~15 minutes.
PSI_PAGES_PER_RUN = 15

# How many monthly snapshots of pageHealth to keep in data.json. 12 gives a
# full year of month-over-month comparison; the snapshot is deliberately
# compact (see snapshot_page_health()) so a year of them stays small rather
# than turning data.json into an archive the frontend has to download.
PAGE_HEALTH_HISTORY_MONTHS = 12

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

# ---------------------------------------------------------------------------
# Ownership model (Sept 2026)
# ---------------------------------------------------------------------------
# The single change that makes this report usable as shared IT/Marketing
# documentation rather than one undifferentiated pile of findings. Every issue
# emitted below carries an owner so each team can filter to its own work:
#
#   "it"        — server, template, markup, and build-output concerns. Fixed in
#                 WordPress theme/plugin/hosting layers.
#   "marketing" — copy, content and keyword concerns. Fixed by writing words.
#   "shared"    — needs both: content decides the wording, IT enters/implements
#                 it. Alt text is the canonical example (Marketing confirms
#                 partner/certification logo naming, IT fills the fields).
#
# Deliberately three values, not a free-text field: anything more granular
# stops being filterable and starts being prose.
OWNER_IT = "it"
OWNER_MARKETING = "marketing"
OWNER_SHARED = "shared"

# Owner for each failing Lighthouse SEO-category audit id. Anything not listed
# defaults to OWNER_IT — an unrecognised technical audit is far more likely to
# be a markup/template issue than a copywriting one, and mis-routing something
# to Marketing that they can't action is worse than the reverse.
SEO_AUDIT_OWNERS = {
    "meta-description": OWNER_MARKETING,
    "document-title": OWNER_MARKETING,
    "link-text": OWNER_MARKETING,
    "hreflang": OWNER_IT,
    "canonical": OWNER_IT,
    "is-crawlable": OWNER_IT,
    "http-status-code": OWNER_IT,
    "crawlable-anchors": OWNER_IT,
    "robots-txt": OWNER_IT,
    "viewport": OWNER_IT,
}

# Thresholds for the on-page checks below. These are conventional SEO ranges,
# not Google-guaranteed limits — Google truncates by pixel width, not character
# count, so treat these as "worth a look", which is why they're emitted at
# severity "info"/"warn" rather than as hard failures.
TITLE_MIN_CHARS = 25
TITLE_MAX_CHARS = 65
META_DESC_MIN_CHARS = 70
META_DESC_MAX_CHARS = 165


def slug_from_url(url):
    """Path-based slug, not domain-based — url.rsplit("/") alone breaks on
    the homepage URL itself (the // after https: is also a "/", so naive
    splitting returns the domain name instead of a real slug). Caught this
    with a homepage-URL test case; urlparse's .path avoids the whole class
    of bug by only ever looking at the path component."""
    path = urllib.parse.urlparse(url).path.strip("/")
    return path.rsplit("/", 1)[-1] if path else "home"


def normalize_url(url):
    """Canonical form used for comparing URLs to each other (internal-link
    graph, canonical-tag self-reference check). Drops the fragment and query,
    and forces exactly one trailing slash, so /page, /page/, /page?x=1 and
    /page#top all compare equal. Deliberately does NOT touch percent-encoding:
    this site's Arabic slugs are stored encoded and re-encoding them
    inconsistently is exactly the class of silent mismatch that already bit
    the KNOWN_PAGE_NAMES lookup (see the substring workaround above)."""
    if not url:
        return ""
    url = url.split("#", 1)[0].split("?", 1)[0].strip()
    if not url:
        return ""
    return url.rstrip("/") + "/"


def discover_pages_from_sitemap():
    """Auto-discovers every WordPress 'Page' URL from this site's Yoast-
    generated sitemap instead of relying on a hand-maintained list. Standard
    Yoast structure (confirmed installed on this site — Yoast SEO v27.9):
    sitemap_index.xml lists one sub-sitemap per post type, one of which is
    page-sitemap.xml (or page-sitemap1.xml, page-sitemap2.xml, ... if there
    are enough pages that Yoast splits them — it paginates at 200 URLs per
    file, hence matching by substring below rather than an exact filename).

    Returns (urls, total_found) where urls is capped at MAX_AUTO_PAGES and
    total_found is the real uncapped count, or (None, 0) on any failure — not
    an empty list — so the caller can tell "genuinely zero pages" apart from
    "something went wrong" and fall back to PAGE_LIST_FALLBACK accordingly.
    total_found is returned rather than only logged so the report can show
    real coverage instead of implying it scanned everything."""
    index_xml = fetch_html(f"{ORIGIN}/sitemap_index.xml")
    if not index_xml:
        print("WARNING: could not fetch sitemap_index.xml", file=sys.stderr)
        return None, 0
    try:
        root = ET.fromstring(index_xml)
    except ET.ParseError as e:
        print(f"WARNING: sitemap_index.xml did not parse as XML: {e}", file=sys.stderr)
        return None, 0

    sub_sitemaps = [loc.text.strip() for loc in root.findall(".//sm:loc", SITEMAP_NS) if loc.text]
    page_sitemaps = [s for s in sub_sitemaps if "page-sitemap" in s]
    if not page_sitemaps:
        print("WARNING: no page-sitemap*.xml listed in sitemap_index.xml — "
              "site's sitemap structure may not match the expected Yoast layout.",
              file=sys.stderr)
        return None, 0

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
        return None, 0
    total = len(urls)
    if total > MAX_AUTO_PAGES:
        print(f"WARNING: sitemap has {total} pages — scanning the first "
              f"{MAX_AUTO_PAGES} this run (raise MAX_AUTO_PAGES for more).",
              file=sys.stderr)
    return urls[:MAX_AUTO_PAGES], total


def build_page_list():
    """Combines automatic sitemap discovery with the known bilingual names
    above. Falls back to PAGE_LIST_FALLBACK if discovery fails for any reason.

    Returns (pages, coverage) — coverage records how many pages the sitemap
    actually lists vs. how many got scanned, so the report can state its own
    completeness rather than leaving a truncated scan looking like a full one."""
    discovered, total_found = discover_pages_from_sitemap()
    if not discovered:
        print("Sitemap discovery unavailable this run — using the fixed 5-page fallback list.")
        return PAGE_LIST_FALLBACK, {
            "source": "fallback",
            "totalPages": len(PAGE_LIST_FALLBACK),
            "scannedPages": len(PAGE_LIST_FALLBACK),
            "truncated": False,
            "maxAutoPages": MAX_AUTO_PAGES,
        }

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
    coverage = {
        "source": "sitemap",
        "totalPages": total_found,
        "scannedPages": len(pages),
        "truncated": total_found > len(pages),
        "maxAutoPages": MAX_AUTO_PAGES,
    }
    return pages, coverage


def extract_title_raw(html):
    """The page's <title> text, whitespace-collapsed but otherwise untouched —
    including the "| Site Name" suffix. Length checks and duplicate-title
    detection both need the real string Google sees, not the trimmed display
    name extract_title() returns."""
    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    return re.sub(r'\s+', ' ', m.group(1)).strip() or None


def extract_title(html):
    """Pulls the page's own <title> text as a fallback display name for
    pages without a curated entry in KNOWN_PAGE_NAMES. WordPress/Yoast titles
    are usually "Page Name | Site Name" — trims that suffix so the report
    shows just the page-specific part, not the same site name on every row."""
    title = extract_title_raw(html)
    if not title:
        return None
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


def http_json(url, payload=None, extra_headers=None, timeout=120):
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
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


def fetch_psi_score(strategy, retry=True):
    """Homepage PSI run for the given strategy. Returns all four Lighthouse
    category scores from one call -- Performance, SEO, Accessibility, and
    Best Practices are all computed together by Lighthouse regardless of
    which categories you ask for in scoring terms; requesting them
    explicitly just makes the API return them. Previously this only asked
    for (and returned) performance, so the SEO/Best Practices/Accessibility
    score cards on the site were hand-typed once and never updated by any
    automated process since -- correct fix is to actually read the numbers
    already present in the response we were already making, not add a
    second request.

    Uses a longer timeout than the default (200s, not 120s) and retries
    once on a timeout before giving up -- added after an Aug 19 2026 run
    crashed entirely because a single slow mobile Lighthouse test (these
    run a full simulated-connection audit and are the slowest of the API
    calls this script makes) exceeded the old 120s default. A retry after
    a real timeout is a legitimate thing to attempt here, unlike blindly
    retrying a 4xx/5xx error response: a timeout means no response came
    back at all, not that the server rejected something retrying would
    repeat identically."""
    url = ("https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
           f"?url={urllib.parse.quote(ORIGIN + '/', safe='')}"
           f"&strategy={strategy}&category=performance&category=seo"
           f"&category=accessibility&category=best-practices&key={API_KEY}")
    try:
        resp = http_json(url, timeout=200)
    except (TimeoutError, urllib.error.URLError) as e:
        if retry:
            print(f"  WARNING: PSI {strategy} request timed out, retrying once: {e}", file=sys.stderr)
            return fetch_psi_score(strategy, retry=False)
        raise
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


# Lighthouse audits deliberately not reported, because a check in this file
# already covers the same ground more precisely:
#   image-alt        — check_alt_text() names the actual image files; Lighthouse
#                      only says pass/fail for the page.
#   meta-description — check_onpage() reads the tag directly and also measures
#                      its length. Sept 6 2026: leaving both in produced two
#                      separate findings for the same problem (14 pages under
#                      the Arabic label, 10 under Lighthouse's English one, on
#                      overlapping page sets), which inflated the totals and
#                      made Marketing's queue look longer than it is. Note the
#                      counts differ because the checks genuinely differ —
#                      Lighthouse only fires when the tag is absent, while
#                      check_onpage() also catches a tag that's present but
#                      empty. Keeping the more thorough one.
SKIP_SEO_AUDITS = {"image-alt", "meta-description"}


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
    we just weren't reading it before.

    Sept 2026: each returned SEO issue now carries an `owner` (see
    SEO_AUDIT_OWNERS) so a failing meta-description audit lands on Marketing's
    row and a failing hreflang audit lands on IT's, without either team having
    to interpret Lighthouse audit ids.

    Also retries on the retryable statuses in PSI_RETRY_STATUSES with growing
    backoff — see that constant for why a 500 from this endpoint is worth
    retrying when a 4xx isn't. Raises the last error if every attempt fails,
    so the caller still marks the page's PSI fields unavailable rather than
    inventing values."""
    url = ("https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
           f"?url={urllib.parse.quote(page_url, safe='')}"
           f"&strategy={strategy}&category=performance&category=seo&key={API_KEY}")

    resp = None
    for attempt in range(PSI_MAX_RETRIES + 1):
        try:
            resp = http_json(url)
            break
        except urllib.error.HTTPError as e:
            if e.code not in PSI_RETRY_STATUSES or attempt == PSI_MAX_RETRIES:
                raise
            wait = PSI_RETRY_BACKOFF_SECONDS[min(attempt, len(PSI_RETRY_BACKOFF_SECONDS) - 1)]
            print(f"    PSI HTTP {e.code} for {page_url} — retrying in {wait}s "
                  f"(attempt {attempt + 2} of {PSI_MAX_RETRIES + 1})", file=sys.stderr)
            time.sleep(wait)
        except (TimeoutError, urllib.error.URLError) as e:
            # A timeout means no response came back at all, so the same request
            # may well succeed — same reasoning as fetch_psi_score's retry.
            if attempt == PSI_MAX_RETRIES:
                raise
            wait = PSI_RETRY_BACKOFF_SECONDS[min(attempt, len(PSI_RETRY_BACKOFF_SECONDS) - 1)]
            print(f"    PSI network error for {page_url} ({e}) — retrying in {wait}s "
                  f"(attempt {attempt + 2} of {PSI_MAX_RETRIES + 1})", file=sys.stderr)
            time.sleep(wait)

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
        seo_issues.append({
            "id": aid,
            "title": a.get("title", aid),
            "owner": SEO_AUDIT_OWNERS.get(aid, OWNER_IT),
        })

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
            issues.append({
                "file": resolved.rsplit("/", 1)[-1],
                "url": resolved,
                # Distinguishes "no alt attribute at all" from "alt is a
                # generic placeholder like logo/image" — same fix either way,
                # but the second kind is invisible in a browser devtools check
                # (which only finds empty ones), so naming it here stops the
                # two counts looking like a discrepancy in the report.
                "reason": "missing" if alt_val is None else "placeholder",
            })
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


def check_onpage(html, page_url):
    """On-page fundamentals read off HTML this script already fetched — no
    extra requests, no new API quota. Everything here was previously invisible
    to the report despite the markup being right there in the same string the
    schema and alt-text checks were already scanning.

    Collected per page:
      canonical      — the rel=canonical href, and whether it points at this
                       page itself. A canonical pointing somewhere else means
                       this page is asking Google not to rank it, which is
                       occasionally intentional and usually not.
      noindex        — meta robots noindex. Not a fault on its own: the
                       cart/checkout/account pages SHOULD be noindexed (see
                       SCORE_EXCLUDED_SLUGS), so this is reported as a fact and
                       only flagged as an issue on pages that aren't excluded.
      title          — raw <title> and its length.
      metaDescription— the description text and its length (presence itself is
                       also caught by Lighthouse's meta-description audit; the
                       length check is the part Lighthouse doesn't give us).
      h1Count        — number of <h1> elements. Zero or several both indicate a
                       heading structure worth a look.
      internalLinks  — same-origin link targets, normalized. Aggregated across
                       all pages in run_page_health_scan() to find orphans.
    """
    canonical = None
    m = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]*>', html, re.IGNORECASE)
    if m:
        h = re.search(r'href=["\']([^"\']+)["\']', m.group(0), re.IGNORECASE)
        canonical = h.group(1).strip() if h else None

    robots_content = ""
    m = re.search(r'<meta[^>]+name=["\']robots["\'][^>]*>', html, re.IGNORECASE)
    if m:
        c = re.search(r'content=["\']([^"\']*)["\']', m.group(0), re.IGNORECASE)
        robots_content = (c.group(1) if c else "").lower()

    meta_desc = None
    m = re.search(r'<meta[^>]+name=["\']description["\'][^>]*>', html, re.IGNORECASE)
    if m:
        c = re.search(r'content=["\']([^"\']*)["\']', m.group(0), re.IGNORECASE)
        meta_desc = ((c.group(1) if c else "") or "").strip() or None

    title = extract_title_raw(html)
    h1s = re.findall(r'<h1\b[^>]*>(.*?)</h1>', html, re.DOTALL | re.IGNORECASE)

    hrefs = re.findall(r'<a\b[^>]*href=["\']([^"\']+)["\']', html, re.IGNORECASE)
    internal = set()
    for h in hrefs:
        h = h.strip()
        if not h or h.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        resolved = urllib.parse.urljoin(page_url, h)
        if resolved.startswith(ORIGIN):
            internal.add(normalize_url(resolved))
    internal.discard(normalize_url(page_url))  # self-links aren't inbound links

    return {
        "canonical": canonical,
        "canonicalIsSelf": normalize_url(canonical) == normalize_url(page_url) if canonical else None,
        "noindex": "noindex" in robots_content,
        "robotsMeta": robots_content or None,
        "title": title,
        "titleLength": len(title) if title else 0,
        "metaDescription": meta_desc,
        "metaDescriptionLength": len(meta_desc) if meta_desc else 0,
        "h1Count": len(h1s),
        "internalLinks": sorted(internal),
    }


def build_issue_list(entry):
    """Flattens everything known about one page into a single owner-tagged
    issue list. This is what makes the report readable as shared documentation:
    each team filters to its own owner and sees exactly its outstanding work,
    with no interpretation step in between.

    Severity is "high" / "medium" / "low" — high means it affects whether the
    page can rank at all, medium means it measurably weakens it, low is
    hygiene. Deliberately conservative: things that are merely conventional
    (title length, H1 count) are never "high", because treating a style
    convention as a blocker is how a report trains people to ignore it.

    Checks that didn't run this time (null, same graceful-degradation rule as
    everywhere else in this file) produce no issues rather than false ones —
    "we didn't measure it" must never render as "it's fine"."""
    issues = []
    excluded = entry.get("excludedFromScore", False)

    def add(code, owner, severity, ar, en, detail=None):
        item = {"code": code, "owner": owner, "severity": severity,
                "labelAr": ar, "labelEn": en}
        if detail is not None:
            item["detail"] = detail
        issues.append(item)

    # Funnel/test/expired pages (SCORE_EXCLUDED_SLUGS) are already kept out of
    # the aggregate score for a reason: nobody should be writing a meta
    # description for the shopping cart. That reasoning applies just as much to
    # the issue list — leaving them in would put "cart page has no meta
    # description" onto Marketing's queue every month, which is exactly the
    # noise the exclusion list exists to prevent, and would make the owner
    # counts overstate real outstanding work. These pages still get scanned and
    # still appear in the table with all their raw data; they just contribute
    # one actionable finding at most (should they be noindexed), and nothing
    # content-related.
    if excluded:
        op_ex = entry.get("onPage")
        if op_ex is not None and not op_ex["noindex"]:
            add("should_be_noindex", OWNER_IT, "low",
                "صفحة غير تسويقية يُفضّل منع فهرستها",
                "Non-content funnel page that should probably be noindexed")
        return issues

    schema = entry.get("schema")
    if schema is not None and not schema["hasExpectedType"]:
        if schema["typesFound"]:
            add("schema_wrong_type", OWNER_IT, "medium",
                "بيانات منظمة موجودة لكن ليست من النوع المتوقع",
                "Structured data present but not the expected type",
                {"typesFound": schema["typesFound"]})
        else:
            add("schema_missing", OWNER_IT, "high",
                "لا توجد بيانات منظمة (Schema) على الصفحة",
                "No Schema.org structured data on the page")

    alt = entry.get("altText")
    if alt is not None and alt["issueCount"] > 0:
        add("alt_text_missing", OWNER_SHARED, "medium",
            f"{alt['issueCount']} صورة بدون نص بديل",
            f"{alt['issueCount']} image(s) missing alt text",
            {"count": alt["issueCount"], "totalImages": alt["totalImages"]})

    op = entry.get("onPage")
    if op is not None:
        if op["noindex"]:
            # Reachable only for non-excluded pages — the excluded case returns
            # early above, so a noindex here is genuinely a content page hidden
            # from Google, which is the highest-severity thing this scan finds.
            add("noindex_unexpected", OWNER_IT, "high",
                "الصفحة تمنع الفهرسة (noindex) رغم أنها صفحة محتوى",
                "Page is set to noindex despite being a content page")
        if not op["canonical"]:
            add("canonical_missing", OWNER_IT, "medium",
                "لا يوجد وسم canonical",
                "No rel=canonical tag")
        elif op["canonicalIsSelf"] is False:
            add("canonical_points_elsewhere", OWNER_IT, "high",
                "وسم canonical يشير إلى صفحة أخرى",
                "Canonical tag points to a different URL",
                {"canonical": op["canonical"]})
        if not op["title"]:
            add("title_missing", OWNER_MARKETING, "high",
                "لا يوجد عنوان للصفحة (title)",
                "Page has no <title>")
        elif op["titleLength"] > TITLE_MAX_CHARS:
            add("title_too_long", OWNER_MARKETING, "low",
                f"عنوان الصفحة طويل ({op['titleLength']} حرف)",
                f"Title is long ({op['titleLength']} chars)",
                {"length": op["titleLength"], "max": TITLE_MAX_CHARS})
        elif op["titleLength"] < TITLE_MIN_CHARS:
            add("title_too_short", OWNER_MARKETING, "low",
                f"عنوان الصفحة قصير ({op['titleLength']} حرف)",
                f"Title is short ({op['titleLength']} chars)",
                {"length": op["titleLength"], "min": TITLE_MIN_CHARS})
        if not op["metaDescription"]:
            add("meta_description_missing", OWNER_MARKETING, "medium",
                "لا يوجد وصف تعريفي (meta description)",
                "No meta description")
        elif op["metaDescriptionLength"] > META_DESC_MAX_CHARS:
            add("meta_description_too_long", OWNER_MARKETING, "low",
                f"الوصف التعريفي طويل ({op['metaDescriptionLength']} حرف)",
                f"Meta description is long ({op['metaDescriptionLength']} chars)")
        elif op["metaDescriptionLength"] < META_DESC_MIN_CHARS:
            add("meta_description_too_short", OWNER_MARKETING, "low",
                f"الوصف التعريفي قصير ({op['metaDescriptionLength']} حرف)",
                f"Meta description is short ({op['metaDescriptionLength']} chars)")
        if op["h1Count"] == 0:
            add("h1_missing", OWNER_MARKETING, "medium",
                "لا يوجد عنوان رئيسي H1",
                "No H1 heading on the page")
        elif op["h1Count"] > 1:
            add("h1_multiple", OWNER_MARKETING, "low",
                f"يوجد {op['h1Count']} عناوين H1",
                f"{op['h1Count']} H1 headings on the page")

    if entry.get("inboundInternalLinks") == 0 and not excluded:
        add("orphan_page", OWNER_MARKETING, "high",
            "صفحة يتيمة — لا ترتبط بها أي صفحة أخرى",
            "Orphan page — no other scanned page links to it")

    if entry.get("mobileScore") is not None and entry["mobileScore"] < 50:
        add("mobile_performance_poor", OWNER_IT, "high",
            f"أداء الجوال ضعيف ({entry['mobileScore']}/100)",
            f"Poor mobile performance ({entry['mobileScore']}/100)")
    elif entry.get("mobileScore") is not None and entry["mobileScore"] < 90:
        add("mobile_performance_below_target", OWNER_IT, "medium",
            f"أداء الجوال دون الهدف ({entry['mobileScore']}/100)",
            f"Mobile performance below target ({entry['mobileScore']}/100)")

    if entry.get("unusedCssKb") is not None and entry["unusedCssKb"] > 60:
        add("unused_css_high", OWNER_IT, "medium",
            f"CSS غير مستخدم ({entry['unusedCssKb']}KB)",
            f"High unused CSS ({entry['unusedCssKb']}KB)")
    if entry.get("unusedJsKb") is not None and entry["unusedJsKb"] > 150:
        add("unused_js_high", OWNER_IT, "medium",
            f"JavaScript غير مستخدم ({entry['unusedJsKb']}KB)",
            f"High unused JavaScript ({entry['unusedJsKb']}KB)")

    for si in entry.get("seoIssues") or []:
        add(f"lighthouse_{si['id']}", si.get("owner", OWNER_IT), "medium",
            si.get("title", si["id"]), si.get("title", si["id"]))

    if entry.get("duplicateTitleWith"):
        add("duplicate_title", OWNER_MARKETING, "medium",
            "عنوان الصفحة مكرر مع صفحة أخرى",
            "Title is duplicated on another page",
            {"sharedWith": entry["duplicateTitleWith"]})

    return issues


def select_psi_pages(page_list, previous, month):
    """Chooses which pages get a live PSI/Lighthouse run this time.

    Oldest-measured-first, so the rotation is self-balancing: a page never
    measured has no psiCheckedMonth and sorts first, then the page measured
    longest ago, and so on. No stored cursor to drift out of sync with the
    page list, and pages added to the sitemap later automatically jump to the
    front of the queue instead of waiting for a full cycle.

    Ties (several pages last measured the same month) keep sitemap order,
    which is stable between runs, so the rotation advances predictably rather
    than reshuffling."""
    prev_by_id = {p.get("id"): p for p in (previous or [])}

    def last_measured(page):
        prev = prev_by_id.get(page["id"])
        # "" sorts before any real "YYYY-MM", putting never-measured pages first.
        return (prev or {}).get("psiCheckedMonth") or ""

    ordered = sorted(page_list, key=lambda pg: (last_measured(pg), page_list.index(pg)))
    chosen = {pg["id"] for pg in ordered[:PSI_PAGES_PER_RUN]}
    return chosen, prev_by_id


def carry_forward_psi(entry, prev):
    """Copies the last known PSI numbers onto a page that wasn't measured this
    run. Without this, rotating PSI would blank two thirds of the performance
    column every month, which is strictly worse than the old every-page scan.

    psiCheckedMonth travels with the values, so the report can say how old each
    one is instead of implying they're all current. A page with no previous
    measurement at all stays null — that's honest, and it will be first in line
    next run."""
    entry["mobileScore"] = (prev or {}).get("mobileScore")
    entry["unusedCssKb"] = (prev or {}).get("unusedCssKb")
    entry["unusedJsKb"] = (prev or {}).get("unusedJsKb")
    entry["seoIssues"] = (prev or {}).get("seoIssues")
    entry["psiCheckedMonth"] = (prev or {}).get("psiCheckedMonth")
    entry["psiFresh"] = False


def run_page_health_scan(previous=None, month=None):
    """One pass over the auto-discovered page list.

    Two passes of differing cost, deliberately decoupled (see PSI_PAGES_PER_RUN):
      - HTML checks (schema, alt text, on-page, links) run on EVERY page, every
        run. One cheap fetch each.
      - PSI/Lighthouse runs on a rotating subset. Pages not selected keep their
        previous numbers via carry_forward_psi() rather than going blank.

    Any single page failing doesn't stop the others — each result just gets
    marked unavailable for this run.

    Returns (results, coverage). Cross-page analysis (internal-link graph for
    orphan detection, duplicate titles) happens after the per-page loop, since
    both need every page's data before either can be decided."""
    page_list, coverage = build_page_list()
    psi_ids, prev_by_id = select_psi_pages(page_list, previous, month)
    print(f"  Page list: {len(page_list)} page(s) — HTML checks on all, "
          f"live PSI on {len(psi_ids)} this run (rotating oldest-first).")
    results = []
    psi_failures = []
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
            entry["onPage"] = check_onpage(html, page["url"])
        else:
            entry["schema"] = None
            entry["altText"] = None
            entry["onPage"] = None

        if page["id"] not in psi_ids:
            carry_forward_psi(entry, prev_by_id.get(page["id"]))
        else:
            try:
                if PSI_CALL_DELAY_SECONDS and i > 0:
                    time.sleep(PSI_CALL_DELAY_SECONDS)
                psi = fetch_psi_full(page["url"], "mobile")
                entry["mobileScore"] = psi["score"]
                entry["unusedCssKb"] = psi["unusedCssKb"]
                entry["unusedJsKb"] = psi["unusedJsKb"]
                entry["seoIssues"] = psi["seoIssues"]
                entry["psiCheckedMonth"] = month
                entry["psiFresh"] = True
            except Exception as e:
                print(f"  WARNING: PSI failed for {page['id']}: {e}", file=sys.stderr)
                psi_failures.append(page["id"])
                # Falls back to whatever was last known rather than blanking the
                # row: a failed measurement is not evidence the old one is wrong,
                # and this page sorts to the front of next run's rotation anyway
                # because its psiCheckedMonth didn't advance.
                carry_forward_psi(entry, prev_by_id.get(page["id"]))

        results.append(entry)

    analyze_cross_page(results)
    # Surfaced in the return value (and from there into data.json) rather than
    # only logged: a page whose PSI call failed shows blank cells in the
    # report, which is indistinguishable from "measured and fine" unless the
    # report can say how many pages this affected. Same principle as
    # scanCoverage — the report should state the limits of its own data.
    coverage["psiFailures"] = len(psi_failures)
    coverage["psiFailedPages"] = psi_failures
    coverage["psiAttempted"] = len(psi_ids)
    coverage["psiFresh"] = sum(1 for e in results if e.get("psiFresh"))
    coverage["psiRotating"] = len(psi_ids) < len(page_list)
    if psi_failures:
        print(f"  NOTE: PSI returned no data for {len(psi_failures)} page(s) after retries: "
              f"{', '.join(psi_failures[:8])}{' …' if len(psi_failures) > 8 else ''}")
    return results, coverage


def analyze_cross_page(results):
    """Cross-page analysis that can only run once every page has been scanned:

    Internal-link graph -> inbound link counts and orphan detection. An orphan
    is a page in the sitemap that no OTHER scanned page links to. Note the
    honest limitation: this only sees links on pages that were scanned this
    run, so if the sitemap was truncated (see MAX_AUTO_PAGES) a page could look
    orphaned only because the page linking to it wasn't scanned. That's why
    orphan flagging is skipped entirely on a truncated run rather than
    reporting findings we can't stand behind.

    Duplicate titles -> two pages sharing an identical <title> compete with
    each other in search results. This site has had exactly this bug before
    (a duplicate title tag fixed by the WordPress developer in Aug 2026), so
    it's worth catching automatically rather than by eye.

    Mutates entries IN PLACE, same pattern as compute_site_seo_score below."""
    all_links = set()
    scanned_ok = [e for e in results if e.get("onPage")]
    for e in scanned_ok:
        all_links.update(e["onPage"]["internalLinks"])

    for e in results:
        if not e.get("onPage"):
            e["inboundInternalLinks"] = None
            continue
        target = normalize_url(e["url"])
        inbound = sum(1 for other in scanned_ok
                      if other is not e and target in other["onPage"]["internalLinks"])
        e["inboundInternalLinks"] = inbound

    titles = {}
    for e in scanned_ok:
        t = (e["onPage"].get("title") or "").strip()
        if t:
            titles.setdefault(t, []).append(e["id"])
    for e in results:
        e["duplicateTitleWith"] = None
        if not e.get("onPage"):
            continue
        t = (e["onPage"].get("title") or "").strip()
        if t and len(titles.get(t, [])) > 1:
            e["duplicateTitleWith"] = [pid for pid in titles[t] if pid != e["id"]]


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
    Returns None if every component is unavailable.

    Sept 2026 note: the new on-page checks (canonical, title, H1, internal
    links) are deliberately NOT folded into this formula. Changing the weights
    would move every page's score for reasons unrelated to the site actually
    changing, which would make the first month of pageHealthHistory a
    meaningless comparison and undermine the point of keeping history at all.
    Those findings surface in the owner-tagged issue list instead. Revisit
    after there are a few months of history worth comparing against."""
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


def attach_issues(page_health, coverage):
    """Builds each page's owner-tagged issue list and returns a site-wide
    summary grouped by owner. Must run AFTER compute_site_seo_score, since
    build_issue_list() reads excludedFromScore to decide whether noindex on a
    given page is correct or a fault.

    Orphan detection is suppressed on a truncated run — see analyze_cross_page
    for why a partial scan can't distinguish a real orphan from a page whose
    only inbound link lives on a page that wasn't scanned."""
    truncated = coverage.get("truncated", False)
    summary = {OWNER_IT: 0, OWNER_MARKETING: 0, OWNER_SHARED: 0}
    by_severity = {"high": 0, "medium": 0, "low": 0}
    for entry in page_health:
        issues = build_issue_list(entry)
        if truncated:
            issues = [i for i in issues if i["code"] != "orphan_page"]
        entry["issues"] = issues
        for i in issues:
            summary[i["owner"]] = summary.get(i["owner"], 0) + 1
            by_severity[i["severity"]] = by_severity.get(i["severity"], 0) + 1
    return {
        "byOwner": summary,
        "bySeverity": by_severity,
        "totalIssues": sum(summary.values()),
        "orphanDetectionSkipped": truncated,
    }


def snapshot_page_health(page_health):
    """A compact per-page record for the monthly history. Deliberately NOT the
    full entry: keeping every alt-text filename and internal-link list for 12
    months would balloon data.json (which the frontend downloads in full on
    every page load) for no benefit — history exists to answer "is this getting
    better or worse", which needs numbers, not detail. The current month's full
    detail always lives in data["pageHealth"]."""
    return [{
        "id": e["id"],
        "nameAr": e.get("nameAr"),
        "seoScore": e.get("seoScore"),
        "mobileScore": e.get("mobileScore"),
        "altIssueCount": (e["altText"]["issueCount"] if e.get("altText") else None),
        "schemaOk": (e["schema"]["hasExpectedType"] if e.get("schema") else None),
        "issueCount": len(e.get("issues") or []),
    } for e in page_health]


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


def build_note(new_month, keywords_source, coverage):
    """The report's own plain-language summary of what's live vs. still manual.

    This used to be a hardcoded string containing the literal text "as of Aug
    2026", rewritten identically on every run — so the one field whose entire
    job was to stop the report going stale was itself guaranteed to go stale,
    claiming August forever. Now every changing part is derived from this run's
    actual state: the real month, whether keywords genuinely came from GSC this
    time, and real scan coverage."""
    year, month = new_month.split("-")
    month_ar = AR_MONTHS[int(month) - 1]
    kw_line = (f"الكلمات المفتاحية: بيانات فعلية من Google Search Console حتى {month_ar} {year}."
               if keywords_source == "gsc" else
               f"الكلمات المفتاحية: مُدخلة يدويًا — لم يتم تحديثها من Search Console "
               f"في تشغيل {month_ar} {year}.")
    if coverage.get("truncated"):
        cov_line = (f"تم فحص {coverage['scannedPages']} صفحة من أصل {coverage['totalPages']} "
                    f"في خريطة الموقع (الحد الأقصى الحالي {coverage['maxAutoPages']}).")
    elif coverage.get("source") == "fallback":
        cov_line = "تعذّر قراءة خريطة الموقع في هذا التشغيل — تم فحص القائمة الاحتياطية الثابتة فقط."
    else:
        cov_line = f"تم فحص جميع صفحات خريطة الموقع ({coverage['scannedPages']} صفحة)."
    return (f"{kw_line} {cov_line} "
            "كل ملاحظة في التقرير موسومة بالجهة المسؤولة عنها (تقنية / تسويق / مشتركة). "
            "صفوف أداء الصفحات غير الرئيسية لا تزال تحتاج إضافة الروابط يدويًا "
            "(راجع pageRegistry في index.html). كل ما عدا ذلك — سلامة الصفحات، وصول "
            "زواحف الذكاء الاصطناعي، درجات Lighthouse، ومؤشرات CrUX — يُحدَّث تلقائيًا "
            "شهريًا عبر GitHub Action من واجهات Google مباشرة ومن الموقع نفسه.")


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
    try:
        mobile_psi = fetch_psi_score("mobile")
        desktop_psi = fetch_psi_score("desktop")
        mobile_score = mobile_psi["performance"]
        desktop_score = desktop_psi["performance"]
        print("PSI mobile:", mobile_score, "| desktop:", desktop_score)

        # Real SEO/Best Practices/Accessibility scores from the PSI runs
        # above -- previously these three were hand-typed once into
        # index.html (100 / 100 / "93-100") and never touched again by any
        # automated process. Mobile score used for the single-number cards
        # (SEO and Best Practices don't meaningfully differ by device in
        # Lighthouse); Accessibility kept as a mobile-desktop range since
        # the existing card's own label already implied that was the intent.
        data["seoScore"] = mobile_psi["seo"]
        data["bestPracticesScore"] = mobile_psi["bestPractices"]
        data["a11yScoreMobile"] = mobile_psi["accessibility"]
        data["a11yScoreDesktop"] = desktop_psi["accessibility"]
        data["lighthouseScoresCheckedMonth"] = new_month
    except Exception as e:
        # Both the direct timeout and the one retry inside fetch_psi_score
        # already failed if execution reaches here - a persistent PSI
        # slowdown, not a one-off blip. Falls back to leaving seoScore/
        # bestPracticesScore/a11yScore*/mobilePerfNow/desktopPerfScore
        # exactly as they already were in data.json, rather than writing
        # nulls over real numbers or crashing the whole run (Aug 19 2026:
        # an unhandled timeout here took down page-health, AI-crawler, and
        # GSC keyword updates too, none of which have anything to do with
        # PSI at all). mobile_score/desktop_score set to None so the
        # homepage_updated branch below knows not to touch those two
        # fields either.
        print(f"  WARNING: PSI fetch failed after retry, keeping previous scores this run: {type(e).__name__}: {e}", file=sys.stderr)
        mobile_score = desktop_score = None

    if not homepage_updated:
        print("CrUX/homepage data already at", new_month, "— skipping that part, still running page-health scan below.")
    else:
        prev_now = data.get("mobilePerfNow")
        data["mobilePerfPrev"] = prev_now if prev_now is not None else data.get("mobilePerfPrev")
        # Only overwrite if the fetch above actually succeeded this run -
        # mobile_score/desktop_score are None specifically when PSI failed
        # even after its retry, and reportMonth/monthLabels/etc still need
        # to advance below regardless (CrUX itself did report a new month,
        # independent of whether PSI cooperated), just not these two scores.
        if mobile_score is not None:
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

    print("Scanning per-page health (schema, alt text, on-page, unused CSS/JS) ...")
    page_health, coverage = run_page_health_scan(previous=data.get("pageHealth"), month=new_month)
    data["pageHealth"] = page_health
    data["pageHealthCheckedMonth"] = new_month
    data["scanCoverage"] = coverage
    if coverage.get("truncated"):
        print(f"  NOTE: scanned {coverage['scannedPages']} of {coverage['totalPages']} sitemap pages "
              f"— raise MAX_AUTO_PAGES to cover the rest. Orphan detection skipped this run.")
    if coverage.get("psiRotating"):
        print(f"  PSI: {coverage['psiFresh']} of {coverage['psiAttempted']} attempted pages measured live "
              f"this run; the other {coverage['scannedPages'] - coverage['psiAttempted']} kept their "
              f"previous performance numbers (rotating schedule).")

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

    # Owner-tagged issue lists. Runs after scoring because build_issue_list()
    # needs excludedFromScore to judge whether noindex on a page is correct.
    data["issueSummary"] = attach_issues(data["pageHealth"], coverage)
    s = data["issueSummary"]
    print(f"  Issues: {s['totalIssues']} total — IT {s['byOwner']['it']}, "
          f"Marketing {s['byOwner']['marketing']}, Shared {s['byOwner']['shared']} "
          f"({s['bySeverity']['high']} high / {s['bySeverity']['medium']} medium / "
          f"{s['bySeverity']['low']} low)")

    # Monthly history. Overwrites this month's own entry if the Action is run
    # more than once in a month (workflow_dispatch) rather than appending a
    # duplicate - the latest run within a month is the one that counts. Older
    # months are never modified, only aged out past PAGE_HEALTH_HISTORY_MONTHS.
    history = data.get("pageHealthHistory") or {}
    history[new_month] = snapshot_page_health(data["pageHealth"])
    for stale_key in sorted(history)[:-PAGE_HEALTH_HISTORY_MONTHS]:
        del history[stale_key]
    data["pageHealthHistory"] = history
    print(f"  History: {len(history)} month(s) retained ({', '.join(sorted(history))})")

    print("Checking AI crawler access (robots.txt) and llms.txt ...")
    data["aiSearchReadiness"] = check_ai_search_readiness()
    data["aiSearchReadinessCheckedMonth"] = new_month
    if data["aiSearchReadiness"]:
        blocked = [c["agent"] for c in data["aiSearchReadiness"]["crawlers"] if c["flagIfBlocked"] and not c["allowed"]]
        print(f"  AI crawlers blocked: {blocked or 'none'} | llms.txt present: {data['aiSearchReadiness']['llmsTxtPresent']}")

    print("Building Index Cleanup Map ...")
    try:
        # Sitemap URLs come from the page-health scan that already ran above —
        # no second sitemap fetch needed. Used only to flag published pages the
        # sitemap omits (noindex), which is how the Sept 2026 audit found a live
        # academy page and two campaign landing pages missing from it entirely.
        sitemap_urls = [p.get("url") for p in (data.get("pageHealth") or []) if p.get("url")]
        cleanup = build_index_cleanup(
            sitemap_urls=sitemap_urls,
            previous=data.get("indexCleanup"),
        )
        if cleanup:
            data["indexCleanup"] = cleanup
            print(f"  Index Cleanup: {cleanup['total']} pages, "
                  f"{cleanup['actionNeeded']} need action")
    except Exception as e:
        # One data source must never fail the whole run — same philosophy as the
        # CrUX and PSI handlers above. A WAF block on the WordPress endpoint
        # leaves the previous indexCleanup block in place rather than wiping it.
        print(f"  WARNING: Index Cleanup failed, keeping previous data: "
              f"{type(e).__name__}: {e}", file=sys.stderr)

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

    data["_note"] = build_note(new_month, data.get("keywordsSource", "manual"), coverage)

    # Exact timestamp of THIS run, set unconditionally regardless of which
    # individual sections above succeeded, failed, or were skipped this
    # time. Distinct on purpose from the various *CheckedMonth fields
    # (pageHealthCheckedMonth, lighthouseScoresCheckedMonth, etc.) - those
    # are month-granularity and only advance when that section's own data
    # actually changed; this one is a precise date+time and always moves,
    # since its only job is answering "when did the report last actually
    # run" - a question that came up repeatedly on Aug 19 2026 with no
    # single clear answer anywhere on the page.
    data["lastRefreshedAt"] = datetime.now(timezone.utc).isoformat()

    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print("data.json updated —", "homepage+" if homepage_updated else "", "page-health refreshed for", new_month)
    return 0


if __name__ == "__main__":
    sys.exit(main())
