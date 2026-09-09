"""
Index Cleanup Map — WordPress inventory crossed against live reachability.

WHY THIS EXISTS
---------------
Every other check in this report starts from the sitemap. A sitemap only ever
lists pages that are published AND indexable, which makes it structurally
blind to the exact thing the Sept 2026 audit asked about: pages that are NOT
published but that a customer can still reach.

The Sept 2026 scan made the size of that blind spot concrete — the sitemap
listed 93 URLs; WordPress itself reported 139. The 46-page difference was
drafts, private pages, trashed pages, and 17 published pages excluded from the
sitemap by a noindex flag (including a live e-learning academy page and two
live campaign landing pages).

So this module reads from two sources and crosses them:

  WordPress says   publish / draft / pending / private / future / trash
  The live web says 200 / 3xx / 404

Neither source alone answers the question. A page can be drafted in WordPress
and still return 200 to a visitor (stale cache, or a plugin serving it). A page
can be published and still 404. The cross-product is the finding.

DEPENDENCY
----------
Needs the read-only endpoint installed on the WordPress side:
    GET /wp-json/silah/v1/inventory
    header: X-Silah-Key: <secret>

The key comes from the INVENTORY_KEY env var (a GitHub repo secret, mirrored
into Vercel). It is never written into data.json and never reaches the browser.

If the key is missing or the endpoint is unreachable, this module returns None
and the caller leaves the previous indexCleanup block untouched — same
philosophy as the CrUX and PSI fetches in update_data.py: one failing data
source never fails the whole run.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ORIGIN = "https://www.silah.com.sa"
INVENTORY_URL = f"{ORIGIN}/wp-json/silah/v1/inventory"

# Sucuri throttled the PSI runs in Aug 2026 and the GitHub Action runs from an
# IP range the WAF has never seen, so keep the live checks unhurried.
LIVE_CHECK_DELAY_SECONDS = float(os.environ.get("INDEX_CLEANUP_DELAY", "0.4"))
LIVE_CHECK_TIMEOUT = 20
MAX_REDIRECT_HOPS = 5

USER_AGENT = (
    "Mozilla/5.0 (compatible; SilahReportBot/1.0; "
    "+https://sila-website-report.vercel.app)"
)

OWNER_IT = "it"
OWNER_MARKETING = "marketing"
OWNER_SHARED = "shared"


# ---------------------------------------------------------------------------
# Verdict model
# ---------------------------------------------------------------------------
# The whole point of the module is this table. Each verdict is a WordPress
# status crossed with what a visitor actually gets, plus the plain-language
# consequence — written for Marketing, who are the primary readers and should
# not have to interpret an HTTP status code.
#
# severity "high" means a customer-visible defect or something publicly
# reachable that shouldn't be. "medium" is hygiene. "ok" is no action.

VERDICTS = {
    "live_ok": {
        "ar": "منشورة وتعمل",
        "en": "Published and working",
        "severity": "ok", "owner": None,
        "actionAr": "—",
        "actionEn": "—",
    },
    "live_redirect": {
        "ar": "منشورة ومعاد توجيهها",
        "en": "Published, redirects",
        "severity": "medium", "owner": OWNER_IT,
        "actionAr": "مراجعة التوجيه — يجب أن يصل الزائر للصفحة مباشرة دون تحويل",
        "actionEn": "Review the redirect — visitors should land directly",
    },
    "published_broken": {
        "ar": "منشورة لكنها لا تفتح",
        "en": "Published but not loading",
        "severity": "high", "owner": OWNER_IT,
        "actionAr": "إصلاح عاجل — الصفحة منشورة لكن الزائر يصل إلى خطأ",
        "actionEn": "Urgent — page is published but visitors hit an error",
    },
    "draft_reachable": {
        "ar": "مسودة لكن يمكن الوصول لها",
        "en": "Draft but publicly reachable",
        "severity": "high", "owner": OWNER_IT,
        "actionAr": "الصفحة غير منشورة لكنها تُفتح للعامة — يجب حجبها أو حذفها",
        "actionEn": "Unpublished but publicly accessible — block or delete it",
    },
    "draft_hidden": {
        "ar": "مسودة وغير متاحة",
        "en": "Draft, not accessible",
        "severity": "ok", "owner": None,
        "actionAr": "—",
        "actionEn": "—",
    },
    "trash_reachable": {
        "ar": "محذوفة لكنها لا تزال تفتح",
        "en": "Deleted but still reachable",
        "severity": "high", "owner": OWNER_IT,
        "actionAr": "الصفحة محذوفة لكنها تُفتح — يجب إضافة إعادة توجيه أو حجبها",
        "actionEn": "Deleted but still loading — add a redirect or block it",
    },
    "trash_gone": {
        "ar": "محذوفة ولا تفتح",
        "en": "Deleted, not reachable",
        "severity": "medium", "owner": OWNER_MARKETING,
        "actionAr": "يوصى بإضافة إعادة توجيه إن كانت الصفحة تتلقى زيارات سابقاً",
        "actionEn": "Add a redirect if the page previously received traffic",
    },
    "private": {
        "ar": "خاصة — للمسجلين فقط",
        "en": "Private — logged-in only",
        "severity": "ok", "owner": None,
        "actionAr": "—",
        "actionEn": "—",
    },
    "scheduled": {
        "ar": "مجدولة للنشر",
        "en": "Scheduled",
        "severity": "ok", "owner": None,
        "actionAr": "—",
        "actionEn": "—",
    },
    "pending": {
        "ar": "بانتظار المراجعة",
        "en": "Pending review",
        "severity": "medium", "owner": OWNER_MARKETING,
        "actionAr": "بانتظار قرار النشر",
        "actionEn": "Awaiting a publish decision",
    },
    "hidden_from_search": {
        "ar": "منشورة لكنها خارج خريطة الموقع",
        "en": "Published but not in the sitemap",
        "severity": "medium", "owner": OWNER_SHARED,
        "actionAr": "الصفحة تعمل ويمكن للعميل فتحها لكنها لا تظهر في نتائج البحث — يُراجع إن كان ذلك مقصوداً",
        "actionEn": "Reachable by customers but excluded from search — confirm this is intended",
    },
}


def _request(url, headers=None, method="GET", allow_redirects=False, timeout=None):
    """One HTTP call. Never raises for HTTP status — a 404 is data, not an error."""
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", USER_AGENT)
    for k, v in (headers or {}).items():
        req.add_header(k, v)

    opener_handlers = []
    if not allow_redirects:
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener_handlers.append(_NoRedirect())

    opener = urllib.request.build_opener(*opener_handlers)

    try:
        with opener.open(req, timeout=timeout or LIVE_CHECK_TIMEOUT) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except Exception:
            pass
        return e.code, dict(e.headers or {}), body
    except Exception as e:
        return 0, {}, str(e).encode("utf-8", "replace")


def fetch_wordpress_inventory():
    """Pull every page and post from WordPress, with its real status.

    Returns the parsed payload, or None if the endpoint is unavailable —
    the caller keeps whatever indexCleanup block is already in data.json.
    """
    key = os.environ.get("INVENTORY_KEY", "").strip()
    if not key:
        print("  index-cleanup: INVENTORY_KEY not set — skipping this run.",
              file=sys.stderr)
        return None

    status, _headers, body = _request(
        INVENTORY_URL,
        headers={"X-Silah-Key": key, "Accept": "application/json"},
        allow_redirects=True,
        timeout=90,
    )

    if status != 200:
        # 403 here almost always means the WAF blocked us rather than a bad
        # key — the endpoint itself returns JSON on auth failure. Worth saying
        # so, because the fix is different (whitelist the runner IP, not
        # rotate the secret).
        hint = " (likely the WAF, not the key — the endpoint returns JSON on auth failure)" if status == 403 else ""
        print(f"  index-cleanup: endpoint returned HTTP {status}{hint} — skipping.",
              file=sys.stderr)
        return None

    try:
        return json.loads(body.decode("utf-8"))
    except Exception as e:
        print(f"  index-cleanup: response was not valid JSON ({e}) — skipping.",
              file=sys.stderr)
        return None


def check_live(url):
    """What a visitor actually gets: final status, hop count, final URL."""
    current = url
    hops = 0
    first = None

    for _ in range(MAX_REDIRECT_HOPS):
        status, headers, _body = _request(current, allow_redirects=False)

        if first is None:
            first = status

        if status in (301, 302, 303, 307, 308):
            location = headers.get("Location") or headers.get("location")
            if not location:
                break
            current = urllib.parse.urljoin(current, location)
            hops += 1
            time.sleep(LIVE_CHECK_DELAY_SECONDS)
            continue

        return {"code": status, "hops": hops, "finalUrl": current, "firstCode": first}

    return {"code": 0, "hops": hops, "finalUrl": current, "firstCode": first}


def _normalize(url):
    if not url:
        return ""
    u = urllib.parse.unquote(url).split("#")[0].rstrip("/")
    u = u.replace("http://", "https://")
    return u.replace("https://silah.com.sa", "https://www.silah.com.sa")


def classify(wp_status, live, in_sitemap):
    """Cross WordPress status with live reachability into one verdict key."""
    code = live["code"]
    reachable = code == 200
    redirected = live["hops"] > 0 and code == 200

    if wp_status == "publish":
        if code == 404 or code == 0:
            return "published_broken"
        if redirected:
            return "live_redirect"
        if reachable and not in_sitemap:
            return "hidden_from_search"
        return "live_ok"

    if wp_status == "draft":
        return "draft_reachable" if reachable else "draft_hidden"

    if wp_status == "trash":
        return "trash_reachable" if reachable else "trash_gone"

    if wp_status == "private":
        return "private"
    if wp_status == "future":
        return "scheduled"
    if wp_status == "pending":
        return "pending"

    return "live_ok" if reachable else "draft_hidden"


def build_index_cleanup(sitemap_urls=None, previous=None):
    """Assemble the whole indexCleanup block for data.json.

    sitemap_urls: iterable of URLs already discovered from the sitemap this
    run, used to detect published-but-not-in-sitemap pages. Pass None to skip
    that particular flag rather than report it wrongly.
    """
    payload = fetch_wordpress_inventory()
    if not payload or not payload.get("items"):
        return previous

    sitemap_set = {_normalize(u) for u in (sitemap_urls or [])}
    have_sitemap = bool(sitemap_set)

    items = []
    total = len(payload["items"])
    print(f"  index-cleanup: WordPress reported {total} pages — checking each live ...")

    for n, raw in enumerate(payload["items"], 1):
        url = raw.get("url") or ""

        # Private and draft pages are often exposed only as ?page_id=NN. Those
        # are still worth live-checking — that IS the URL a customer could hold.
        live = check_live(url)

        in_sitemap = _normalize(url) in sitemap_set if have_sitemap else True
        verdict_key = classify(raw.get("status", ""), live, in_sitemap)
        v = VERDICTS[verdict_key]

        items.append({
            "id": raw.get("id"),
            "title": (raw.get("title") or "").strip(),
            "url": url,
            "slug": raw.get("slug"),
            "type": raw.get("type"),
            "typeLabel": raw.get("type_label"),
            "wpStatus": raw.get("status"),
            "liveCode": live["code"],
            "hops": live["hops"],
            "finalUrl": live["finalUrl"] if live["hops"] else None,
            "published": raw.get("published"),
            "modified": raw.get("modified"),
            "linkCount": raw.get("link_count", 0),
            "internalLinks": raw.get("internal_links", [])[:50],
            "inSitemap": in_sitemap if have_sitemap else None,
            "verdict": verdict_key,
            "verdictAr": v["ar"],
            "verdictEn": v["en"],
            "severity": v["severity"],
            "owner": v["owner"],
            "actionAr": v["actionAr"],
            "actionEn": v["actionEn"],
        })

        if n % 25 == 0:
            print(f"    {n}/{total} checked")

        time.sleep(LIVE_CHECK_DELAY_SECONDS)

    by_status, by_verdict, by_severity = {}, {}, {}
    for it in items:
        by_status[it["wpStatus"]] = by_status.get(it["wpStatus"], 0) + 1
        by_verdict[it["verdict"]] = by_verdict.get(it["verdict"], 0) + 1
        by_severity[it["severity"]] = by_severity.get(it["severity"], 0) + 1

    # Duplicate titles: three pages called "احصل على تسعيرة" is a content
    # problem Marketing can act on, and it only shows up once drafts are
    # visible — which is exactly what this module adds.
    titles = {}
    for it in items:
        t = it["title"].strip()
        if t:
            titles.setdefault(t, []).append(it["url"])
    duplicates = [
        {"title": t, "count": len(urls), "urls": urls[:6]}
        for t, urls in titles.items() if len(urls) > 1
    ]
    duplicates.sort(key=lambda d: -d["count"])

    action_needed = sum(1 for it in items if it["severity"] in ("high", "medium"))

    return {
        "checkedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "generatedAt": payload.get("generated"),
        "source": "wordpress+live",
        "total": len(items),
        "byStatus": by_status,
        "byVerdict": by_verdict,
        "bySeverity": by_severity,
        "actionNeeded": action_needed,
        "duplicateTitles": duplicates[:15],
        "sitemapComparisonAvailable": have_sitemap,
        "sitemapCount": len(sitemap_set) if have_sitemap else None,
        "items": items,
    }
