"""Read-only live check of a list of URLs: HTTP status, redirects, robots
(meta + X-Robots-Tag), title, meta description, H1 count, sitemap membership.
Input: scripts/url_audit_list.tsv (category<TAB>url<TAB>requested action)."""
import gzip, html, re, sys, urllib.parse, urllib.request, xml.etree.ElementTree as ET

UA = "Mozilla/5.0 (compatible; SilahReportBot/1.0)"
def iri(url):
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, urllib.parse.quote(urllib.parse.unquote(p.path), safe="/%"), p.query, p.fragment))

def get(url):
    req = urllib.request.Request(iri(url), headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            body = r.read()
            if r.headers.get("Content-Encoding") == "gzip": body = gzip.decompress(body)
            return r.status, r.geturl(), dict(r.headers), body.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, url, dict(e.headers or {}), ""
    except Exception as e:
        return None, url, {}, f"ERR {e}"

def norm(u):
    p = urllib.parse.urlsplit(u.strip())
    path = urllib.parse.unquote(p.path).rstrip("/") + "/"
    q = ("?" + p.query) if p.query else ""
    return path + q

# sitemap URLs
sm = set()
st, _, _, idx = get("https://www.silah.com.sa/sitemap_index.xml")
children = re.findall(r"<loc>(.*?)</loc>", idx)
for c in children:
    _, _, _, body = get(html.unescape(c))
    for loc in re.findall(r"<loc>(.*?)</loc>", body):
        sm.add(norm(html.unescape(loc)))
print(f"SITEMAP children={len(children)} urls={len(sm)}")
print("SITEMAP_CHILDREN " + " ".join(children))

for line in open("scripts/url_audit_list.tsv", encoding="utf-8"):
    cat, url, action = (line.rstrip("\n").split("\t") + ["", ""])[:3]
    code, final, hdr, body = get(url)
    robots_meta = re.findall(r'<meta[^>]+name=["\']robots["\'][^>]*content=["\']([^"\']*)', body, re.I)
    xr = hdr.get("X-Robots-Tag") or hdr.get("x-robots-tag") or ""
    title = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
    desc = re.findall(r'<meta[^>]+name=["\']description["\'][^>]*content=["\']([^"\']*)', body, re.I)
    h1 = len(re.findall(r"<h1[\s>]", body, re.I))
    pw = "post-password-form" in body
    redirected = norm(final) != norm(url)
    print("ROW\t" + "\t".join([
        cat, url, action, str(code), final if redirected else "-",
        (robots_meta[0] if robots_meta else "-"), xr or "-",
        html.unescape(title.group(1).strip()) if title else "-",
        str(len(html.unescape(desc[0]))) if desc and desc[0].strip() else "0",
        str(h1), "pw" if pw else "-", "sitemap" if norm(url) in sm else "-",
    ]))

# Are the "whylinkus" / link-number items content blocks on the homepage?
_, _, _, home = get("https://www.silah.com.sa/")
for phrase in ["إدارة التوظيف والاستقطاب", "برمجية متطورة لرفع جودة الكوادر السعودية",
               "تخطيط التدريب وقياس التطوير المهني", "تعهيد المشروعات وتشغيل مراكز الأعمال",
               "خبراء وفرق متخصصة لاستشارات التوطين", "1450", "17000", "6250"]:
    print("HOME\t" + phrase + "\t" + ("FOUND" if phrase in html.unescape(home) else "absent"))
