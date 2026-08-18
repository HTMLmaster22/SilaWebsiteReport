// POST /api/refresh-report
//
// Lets the public report page trigger a fresh scan without anyone needing
// GitHub access. The GitHub token this needs lives ONLY here, as a Vercel
// environment variable (GH_DISPATCH_TOKEN) - it is never sent to the
// browser in any form. The button on index.html calls this endpoint, not
// GitHub directly, which is the whole point: a public static page has no
// safe way to hold a credential itself.
//
// Token requirements (set up once in GitHub, then pasted into Vercel's
// project settings as GH_DISPATCH_TOKEN - see SETUP.md):
//   Fine-grained personal access token, scoped to ONLY the
//   HTMLmaster22/SilaWebsiteReport repository, with ONLY the
//   "Actions: Read and write" permission. Nothing broader than that -
//   this token can start a workflow run and check its status, and
//   nothing else, even in a worst case where it somehow leaked.
//
// Guards against overlapping runs before triggering anything, for the
// same reason the git-conflict bug happened in the first place (Aug 17
// 2026): two scans racing to write data.json at once causes real
// conflicts. Checking first and refusing to double-trigger prevents that
// class of problem at the source instead of needing the git-side fix to
// clean up after it.

const OWNER = "HTMLmaster22";
const REPO = "SilaWebsiteReport";
const WORKFLOW_FILE = "update-report-data.yml";
const GITHUB_API = `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW_FILE}`;

// Minimum time since the last run completed before allowing another
// manual trigger. Not about GitHub cost (Actions minutes are cheap) - it's
// about the underlying data sources: PSI/CrUX/GSC numbers don't
// meaningfully change minute to minute, so a second run five minutes
// after the first would just burn API quota and 9 more minutes for
// identical results.
const COOLDOWN_MINUTES = 15;

async function githubRequest(path, options = {}) {
  const token = process.env.GH_DISPATCH_TOKEN;
  if (!token) {
    throw new Error("GH_DISPATCH_TOKEN is not configured on the server");
  }
  const res = await fetch(`${GITHUB_API}${path}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      ...(options.headers || {}),
    },
  });
  return res;
}

module.exports = async (req, res) => {
  // Reject anything but POST - this action changes state (starts a real
  // workflow run), so it should never be reachable via a plain GET, which
  // browsers, crawlers, and link previews can trigger accidentally.
  if (req.method !== "POST") {
    res.status(405).json({ ok: false, error: "Use POST." });
    return;
  }

  try {
    const runsRes = await githubRequest("/runs?per_page=3");
    if (!runsRes.ok) {
      const text = await runsRes.text();
      res.status(502).json({ ok: false, error: `GitHub API error checking run status: ${runsRes.status} ${text}` });
      return;
    }
    const runsData = await runsRes.json();
    const runs = runsData.workflow_runs || [];

    const inFlight = runs.find(r => r.status === "in_progress" || r.status === "queued");
    if (inFlight) {
      res.status(409).json({
        ok: false,
        error: "already_running",
        message: "تحديث قيد التشغيل بالفعل، يستغرق حوالي ٩ دقائق. جرّب بعد قليل.",
      });
      return;
    }

    const lastRun = runs[0];
    if (lastRun && lastRun.updated_at) {
      const minutesSinceLastRun = (Date.now() - new Date(lastRun.updated_at).getTime()) / 60000;
      if (minutesSinceLastRun < COOLDOWN_MINUTES) {
        const waitMore = Math.ceil(COOLDOWN_MINUTES - minutesSinceLastRun);
        res.status(429).json({
          ok: false,
          error: "cooldown",
          message: `تم التحديث مؤخراً. جرّب مرة ثانية بعد ${waitMore} دقيقة.`,
        });
        return;
      }
    }

    const dispatchRes = await githubRequest("/dispatches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ref: "main" }),
    });

    if (dispatchRes.status !== 204) {
      const text = await dispatchRes.text();
      res.status(502).json({ ok: false, error: `GitHub refused the trigger: ${dispatchRes.status} ${text}` });
      return;
    }

    res.status(200).json({
      ok: true,
      message: "بدأ التحديث! يستغرق حوالي ٩ دقائق — أعد تحميل الصفحة بعدها لرؤية البيانات الجديدة.",
    });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message || "Unknown server error" });
  }
};
