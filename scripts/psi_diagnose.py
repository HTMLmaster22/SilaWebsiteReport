"""On-demand PSI diagnostics: which scripts block the main thread.
Runs mobile + desktop PSI for the homepage (3 runs each) and prints the
heaviest scripts, third parties and long tasks. Read-only: writes nothing."""
import json, os, sys, urllib.parse, urllib.request

KEY = os.environ["PSI_API_KEY"]
URL = os.environ.get("TARGET_URL", "https://www.silah.com.sa/")

def run(strategy):
    q = urllib.parse.urlencode({"url": URL, "strategy": strategy, "key": KEY, "category": "performance"})
    with urllib.request.urlopen(f"https://www.googleapis.com/pagespeedonline/v5/runPagespeed?{q}", timeout=240) as r:
        return json.loads(r.read().decode())["lighthouseResult"]

for strategy in ("desktop", "mobile"):
    for i in range(3):
        try:
            lh = run(strategy)
        except Exception as e:
            print(f"### {strategy} run {i+1}: FAILED {e}"); continue
        a = lh["audits"]
        print(f"\n### {strategy} run {i+1}  score={round(lh['categories']['performance']['score']*100)}  "
              f"TBT={round(a['total-blocking-time']['numericValue'])}ms  LCP={round(a['largest-contentful-paint']['numericValue'])}ms  "
              f"TTFB={round(a['server-response-time']['numericValue'])}ms  fetch={lh['fetchTime']}")
        print("-- bootup-time (script execution, ms):")
        for it in sorted(a.get("bootup-time", {}).get("details", {}).get("items", []), key=lambda x: -x.get("total", 0))[:12]:
            print(f"   {round(it.get('total',0)):>6} total  {round(it.get('scripting',0)):>6} scripting  {it.get('url')}")
        print("-- third parties (blocking ms / main-thread ms):")
        for it in a.get("third-party-summary", {}).get("details", {}).get("items", [])[:10]:
            ent = it.get("entity"); ent = ent.get("text") if isinstance(ent, dict) else ent
            print(f"   {round(it.get('blockingTime',0)):>6} / {round(it.get('mainThreadTime',0)):>6}  {ent}")
        print("-- long tasks:")
        for it in a.get("long-tasks", {}).get("details", {}).get("items", [])[:10]:
            print(f"   {round(it.get('duration',0)):>6}ms  start {round(it.get('startTime',0))}  {it.get('url')}")
        print("-- main-thread breakdown:")
        for it in a.get("mainthread-work-breakdown", {}).get("details", {}).get("items", [])[:6]:
            print(f"   {round(it.get('duration',0)):>6}ms  {it.get('groupLabel') or it.get('group')}")
