"""Temporary check: does the origin now give link-preview bots the optimized page? Prints no secrets."""
import json, re, time, urllib.request, urllib.error, urllib.parse
DESK = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
MOB = "Mozilla/5.0 (Linux; Android 11; moto g power (2022)) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Mobile Safari/537.36"
UAS = {"desktop": DESK, "mobile": MOB, "whatsapp-android": "WhatsApp/2.24.20.89 A", "whatsapp-ios": "WhatsApp/2.24.20.80 i",
       "facebook": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
       "facebook-catalog": "facebookcatalog/1.0", "meta-agent": "meta-externalagent/1.1 (+https://developers.facebook.com/docs/sharing/webmasters/crawler)",
       "twitter": "Twitterbot/1.0", "linkedin": "LinkedInBot/1.0 (compatible; Mozilla/5.0; Apache-HttpClient +http://www.linkedin.com)",
       "telegram": "TelegramBot (like TwitterBot)", "googlebot": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
       "lighthouse": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36"}
AUTH = {"Authorization": "Basic Zm9vOmJhcg=="}
def fetch(url, ua, extra=None):
    u = urllib.parse.urlsplit(url); url = urllib.parse.urlunsplit((u.scheme, u.netloc, urllib.parse.quote(urllib.parse.unquote(u.path)), u.query, ""))
    h = {"User-Agent": ua, "Accept": "text/html,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"}; h.update(extra or {})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60) as r:
            body = r.read().decode("utf-8", "replace"); st = r.status; hd = r.headers
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace"); st = e.code; hd = e.headers
    except Exception as e:
        return {"error": type(e).__name__}
    og = lambda p: bool(re.search(r'<meta[^>]+property=["\']og:%s["\'][^>]+content=["\'][^"\']+' % p, body))
    return {"st": st, "kb": len(body)//1024, "delayed": len(re.findall("rocketlazyloadscript", body)), "sucuri": hd.get("x-sucuri-cache"),
            "og_title": og("title"), "og_image": og("image"), "og_desc": og("description"), "title": bool(re.search(r"<title>[^<]+", body)),
            "err": bool(re.search(r"Fatal error|critical error", body))}
print("NOW", time.strftime("%H:%M:%S", time.gmtime()))
for page in ["https://www.silah.com.sa/", "https://www.silah.com.sa/about-us/", "https://www.silah.com.sa/solutions/"]:
    for name, ua in UAS.items():
        r = fetch(page, ua, AUTH); print("ORIGIN", "OPT" if r.get("delayed") else "RAW", name.ljust(18), page[24:].ljust(12), json.dumps(r)); time.sleep(0.4)
for name in ["desktop", "mobile", "lighthouse"]:
    for i in range(2):
        r = fetch("https://www.silah.com.sa/", UAS[name]); print("EDGE", "OPT" if r.get("delayed") else "RAW", name.ljust(18), json.dumps(r)); time.sleep(0.6)
