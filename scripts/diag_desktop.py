"""Temporary diagnostic 2: survey of cached desktop copies at this edge, and
what the origin returns when the firewall cache is bypassed. Prints no secrets."""
import json, re, time, urllib.request, urllib.error, urllib.parse

DESK = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
MOB = "Mozilla/5.0 (Linux; Android 11; moto g power (2022)) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Mobile Safari/537.36"
HDRS = ["x-sucuri-cache", "last-modified", "expires", "set-cookie"]

def fetch(url, ua=DESK, extra=None, method="GET"):
    u = urllib.parse.urlsplit(url)
    url = urllib.parse.urlunsplit((u.scheme, u.netloc, urllib.parse.quote(urllib.parse.unquote(u.path)), u.query, ""))
    h = {"User-Agent": ua, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"}
    h.update(extra or {})
    req = urllib.request.Request(url, headers=h, method=method)
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode("utf-8", "replace"); status = r.status; hd = r.headers
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace"); status = e.code; hd = e.headers
    except Exception as e:
        return {"error": type(e).__name__}
    foot = re.findall(r"cached@(\d+)", body)
    out = {"st": status, "ms": int((time.time() - t) * 1000), "len": len(body), "delayed": len(re.findall("rocketlazyloadscript", body)),
           "cachedAt": time.strftime("%H:%M:%S", time.gmtime(int(foot[-1]))) if foot else None}
    for k in HDRS:
        v = hd.get(k)
        if v: out[k] = (v[:40] if k == "set-cookie" else v.replace("Tue, 10 Nov 2026 ", "").replace(" GMT", ""))
    return out

d = json.load(open("data.json"))
urls = [p["url"] for p in d["pageHealth"] if p.get("url")]
print("NOW", time.strftime("%H:%M:%S", time.gmtime()))
bad = 0
for u in urls:
    r = fetch(u)
    if r.get("delayed") == 0: bad += 1
    print("SURVEY", json.dumps(r, ensure_ascii=False), urllib.parse.unquote(u)[24:70])
    time.sleep(0.4)
print("SURVEY_TOTAL", len(urls), "unoptimized", bad)

home = "https://www.silah.com.sa/"
print("BYPASS tests on homepage")
for label, extra in [
    ("cookie foo", {"Cookie": "foo=bar"}),
    ("cookie phpsessid", {"Cookie": "PHPSESSID=abcdef0123456789abcdef0123"}),
    ("cookie wp-settings", {"Cookie": "wp-settings-time-1=1794300000"}),
    ("cache-control no-cache", {"Cache-Control": "no-cache", "Pragma": "no-cache"}),
    ("authorization", {"Authorization": "Basic Zm9vOmJhcg=="}),
]:
    print("BYP", label, json.dumps(fetch(home, extra=extra), ensure_ascii=False)); time.sleep(1)
print("HEAD", json.dumps(fetch(home, method="HEAD"), ensure_ascii=False))
print("MOBILE", json.dumps(fetch(home, ua=MOB), ensure_ascii=False))

# Does the firewall share one cached copy between a bot UA and a normal desktop UA? (unique query URL, no real page touched)
q = home + "?diag=" + str(int(time.time()))
for label, ua in [("facebook", "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"), ("desktop", DESK), ("mobile", MOB), ("desktop-again", DESK)]:
    print("KEY", label, json.dumps(fetch(q, ua=ua), ensure_ascii=False)); time.sleep(1)
