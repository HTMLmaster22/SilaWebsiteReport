// GET /api/run-status
//
// Companion to /api/refresh-report. That endpoint starts a scan and returns
// immediately; this one answers "what's happening right now?" so the page can
// show live progress instead of a static "come back in a few minutes" message
// that the person has no way to verify.
//
// Uses the same GH_DISPATCH_TOKEN, which already carries "Actions: Read and
// write" on this one repository — reading run status needs no extra permission
// and no second credential.
//
// HONESTY NOTE, because this shapes the whole design: GitHub does NOT report a
// percentage for a running workflow. It reports queued / in_progress /
// completed, plus per-step status. So this endpoint deliberately does not
// invent a progress number. What it returns is all real:
//   - the actual run state
//   - which named step is currently executing, mapped to a plain-language phase
//   - elapsed seconds, measured from the run's own start time
//   - an ETA derived from how long recent successful runs of THIS workflow
//     actually took — a measured average, not a hardcoded guess
// The UI presents the ETA as an estimate and lets elapsed time run past it
// rather than pinning a fake bar at 99%.

const OWNER = "HTMLmaster22";
const REPO = "SilaWebsiteReport";
const WORKFLOW_FILE = "update-report-data.yml";
const API_ROOT = `https://api.github.com/repos/${OWNER}/${REPO}`;

// How many recent successful runs to average for the ETA. Small enough to
// track real changes in run length (the Sept 2026 scanner takes ~26 min where
// the older one took ~10, and the ETA should follow that within a run or two
// rather than being anchored to ancient history), large enough that one
// unusually slow run doesn't skew it.
// Sept 6 2026: lowered 5 -> 3. With 5, the estimate stayed anchored to older
// ~10-minute runs and told someone "~17 min" on a run that took 42 — worse
// than no estimate, because it looks broken rather than slow. Three samples
// track a real change in run length within a run or two, which is what
// matters on the day the scanner changes.
const ETA_SAMPLE_RUNS = 3;
// Used only when there is no completed-run history at all to average.
const ETA_FALLBACK_SECONDS = 25 * 60;

// Maps the workflow's actual step names to a phase key the page can translate.
// Matched by substring so a step rename like "Install dependencies (pip)"
// still lands correctly instead of silently falling through to "working".
const STEP_PHASES = [
  { match: "set up job", phase: "starting" },
  { match: "checkout", phase: "starting" },
  { match: "setup-python", phase: "starting" },
  { match: "install dependencies", phase: "installing" },
  { match: "refresh data.json", phase: "scanning" },
  { match: "commit", phase: "saving" },
];

async function gh(url) {
  const token = process.env.GH_DISPATCH_TOKEN;
  if (!token) throw new Error("GH_DISPATCH_TOKEN is not configured on the server");
  return fetch(url, {
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
    },
  });
}

function runSeconds(run) {
  const start = new Date(run.run_started_at || run.created_at).getTime();
  const end = new Date(run.updated_at).getTime();
  const secs = (end - start) / 1000;
  return Number.isFinite(secs) && secs > 0 ? secs : null;
}

module.exports = async (req, res) => {
  // Read-only, so GET is correct here — unlike /api/refresh-report, which
  // starts a real run and is POST-only for exactly that reason.
  if (req.method !== "GET") {
    res.status(405).json({ ok: false, error: "Use GET." });
    return;
  }

  // Short cache: the page polls every few seconds and several people may have
  // it open at once during a scan. Ten seconds keeps it live enough to feel
  // real-time while collapsing a burst of pollers into one GitHub call, which
  // matters because GitHub rate-limits by token, not by visitor.
  res.setHeader("Cache-Control", "public, s-maxage=10, stale-while-revalidate=20");

  try {
    const runsRes = await gh(
      `${API_ROOT}/actions/workflows/${WORKFLOW_FILE}/runs?per_page=${ETA_SAMPLE_RUNS + 5}`
    );
    if (!runsRes.ok) {
      const text = await runsRes.text();
      res.status(502).json({ ok: false, error: `GitHub API error: ${runsRes.status} ${text}` });
      return;
    }
    const runs = (await runsRes.json()).workflow_runs || [];
    if (!runs.length) {
      res.status(200).json({ ok: true, state: "idle", run: null });
      return;
    }

    // ETA from real history: successful, completed runs only. A failed run
    // that died in 6 seconds (as #27 did on a syntax error) would drag the
    // average down and promise a scan time nothing can deliver.
    const durations = runs
      .filter((r) => r.status === "completed" && r.conclusion === "success")
      .map(runSeconds)
      .filter((s) => s !== null)
      .slice(0, ETA_SAMPLE_RUNS);
    const etaSeconds = durations.length
      ? Math.round(durations.reduce((a, b) => a + b, 0) / durations.length)
      : ETA_FALLBACK_SECONDS;

    const latest = runs[0];
    const active = runs.find((r) => r.status === "in_progress" || r.status === "queued");
    const run = active || latest;

    const payload = {
      ok: true,
      state: active ? (active.status === "queued" ? "queued" : "running") : "idle",
      etaSeconds,
      etaFromRuns: durations.length,
      run: {
        id: run.id,
        status: run.status,
        conclusion: run.conclusion,
        startedAt: run.run_started_at || run.created_at,
        updatedAt: run.updated_at,
        url: run.html_url,
        elapsedSeconds: active
          ? Math.max(0, Math.round((Date.now() - new Date(run.run_started_at || run.created_at).getTime()) / 1000))
          : runSeconds(run),
      },
      phase: null,
    };

    // Per-step detail only while something is actually running — one extra
    // GitHub call per poll is worth it live, pointless once the run is over.
    if (active) {
      const jobsRes = await gh(`${API_ROOT}/actions/runs/${active.id}/jobs`);
      if (jobsRes.ok) {
        const jobs = (await jobsRes.json()).jobs || [];
        const job = jobs[0];
        if (job && Array.isArray(job.steps)) {
          const running = job.steps.find((s) => s.status === "in_progress");
          const current = running || [...job.steps].reverse().find((s) => s.status === "completed");
          const name = (current && current.name ? current.name : "").toLowerCase();
          const hit = STEP_PHASES.find((p) => name.includes(p.match));
          payload.phase = {
            key: hit ? hit.phase : "working",
            stepName: current ? current.name : null,
            stepNumber: current ? job.steps.indexOf(current) + 1 : null,
            stepTotal: job.steps.length,
          };
        }
      }
    }

    res.status(200).json(payload);
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message || "Unknown server error" });
  }
};
