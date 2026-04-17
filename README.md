# E2E Dashboard

Automated Confluence dashboard that keeps a running history of E2E test runs for a configurable list of Procore repos.

## How it works

The dashboard lives on a single Confluence page with two sections:

1. **Latest E2E Results** (auto-generated, append-only history). Each scheduled refresh appends new rows on top; the Date column is merged across same-day rows for readability.
2. **Configuration** (manually editable) -- a table listing which repos to track. Anyone with edit access to the page can add/remove rows. The script reads this table before every update.

### Append rules

On every refresh the script fetches the latest E2E run per configured repo, then decides whether to add it to the history:

- ✅ **Added** if the run's date (in `DASHBOARD_TZ`) matches today's date AND the run ID isn't already in history.
- ⏭ **Skipped (stale)** if the repo's latest run is not from today. The repo is quietly absent from today's rows -- previous days' rows are kept intact.
- ⏭ **Skipped (duplicate)** if we've already recorded this exact run ID (can happen when the script runs twice on the same day).

History is persisted in `history.json`, committed back to the repo by the GitHub Action so it survives across runs. There is no retention cap -- the history grows indefinitely.

Data comes from the [E2E Test Runner API](https://e2e-test-runner-service.us00.ops.procoretech.com).

## Local setup

1. Create a Python virtualenv and install dependencies:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Create your `.env` file:

   ```bash
   cp .env.example .env
   ```

3. Fill in the values:
   - **`ATLASSIAN_EMAIL`** -- your Procore email
   - **`ATLASSIAN_TOKEN`** -- create at <https://id.atlassian.com/manage-profile/security/api-tokens>
   - **`CONFLUENCE_PAGE_ID`** -- from your Confluence page URL (`/pages/edit-v2/<ID>`)
   - **`E2E_RUNNER_TOKEN`** -- bearer token from the E2E Runner web app (inspect network tab)
   - **`DASHBOARD_TZ`** (optional) -- IANA timezone used to compute "today". Default: `Asia/Kolkata` (IST)
   - **`GITHUB_REPO_URL`** (optional) -- Repo that hosts the Action; used to render the "Run Now" link on the Confluence page. Default: `https://github.com/raghunandan-k/e2e-dashboard`

## Usage

**Seed the page with the default list of 7 repos** (only needed once):

```bash
python dashboard.py --init
```

**Refresh the results table** (what the GitHub Action runs daily):

```bash
python dashboard.py --run
```

**Preview without writing** (useful for debugging):

```bash
python dashboard.py --run --dry-run
```

## Customizing the result columns

The columns in the results table are driven by the `COLUMNS` list near the top of `dashboard.py`. Edit that list to reorder, add, or remove columns. Defaults:

```python
COLUMNS = ["date", "system", "branch", "status", "passed", "failed", "skipped", "pass_rate", "argo"]
```

**Available columns** (all defined in `COLUMN_REGISTRY`):

| Key | Header | Source |
|---|---|---|
| `date` | Date | Day portion of `created_at` -- consecutive rows with the same date are **merged** via rowspan |
| `time` | Time | Time portion of `created_at` |
| `datetime` | Latest Run | Full date + time (non-merged) |
| `system` | System | The display name from the config table |
| `slug` | Project Slug | The repo path (`procore/...`) |
| `branch` | Branch | `tests_branch` from the run |
| `status` | Status | `status` rendered as a colored Confluence badge |
| `passed` | Passed | `total_passed` |
| `failed` | Failed | `total_failed` |
| `skipped` | Skipped | `total_skipped` |
| `total` | Total | `total_tests` |
| `pass_rate` | Pass Rate | `passed / (passed + failed)` as a percentage |
| `argo` | Argo CD | Link to the Argo CD workflow (`workflow_url`) |

To **add a new column** (e.g., showing run duration), register it in `COLUMN_REGISTRY`:

```python
COLUMN_REGISTRY["duration"] = ColumnDef(
    "Duration",
    lambda r: _fmt_duration(getattr(r, "duration_ms", None)),
)
```

Then reference its key in `COLUMNS`. If the column reads a new field from the API, add that field to `RunSummary` and populate it in `fetch_latest_run`.

## Adding a new repo

1. Open the Confluence page in your browser
2. Scroll to the **Configuration** table
3. Add a new row with your display name and the `project-slug` (format: `procore/repo-name`)
4. Save the page
5. The next scheduled run will pick it up -- no code changes needed

To find the correct `project-slug` for a repo, query the E2E Runner:

```bash
curl -s "https://e2e-test-runner-service.us00.ops.procoretech.com/v1/e2e-tests/filters?system=<your-system>&per-page=1" \
  -H "Authorization: Bearer $E2E_RUNNER_TOKEN" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['data'][0]['project_slug'] if d.get('data') else 'none')"
```

## Daily scheduling via GitHub Actions

The workflow at `.github/workflows/dashboard.yml` runs the refresh every weekday at **5 PM IST** (11:30 UTC; IST has no DST so a single cron entry suffices). It also commits the updated `history.json` back to `main` so the history persists.

> ⚠️ **Self-hosted runner required.** The E2E Runner API (`e2e-test-runner-service.us00.ops.procoretech.com`) resolves to a `10.x.x.x` private LB behind Procore VPN. GitHub-hosted runners on Azure cannot reach it and will hang until the 10-minute workflow timeout. The workflow therefore targets `runs-on: [self-hosted, macOS, e2e-dashboard]` -- you need a VPN-connected Mac registered as a self-hosted runner (see setup below).

### On-demand refresh

The Confluence page renders a "Run Now" info panel below the Configuration table. Clicking it opens the workflow in GitHub Actions, where you can click **Run workflow** → **Run workflow** to trigger a refresh immediately (e.g., right after editing the Configuration table). No infrastructure required beyond this repo and Actions.

### Ways to initiate a refresh

All four require the self-hosted runner's Mac to be awake and connected to Procore VPN at the moment the job runs.

| Method | How |
|---|---|
| **Automatic (daily)** | Nothing to do. Cron fires at 5 PM IST weekdays; runner picks it up. |
| **"Run Now" link in Confluence** | Click the info panel below the Configuration table → **Run workflow** → **Run workflow**. |
| **GitHub web UI** | <https://github.com/raghunandan-k/e2e-dashboard/actions/workflows/dashboard.yml> → **Run workflow** → **Run workflow**. |
| **Terminal (`gh` CLI)** | `gh workflow run dashboard.yml --repo raghunandan-k/e2e-dashboard` |

Watch a run live from the terminal: `gh run watch --repo raghunandan-k/e2e-dashboard`.

### One-time setup

1. **Push this project to GitHub** (to [github.com/raghunandan-k/e2e-dashboard](https://github.com/raghunandan-k/e2e-dashboard)):

   ```bash
   cd /Users/aditya.karumuri/Projects/e2e-dashboard
   git init
   git add .
   git commit -m "Initial commit: Confluence E2E dashboard"
   git branch -M main
   git remote add origin https://github.com/raghunandan-k/e2e-dashboard.git
   git push -u origin main
   ```

   > ⚠️ Confirm `.env` is NOT in the commit (`.gitignore` excludes it, but double-check with `git status` before pushing).

2. **Add repository secrets** at <https://github.com/raghunandan-k/e2e-dashboard/settings/secrets/actions>:

   | Secret name | Value |
   |---|---|
   | `ATLASSIAN_EMAIL` | Your Procore email |
   | `ATLASSIAN_TOKEN` | Your Atlassian API token (same as in `.env`) |
   | `ATLASSIAN_DOMAIN` | `procoretech.atlassian.net` (optional; defaults to this) |
   | `CONFLUENCE_PAGE_ID` | `5346525200` (your page ID) |
   | `E2E_RUNNER_TOKEN` | Bearer token for the E2E Runner API |

3. **Add repository variables** (optional) at the same settings page under the **Variables** tab:

   | Variable name | Value |
   |---|---|
   | `DASHBOARD_TZ` | `Asia/Kolkata` (default if unset) |
   | `CUTOFF_HOUR` | `17` (default if unset) |
   | `DASHBOARD_REPO_URL` | `https://github.com/raghunandan-k/e2e-dashboard` (only needed if you fork/move the repo) |

4. **Enable Actions write permissions** at <https://github.com/raghunandan-k/e2e-dashboard/settings/actions>:
   - Scroll to **Workflow permissions**
   - Select **Read and write permissions** (needed so the workflow can commit `history.json` back to the repo)
   - Save

5. **Register a self-hosted runner** on a Procore-VPN-connected Mac. This is required because the E2E Runner API is VPN-gated (see the warning at the top of this section). The runner is a small daemon that polls GitHub for jobs and executes them on your Mac.

   ```bash
   # 1. Pull a single-use registration token (valid for 1 hour)
   gh api -X POST /repos/raghunandan-k/e2e-dashboard/actions/runners/registration-token --jq .token

   # 2. Download the runner (use arm64 for Apple Silicon, x64 for Intel)
   RUNNER_VERSION=$(gh api /repos/actions/runner/releases/latest --jq .tag_name | sed 's/^v//')
   mkdir -p ~/actions-runner-e2e-dashboard && cd ~/actions-runner-e2e-dashboard
   curl -sSL -o runner.tar.gz \
     "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-osx-arm64-${RUNNER_VERSION}.tar.gz"
   tar xzf runner.tar.gz && rm runner.tar.gz

   # 3. Register (substitute the token from step 1)
   ./config.sh \
     --url https://github.com/raghunandan-k/e2e-dashboard \
     --token <TOKEN_FROM_STEP_1> \
     --name "$(whoami)-mac-e2e-dashboard" \
     --labels "e2e-dashboard" \
     --unattended --replace

   # 4. Install + start as a launchd service (auto-starts at login)
   ./svc.sh install && ./svc.sh start && ./svc.sh status
   ```

   Verify from GitHub:

   ```bash
   gh api /repos/raghunandan-k/e2e-dashboard/actions/runners \
     --jq '.runners[] | "\(.name): \(.status)"'
   # expected: aditya-mac-e2e-dashboard: online
   ```

   Also required: create `/Users/runner` owned by your user (it's a hardcoded path used by some macOS Python installers; harmless to leave empty):

   ```bash
   sudo mkdir -p /Users/runner && sudo chown $(whoami):admin /Users/runner
   ```

6. **Run the workflow manually once to verify**: go to the **Actions** tab, pick **Refresh E2E Dashboard**, click **Run workflow**. If everything is configured correctly, it should update the Confluence page and push a `chore(history): ...` commit within ~45 seconds.

### Token maintenance

| Token | Expires | Where to refresh |
|---|---|---|
| `ATLASSIAN_TOKEN` | ~1 year | <https://id.atlassian.com/manage-profile/security/api-tokens> |
| `E2E_RUNNER_TOKEN` | Short-lived (hours to days, tied to SSO session) | see playbook below |

### Rotating the E2E Runner token

The bearer you grab from the browser is an opaque SSO session token. The workflow watches for this and **fails loud** (exit code 2) the moment it sees 401/403 from the E2E Runner API for every configured repo. When that happens GitHub sends you its standard "workflow failed" email. Do this:

1. Open <https://e2e-test-runner.procore.com/> in a logged-in browser tab
2. Open DevTools → Network tab → refresh the page → click any XHR request to `e2e-test-runner-service.us00.ops.procoretech.com` → copy the value of the `Authorization` header (everything after `Bearer `)
3. Go to <https://github.com/raghunandan-k/e2e-dashboard/settings/secrets/actions>
4. Click the edit pencil on `E2E_RUNNER_TOKEN` → paste the new token → **Update secret**
5. Trigger a manual run from the Actions tab to confirm the fix (or just wait for the next 5 PM IST cron)

The short-circuit logic is defensive: if the token is dead, the script does **not** touch Confluence or commit to `history.json`. Nothing gets corrupted -- you'll just see stale data on the page until you rotate.

### Long-term fix

The real fix is a service-account / long-lived API key from the E2E Runner team. Worth asking them whether that's available -- it would eliminate this rotation entirely.

## Self-hosted runner maintenance

### Useful commands

Run these from the runner directory (`~/actions-runner-e2e-dashboard`) unless noted otherwise.

| What you want to check / do | Command |
|---|---|
| Is the runner daemon alive locally? | `./svc.sh status` |
| Is GitHub seeing the runner as online? | `gh api /repos/raghunandan-k/e2e-dashboard/actions/runners --jq '.runners[] \| "\(.name): \(.status)"'` |
| Tail the runner's own logs (not workflow logs) | `tail -f _diag/Runner_*.log` |
| Restart after a Mac reboot if it didn't auto-start | `./svc.sh start` |
| Stop the runner temporarily (e.g., heavy local work) | `./svc.sh stop` |
| Unregister and remove (before wiping the Mac) | `./svc.sh uninstall && ./config.sh remove --token <NEW_REG_TOKEN>` |
| Upgrade to the latest runner version | `./svc.sh stop && ./config.sh remove --token <TOKEN> && <repeat setup step 5>` |
| See recent workflow runs | `gh run list --workflow dashboard.yml --repo raghunandan-k/e2e-dashboard --limit 10` |
| View logs for a specific run | `gh run view <RUN_ID> --log --repo raghunandan-k/e2e-dashboard` |

### Preventing Mac sleep during scheduled runs (optional)

If the Mac is asleep at 5 PM IST the cron-triggered job will sit in GitHub's queue until the Mac wakes up (then run normally). If you want the job to fire on time every weekday regardless, prevent sleep while on power:

```bash
# Don't sleep the system while on AC power; screen can still sleep.
sudo pmset -c sleep 0 disablesleep 1

# Revert:
sudo pmset -c sleep 0 disablesleep 0
```

On battery this setting is ignored. Alternatively, schedule a daily wake in System Settings → Battery → Schedule a few minutes before 5 PM IST.

### Where things live on the runner

| Thing | Path |
|---|---|
| Runner binary + config | `~/actions-runner-e2e-dashboard/` |
| Runner daemon logs | `~/actions-runner-e2e-dashboard/_diag/` |
| Per-run workspace (auto-cleaned) | `~/actions-runner-e2e-dashboard/_work/e2e-dashboard/e2e-dashboard/` |
| `launchd` plist | `~/Library/LaunchAgents/actions.runner.<repo>.<name>.plist` |
| Homebrew Python used by the workflow | `/opt/homebrew/bin/python3` |

### Fully removing the runner

```bash
cd ~/actions-runner-e2e-dashboard
./svc.sh stop && ./svc.sh uninstall

# Pull a fresh removal token (different endpoint from registration)
REMOVE_TOKEN=$(gh api -X POST /repos/raghunandan-k/e2e-dashboard/actions/runners/remove-token --jq .token)
./config.sh remove --token "$REMOVE_TOKEN"

cd ~ && rm -rf ~/actions-runner-e2e-dashboard
```

## Local scheduling (alternative)

If you'd rather schedule it on your own machine instead of GitHub Actions, you can use cron. Example (runs weekdays at 5 PM local time):

```cron
0 17 * * 1-5 cd /Users/you/Projects/e2e-dashboard && .venv/bin/python dashboard.py --run >> run.log 2>&1
```

Note: with the local cron approach, `history.json` is kept locally and never committed anywhere. If you switch machines, history is lost.

## Security

- `.env` is gitignored. Never commit tokens.
- The `E2E_RUNNER_TOKEN` expires periodically -- update it (locally and in GitHub secrets) when the script starts returning 401s.
- The Atlassian API token is long-lived but can be revoked in your Atlassian profile at any time.
- GitHub Actions secrets are encrypted at rest and only exposed to workflow runs on the repository they're set on.
