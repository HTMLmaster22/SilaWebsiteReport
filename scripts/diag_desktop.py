"""Temporary diagnostic: what does the homepage look like from this runner,
and what does Google's desktop test see? Prints no secrets."""
import json, os, re, sys, time, urllib.parse, urllib.request, urllib.error

SITE = "https://www.silah.com.sa/"
UAS = {
    "desktop-lh": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36",
    "desktop-win": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "mobile-lh": "Mozilla/5.0 (Linux; Android 11; moto g power (2022)) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Mobile Safari/537.36",
}
HDRS = ["x-sucuri-cache", "x-sucuri-id", "age", "cache-control", "vary", "last-modified", "etag", "expires", "server", "x-cache", "x-rocket-nginx-serving-static"]

def fetch(url, ua):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode("utf-8", "replace"); status = r.status; h = r.headers
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace"); status = e.code; h = e.headers
    ms = int((time.time() - t) * 1000)
    foot = re.findall(r"<!--[^>]{0,200}(?:Rocket|cached@)[^>]{0,200}-->", body)
    return {
        "status": status, "ms": ms, "len": len(body),
        "hdr": {k: h.get(k) for k in HDRS if h.get(k)},
        "delayed": len(re.findall("rocketlazyloadscript", body)),
        "engati_plain": bool(re.search(r"<script[^>]*\ssrc=[\"'][^\"']*engati", body)),
        "engati_any": "engati" in body,
        "recaptcha_plain": bool(re.search(r"<script[^>]*\ssrc=[\"'][^\"']*recaptcha/api", body)),
        "gtm_inline_plain": bool(re.search(r"<script(?![^>]*rocketlazyloadscript)[^>]*>[^<]{0,400}googletagmanager", body)),
        "foot": foot[-1:] ,
        "rucss": "wpr-usedcss" in body,
    }

try:
    with urllib.request.urlopen("https://ipinfo.io/json", timeout=20) as r:
        j = json.load(r); print("RUNNER", j.get("city"), j.get("region"), j.get("country"), j.get("org"))
except Exception as e:
    print("RUNNER lookup failed", type(e).__name__)

for name, ua in UAS.items():
    for i in range(4):
        print("FETCH", name, i, json.dumps(fetch(SITE, ua), ensure_ascii=False)); time.sleep(2)
    print("FRESH", name, json.dumps(fetch(SITE + "?diag=" + str(int(time.time())), ua), ensure_ascii=False))

KEY = os.environ.get("PSI_API_KEY", "")
def psi(strategy):
    url = ("https://www.googleapis.com/pagespeedonline/v5/runPagespeed?url=" + urllib.parse.quote(SITE, safe="")
           + "&strategy=" + strategy + "&category=performance&key=" + KEY)
    try:
        with urllib.request.urlopen(url, timeout=200) as r:
            d = json.load(r)
    except Exception as e:
        print("PSI", strategy, "failed", type(e).__name__, getattr(e, "code", "")); return
    lh = d["lighthouseResult"]; a = lh["audits"]
    doc = None
    for it in (a.get("network-requests", {}).get("details", {}) or {}).get("items", []):
        if it.get("resourceType") == "Document":
            doc = {k: it.get(k) for k in ("url", "statusCode", "transferSize", "resourceSize", "networkRequestTime", "networkEndTime")}; break
    boot = [(re.sub(r"\?.*", "", x.get("url", ""))[-70:], int(x.get("total", 0))) for x in (a.get("bootup-time", {}).get("details", {}) or {}).get("items", [])[:6]]
    print("PSI", strategy, json.dumps({
        "score": round(lh["categories"]["performance"]["score"] * 100),
        "tbt": int(a["total-blocking-time"]["numericValue"]), "lcp": int(a["largest-contentful-paint"]["numericValue"]),
        "fcp": int(a["first-contentful-paint"]["numericValue"]), "ttfb": int(a.get("server-response-time", {}).get("numericValue", -1)),
        "fetchTime": lh.get("fetchTime"), "doc": doc, "boot": boot,
        "requests": len((a.get("network-requests", {}).get("details", {}) or {}).get("items", [])),
    }, ensure_ascii=False))

for s in ("desktop", "desktop", "desktop", "mobile"):
    psi(s)
for name in ("desktop-lh", "mobile-lh"):
    print("AFTER", name, json.dumps(fetch(SITE, UAS[name]), ensure_ascii=False))
