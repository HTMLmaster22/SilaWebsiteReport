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


home = "https://www.silah.com.sa/"
AUTH = {"Authorization": "Basic Zm9vOmJhcg=="}   # makes the firewall pass the request straight to the origin (nothing is stored)
r0 = fetch(home); print("EDGE desktop", json.dumps(r0)); print("EDGE mobile", json.dumps(fetch(home, ua=MOB)))
UAS = {
 "chrome-desktop": DESK,
 "lighthouse-desktop": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36",
 "ipad": "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
 "whatsapp-android": "WhatsApp/2.24.20.89 A",
 "whatsapp-ios": "WhatsApp/2.24.20.80 i",
 "facebook": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
 "meta-agent": "meta-externalagent/1.1 (+https://developers.facebook.com/docs/sharing/webmasters/crawler)",
 "googlebot": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
 "googlebot-desktop": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Googlebot/2.1; +http://www.google.com/bot.html) Chrome/130.0.0.0 Safari/537.36",
 "bingbot": "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
 "tiktok": "Mozilla/5.0 (compatible; TikTokSpider; ttspider-feedback@tiktok.com)",
 "bytespider": "Mozilla/5.0 (compatible; Bytespider; spider-feedback@bytedance.com)",
 "twitter": "Twitterbot/1.0",
 "linkedin": "LinkedInBot/1.0 (compatible; Mozilla/5.0; Apache-HttpClient +http://www.linkedin.com)",
 "telegram": "TelegramBot (like TwitterBot)",
 "slack": "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)",
 "gptbot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.2; +https://openai.com/gptbot)",
 "ahrefs": "Mozilla/5.0 (compatible; AhrefsBot/7.0; +http://ahrefs.com/robot/)",
 "semrush": "Mozilla/5.0 (compatible; SemrushBot/7~bl; +http://www.semrush.com/bot.html)",
 "rocket-preload": "WP Rocket/Preload",
 "wordpress-loopback": "WordPress/6.8; https://www.silah.com.sa",
 "sucuri-scanner": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.0.0 Safari/537.36 Sucuri/Scanner",
 "uptimerobot": "Mozilla/5.0+(compatible; UptimeRobot/2.0; http://www.uptimerobot.com/)",
 "gtmetrix": "Mozilla/5.0 (X11; Linux x86_64; GTmetrix https://gtmetrix.com/) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36",
 "curl": "curl/8.5.0",
 "python": "Python-urllib/3.12",
 "empty": "",
 "old-ie": "Mozilla/4.0 (compatible; MSIE 8.0; Windows NT 6.1)",
 "engati": "Engati",
}
for name, ua in UAS.items():
    r = fetch(home, ua=ua, extra=AUTH)
    print("ORIGIN", "OPT " if r.get("delayed") else "RAW ", name.ljust(20), json.dumps(r)); time.sleep(0.5)
for label, ck in [("comment_author", "comment_author_abc=x"), ("wp-postpass", "wp-postpass_abc=x"), ("woo-cart", "woocommerce_items_in_cart=1"), ("pll_language", "pll_language=ar"), ("wpml", "wp-wpml_current_language=ar"), ("random", "foo=bar")]:
    r = fetch(home, extra=dict(AUTH, Cookie=ck)); print("ORIGIN-COOKIE", "OPT " if r.get("delayed") else "RAW ", label.ljust(16), json.dumps(r)); time.sleep(0.5)
for label, extra in [("accept-json", {"Accept": "application/json"}), ("accept-any", {"Accept": "*/*"}), ("no-accept-lang", {"Accept-Language": ""}), ("x-requested-with", {"X-Requested-With": "XMLHttpRequest"}), ("http1.0-ish", {"Connection": "close"})]:
    r = fetch(home, extra=dict(AUTH, **extra)); print("ORIGIN-HDR", "OPT " if r.get("delayed") else "RAW ", label.ljust(16), json.dumps(r)); time.sleep(0.5)
r = fetch(home, extra=AUTH, method="HEAD"); print("ORIGIN-HEAD", json.dumps(r))
# which visitors share one firewall copy? unique query URL per pair, no real page touched
n = int(time.time())
for i, name in enumerate(["whatsapp-android", "whatsapp-ios", "ipad", "googlebot", "rocket-preload", "curl", "empty", "tiktok", "wordpress-loopback", "lighthouse-desktop"]):
    q = home + "?diag=" + str(n + i)
    a = fetch(q, ua=UAS[name]); time.sleep(0.6); b = fetch(q, ua=DESK); time.sleep(0.6); c = fetch(q, ua=MOB)
    print("SHARE", name.ljust(20), "first:", a.get("x-sucuri-cache"), "| then desktop:", b.get("x-sucuri-cache"), "| then mobile:", c.get("x-sucuri-cache"))
print("EDGE-AFTER desktop", json.dumps(fetch(home)))
