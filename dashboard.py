#!/usr/bin/env python3
"""E2E Dashboard -- updates a Confluence page with a running history of E2E
test runs for a configurable list of repos.

The Confluence page is divided into two sections:
  1. Results (auto-generated, append-only history; newest on top)
  2. Configuration (manually editable; script reads this to know which repos to track)

Each refresh fetches the latest run per configured repo from the E2E Runner API.
A run is only appended to the history if:
  a) Its date (in DASHBOARD_TZ) matches today's date
  b) It isn't already in the history (deduped by run ID)
Repos whose latest run is not from today are quietly skipped for that refresh.

History is persisted to history.json, which is committed back to the repo by
the GitHub Action so it survives across runs.

Usage:
  python dashboard.py --init          Seed the Confluence page with default config + empty history
  python dashboard.py --run           Read config, fetch E2E data, append to history, update page
  python dashboard.py --dry-run       Fetch everything and print the HTML that would be written
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

ATLASSIAN_EMAIL = os.getenv("ATLASSIAN_EMAIL", "")
ATLASSIAN_TOKEN = os.getenv("ATLASSIAN_TOKEN", "")
ATLASSIAN_DOMAIN = os.getenv("ATLASSIAN_DOMAIN", "procoretech.atlassian.net")
CONFLUENCE_PAGE_ID = os.getenv("CONFLUENCE_PAGE_ID", "")
E2E_RUNNER_TOKEN = os.getenv("E2E_RUNNER_TOKEN", "")
CUTOFF_HOUR = int(os.getenv("CUTOFF_HOUR", "17"))
DASHBOARD_TZ = os.getenv("DASHBOARD_TZ", "Asia/Kolkata")
GITHUB_REPO_URL = os.getenv(
    "GITHUB_REPO_URL", "https://github.com/raghunandan-k/e2e-dashboard"
).rstrip("/")
GITHUB_WORKFLOW_FILE = os.getenv("GITHUB_WORKFLOW_FILE", "dashboard.yml")

HISTORY_FILE = Path(__file__).parent / "history.json"

E2E_API_BASE = "https://e2e-test-runner-service.us00.ops.procoretech.com"
CONF_API_BASE = f"https://{ATLASSIAN_DOMAIN}/wiki/api/v2"

DEFAULT_REPOS = [
    ("Documents Classic", "procore/documents-ui-service"),
    ("Document Viewer", "procore/document-viewer-ui-service"),
    ("PDFTS", "procore/pdf_template_service"),
    ("FAS/FUS", "procore/file-upload-service"),
    ("Document Management", "procore/doc-control-ui-service"),
    ("BIM Model Manager", "procore/bim-model-manager-service"),
    ("Coordination Issues", "procore/coordination-issues-ui-service"),
]

# Columns displayed in the results table, left to right. Change this list to
# reorder, add, or remove columns. All keys must exist in COLUMN_REGISTRY
# (defined below). The "date" column is special: consecutive rows with the
# same date are merged via rowspan -- place it wherever you want the merge.
COLUMNS: list[str] = [
    "date",
    "system",
    "branch",
    "status",
    "passed",
    "failed",
    "skipped",
    "pass_rate",
    "argo",
]

RESULTS_HEADING = "Latest E2E Results"
CONFIG_HEADING = "Configuration"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class RepoConfig:
    display_name: str
    project_slug: str


@dataclass
class RunSummary:
    display_name: str
    project_slug: str
    status: Optional[str]
    branch: Optional[str]
    created_at: Optional[str]
    passed: Optional[int]
    failed: Optional[int]
    skipped: Optional[int]
    total: Optional[int]
    workflow_url: Optional[str]
    run_id: Optional[str] = None
    observed_at: Optional[str] = None
    error: Optional[str] = None

    @property
    def pass_rate(self) -> Optional[float]:
        if self.passed is None or self.failed is None:
            return None
        executed = (self.passed or 0) + (self.failed or 0)
        if executed == 0:
            return None
        return 100.0 * self.passed / executed

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RunSummary":
        # Filter to known fields to tolerate older history files
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# E2E Runner API
# ---------------------------------------------------------------------------

def fetch_latest_run(repo: RepoConfig) -> RunSummary:
    """Fetch the most recent run for the given project-slug."""
    try:
        resp = requests.get(
            f"{E2E_API_BASE}/v1/e2e-tests/filters",
            params={"project-slug": repo.project_slug, "per-page": "1"},
            headers={"Authorization": f"Bearer {E2E_RUNNER_TOKEN}"},
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        items = payload.get("data") or []
        if not items:
            return RunSummary(
                display_name=repo.display_name,
                project_slug=repo.project_slug,
                status=None, branch=None, created_at=None,
                passed=None, failed=None, skipped=None, total=None,
                workflow_url=None,
                error="No runs found",
            )
        run = items[0]
        return RunSummary(
            display_name=repo.display_name,
            project_slug=repo.project_slug,
            status=run.get("status"),
            branch=run.get("tests_branch"),
            created_at=run.get("created_at"),
            passed=run.get("total_passed"),
            failed=run.get("total_failed"),
            skipped=run.get("total_skipped"),
            total=run.get("total_tests"),
            workflow_url=run.get("workflow_url"),
            run_id=run.get("id"),
        )
    except requests.HTTPError as e:
        code = e.response.status_code
        # 401/403 almost always means the E2E Runner bearer token has expired.
        # Tag it distinctly so cmd_run can detect "dead token" vs one-off errors
        # and fail the whole workflow with a loud, rotation-ready message.
        error_msg = (
            f"AUTH_FAILED (HTTP {code}) -- E2E Runner token likely expired"
            if code in (401, 403)
            else f"HTTP {code}"
        )
        return RunSummary(
            display_name=repo.display_name, project_slug=repo.project_slug,
            status=None, branch=None, created_at=None,
            passed=None, failed=None, skipped=None, total=None,
            workflow_url=None,
            error=error_msg,
        )
    except Exception as e:
        return RunSummary(
            display_name=repo.display_name, project_slug=repo.project_slug,
            status=None, branch=None, created_at=None,
            passed=None, failed=None, skipped=None, total=None,
            workflow_url=None,
            error=str(e)[:80],
        )


# ---------------------------------------------------------------------------
# Confluence API
# ---------------------------------------------------------------------------

def _conf_auth():
    return (ATLASSIAN_EMAIL, ATLASSIAN_TOKEN)


def get_page() -> dict:
    resp = requests.get(
        f"{CONF_API_BASE}/pages/{CONFLUENCE_PAGE_ID}",
        params={"body-format": "storage"},
        auth=_conf_auth(),
        headers={"Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def update_page(title: str, body_html: str, version: int) -> dict:
    resp = requests.put(
        f"{CONF_API_BASE}/pages/{CONFLUENCE_PAGE_ID}",
        auth=_conf_auth(),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json={
            "id": CONFLUENCE_PAGE_ID,
            "status": "current",
            "title": title,
            "body": {"representation": "storage", "value": body_html},
            "version": {"number": version},
        },
        timeout=30,
    )
    if not resp.ok:
        print(f"Confluence PUT failed: {resp.status_code}\n{resp.text}", file=sys.stderr)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Config table parsing
# ---------------------------------------------------------------------------

@dataclass
class TableLayout:
    """Width/layout hints Confluence Cloud persists on a <table> element when the
    user resizes it in the editor. We re-emit these verbatim on every refresh so
    manual resizes stick across script-driven updates.

    Attributes captured:
      - data-layout         "default" | "wide" | "full-width"
      - data-table-width    Explicit pixel width (set when user drags the edge)
      - colgroup_html       Raw <colgroup> with per-column <col> widths
    """
    data_layout: Optional[str] = None
    data_table_width: Optional[str] = None
    colgroup_html: Optional[str] = None


def _find_table_after_heading(body_html: str, heading_text: str):
    """Return the BeautifulSoup <table> node immediately following a given h1/h2/h3."""
    soup = BeautifulSoup(body_html or "", "html.parser")
    for h in soup.find_all(["h1", "h2", "h3"]):
        if h.get_text(strip=True).lower() == heading_text.lower():
            return h.find_next("table")
    return None


def extract_table_layout(body_html: str, heading_text: str) -> TableLayout:
    """Pull width hints off the existing table so we can preserve manual resizes."""
    table = _find_table_after_heading(body_html, heading_text)
    if table is None:
        return TableLayout()
    colgroup = table.find("colgroup")
    return TableLayout(
        data_layout=table.get("data-layout"),
        data_table_width=table.get("data-table-width"),
        colgroup_html=str(colgroup) if colgroup else None,
    )


def _render_table_open(layout: Optional[TableLayout]) -> str:
    if not layout:
        return "<table>"
    attrs: list[str] = []
    if layout.data_layout:
        attrs.append(f'data-layout="{layout.data_layout}"')
    if layout.data_table_width:
        attrs.append(f'data-table-width="{layout.data_table_width}"')
    return "<table" + (" " + " ".join(attrs) if attrs else "") + ">"


def parse_config_from_body(body_html: str) -> list[RepoConfig]:
    """Find the <h2>Configuration</h2> section and extract the table below it."""
    table = _find_table_after_heading(body_html, CONFIG_HEADING)
    if table is None:
        return []
    repos: list[RepoConfig] = []
    rows = table.find_all("tr")
    for row in rows:
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        name = cells[0].get_text(strip=True)
        slug = cells[1].get_text(strip=True)
        if not name or not slug:
            continue
        if name.lower() in ("display name", "name") and slug.lower() in ("project slug", "slug"):
            continue
        repos.append(RepoConfig(display_name=name, project_slug=slug))
    return repos


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def _fmt_datetime(iso: Optional[str]) -> str:
    if not iso:
        return "--"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%b %d, %Y %I:%M %p")
    except Exception:
        return iso[:16]


def _date_key(iso: Optional[str]) -> str:
    """Return a date label used as the grouping key for the merged Date column."""
    if not iso:
        return "--"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%b %d, %Y")
    except Exception:
        return iso[:10]


def _fmt_time(iso: Optional[str]) -> str:
    if not iso:
        return "--"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%I:%M %p")
    except Exception:
        return iso[11:16]


def _fmt_duration(ms: Optional[int]) -> str:
    if ms is None:
        return "--"
    total_sec = ms // 1000
    mins, secs = divmod(total_sec, 60)
    if mins >= 60:
        hrs, mins = divmod(mins, 60)
        return f"{hrs}h {mins}m"
    return f"{mins}m {secs}s"


def _status_inner(status: Optional[str]) -> str:
    if not status:
        return '<span style="color: #6B778C;">--</span>'
    s = status.lower()
    if s == "passed":
        colour, label = "Green", "PASSED"
    elif s == "failed":
        colour, label = "Red", "FAILED"
    elif s in ("in progress", "in-progress", "running"):
        colour, label = "Yellow", "IN PROGRESS"
    elif s == "cancelled":
        colour, label = "Grey", "CANCELLED"
    else:
        colour, label = "Grey", status.upper()
    return (
        f'<ac:structured-macro ac:name="status">'
        f'<ac:parameter ac:name="title">{label}</ac:parameter>'
        f'<ac:parameter ac:name="colour">{colour}</ac:parameter>'
        f'</ac:structured-macro>'
    )


def _text(value: Optional[str]) -> str:
    return value if value else "--"


def _num(n: Optional[int]) -> str:
    return str(n) if n is not None else "--"


def _rate(r: Optional[float]) -> str:
    return f"{r:.1f}%" if r is not None else "--"


def _link(url: Optional[str], label: str = "View") -> str:
    return f'<a href="{url}">{label}</a>' if url else "--"


# ---------------------------------------------------------------------------
# Column registry
# ---------------------------------------------------------------------------

@dataclass
class ColumnDef:
    header: str
    render: Callable[[RunSummary], str]  # returns the INNER HTML of the <td>


COLUMN_REGISTRY: dict[str, ColumnDef] = {
    # Grouping/identity
    "date":        ColumnDef("Date",        lambda r: _date_key(r.created_at)),
    "time":        ColumnDef("Time",        lambda r: _fmt_time(r.created_at)),
    "datetime":    ColumnDef("Latest Run",  lambda r: _fmt_datetime(r.created_at)),
    "system":      ColumnDef("System",      lambda r: f"<strong>{r.display_name}</strong>"),
    "slug":        ColumnDef("Project Slug", lambda r: _text(r.project_slug)),
    "branch":      ColumnDef("Branch",      lambda r: _text(r.branch)),
    # Results
    "status":      ColumnDef("Status",      lambda r: _status_inner(r.status)),
    "passed":      ColumnDef("Passed",      lambda r: _num(r.passed)),
    "failed":      ColumnDef("Failed",      lambda r: _num(r.failed)),
    "skipped":     ColumnDef("Skipped",     lambda r: _num(r.skipped)),
    "total":       ColumnDef("Total",       lambda r: _num(r.total)),
    "pass_rate":   ColumnDef("Pass Rate",   lambda r: _rate(r.pass_rate)),
    # Links
    "argo":        ColumnDef("Argo CD",     lambda r: _link(r.workflow_url)),
}


def build_results_table(
    runs: list[RunSummary],
    columns: Optional[list[str]] = None,
    layout: Optional[TableLayout] = None,
) -> str:
    cols = columns if columns is not None else COLUMNS
    col_defs = [COLUMN_REGISTRY[k] for k in cols]  # KeyError surfaces typos loudly

    open_tag = _render_table_open(layout)
    colgroup = layout.colgroup_html if layout and layout.colgroup_html else ""
    header = "<tr>" + "".join(f"<th>{c.header}</th>" for c in col_defs) + "</tr>"

    if not runs:
        empty = (
            f"<tr><td colspan='{len(cols)}'><em>No runs recorded yet. "
            f"The first scheduled refresh will populate this table.</em></td></tr>"
        )
        return open_tag + colgroup + header + empty + "</table>"

    # Pre-compute rowspan for the "date" column (if present).
    has_date = "date" in cols
    date_col_idx = cols.index("date") if has_date else -1
    rowspans: list[int] = [1] * len(runs)
    if has_date:
        date_keys = [_date_key(r.created_at) for r in runs]
        rowspans = [0] * len(runs)
        i = 0
        while i < len(runs):
            j = i
            while j + 1 < len(runs) and date_keys[j + 1] == date_keys[i]:
                j += 1
            rowspans[i] = (j - i) + 1
            i = j + 1

    rows = [header]
    for idx, r in enumerate(runs):
        if r.error:
            # Render the date cell (if it's in the column list), then a single
            # colspan'd error message for the remaining columns.
            error_cells: list[str] = []
            remaining = len(cols)
            if has_date and rowspans[idx] > 0:
                error_cells.append(
                    f'<td rowspan="{rowspans[idx]}">{_date_key(r.created_at)}</td>'
                )
            if has_date:
                remaining = len(cols) - 1  # date column already handled
            # System name before the error message
            if "system" in cols:
                error_cells.append(f"<td>{r.display_name}</td>")
                remaining -= 1
            error_cells.append(f"<td colspan='{max(remaining, 1)}'><em>Error: {r.error}</em></td>")
            rows.append("<tr>" + "".join(error_cells) + "</tr>")
            continue

        cells: list[str] = []
        for i, (key, cdef) in enumerate(zip(cols, col_defs)):
            if key == "date":
                if rowspans[idx] > 0:
                    cells.append(f'<td rowspan="{rowspans[idx]}">{cdef.render(r)}</td>')
                # else: merged into a previous row -- skip emitting a cell
            else:
                cells.append(f"<td>{cdef.render(r)}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")

    return open_tag + colgroup + "".join(rows) + "</table>"


def build_config_table(
    repos: list[RepoConfig],
    layout: Optional[TableLayout] = None,
) -> str:
    open_tag = _render_table_open(layout)
    colgroup = layout.colgroup_html if layout and layout.colgroup_html else ""
    header = "<tr><th>Display Name</th><th>Project Slug</th></tr>"
    rows = [header]
    for r in repos:
        rows.append(f"<tr><td>{r.display_name}</td><td>{r.project_slug}</td></tr>")
    return open_tag + colgroup + "".join(rows) + "</table>"


def build_run_now_panel() -> str:
    """Renders a 'Run Now' call-to-action that deep-links to the GitHub Actions
    page for the dashboard workflow. Confluence Cloud blocks JS/onclick, so the
    on-demand trigger is a 2-click flow: click the link here, then click
    'Run workflow' on GitHub."""
    workflow_url = f"{GITHUB_REPO_URL}/actions/workflows/{GITHUB_WORKFLOW_FILE}"
    return (
        '<ac:structured-macro ac:name="info">'
        '<ac:rich-text-body>'
        '<p>'
        '<strong>&#9654; Trigger an on-demand refresh:</strong> '
        f'<a href="{workflow_url}">Open the GitHub Actions page</a>, '
        "then click <strong>Run workflow</strong> (top right) and "
        "<strong>Run workflow</strong> again in the dropdown. "
        "The refresh usually completes within a minute."
        '</p>'
        '<p><em>Use this after editing the Configuration table below if you '
        "don't want to wait for the next scheduled 5 PM IST refresh.</em></p>"
        '</ac:rich-text-body>'
        '</ac:structured-macro>'
    )


def build_page_body(
    runs: list[RunSummary],
    repos: list[RepoConfig],
    previous_body_html: str = "",
) -> str:
    """Rebuild the Confluence page body.

    If ``previous_body_html`` is provided, any manual table width / column-width
    tweaks the user made in the editor are carried over to the new body. This
    keeps the page from snapping back to the default narrow layout on every
    auto-refresh.
    """
    results_layout = extract_table_layout(previous_body_html, RESULTS_HEADING)
    config_layout = extract_table_layout(previous_body_html, CONFIG_HEADING)

    now = datetime.now().astimezone().strftime("%b %d, %Y at %I:%M %p %Z")
    parts = [
        f"<h2>{RESULTS_HEADING}</h2>",
        build_results_table(runs, layout=results_layout),
        f"<p><em>Last updated: {now}</em></p>",
        "<hr/>",
        f"<h2>{CONFIG_HEADING}</h2>",
        "<p><em>Add or remove rows below to change which repos are tracked. "
        "Use the <code>project-slug</code> from the E2E Runner "
        "(format: <code>procore/repo-name</code>). "
        "Changes take effect on the next scheduled run.</em></p>",
        build_config_table(repos, layout=config_layout),
        build_run_now_panel(),
    ]
    return "".join(parts)


# ---------------------------------------------------------------------------
# History storage (JSON file on disk, committed back by GitHub Action)
# ---------------------------------------------------------------------------

def load_history() -> list[RunSummary]:
    if not HISTORY_FILE.exists():
        return []
    try:
        with open(HISTORY_FILE) as f:
            payload = json.load(f)
        raw = payload.get("runs", []) if isinstance(payload, dict) else payload
        return [RunSummary.from_dict(r) for r in raw if isinstance(r, dict)]
    except Exception as e:
        print(f"Warning: could not load history ({e}); starting fresh.", file=sys.stderr)
        return []


def save_history(runs: list[RunSummary]) -> None:
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "runs": [r.to_dict() for r in runs],
    }
    with open(HISTORY_FILE, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


def _tz():
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(DASHBOARD_TZ)
    except Exception:
        return None


def _today_date_str() -> str:
    tz = _tz()
    now = datetime.now(tz) if tz else datetime.now().astimezone()
    return now.strftime("%Y-%m-%d")


def _run_date_str(iso: Optional[str]) -> Optional[str]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        tz = _tz()
        dt_local = dt.astimezone(tz) if tz else dt.astimezone()
        return dt_local.strftime("%Y-%m-%d")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def validate_env() -> None:
    missing = []
    for name in ("ATLASSIAN_EMAIL", "ATLASSIAN_TOKEN", "CONFLUENCE_PAGE_ID", "E2E_RUNNER_TOKEN"):
        if not os.getenv(name):
            missing.append(name)
    if missing:
        print("Missing required env vars: " + ", ".join(missing), file=sys.stderr)
        print("Copy .env.example to .env and fill in the values.", file=sys.stderr)
        sys.exit(1)


def _next_version(page: dict) -> int:
    """Determine the correct version number to send on update.

    Confluence treats drafts specially: when publishing a draft for the first
    time the version must be 1. After that, it must strictly increment.
    """
    status = page.get("status")
    current = page.get("version", {}).get("number", 1)
    if status == "draft":
        return 1
    return current + 1


def cmd_init(dry_run: bool) -> None:
    validate_env()
    repos = [RepoConfig(n, s) for n, s in DEFAULT_REPOS]

    # Pull the existing page first so we can preserve any manual table widths
    # the user has set. On a brand-new page this just returns empty layouts,
    # which is the correct no-op behavior.
    page = None if dry_run else get_page()
    previous_body = (page.get("body", {}).get("storage", {}).get("value", "") if page else "")

    # Empty history on first init -- rows will be added by subsequent --run calls.
    body = build_page_body([], repos, previous_body_html=previous_body)
    if dry_run:
        print(body)
        return
    title = page.get("title") or "E2E Dashboard"
    version = _next_version(page)
    update_page(title, body, version)
    print(f"Seeded Confluence page {CONFLUENCE_PAGE_ID} with {len(repos)} default repos (version {version}).")


def cmd_run(dry_run: bool) -> None:
    validate_env()
    print(f"Fetching Confluence page {CONFLUENCE_PAGE_ID}...")
    page = get_page()
    body_html = page.get("body", {}).get("storage", {}).get("value", "")
    title = page.get("title") or "E2E Dashboard"

    repos = parse_config_from_body(body_html)
    if not repos:
        print("No config table found on page. Falling back to default repo list.")
        repos = [RepoConfig(n, s) for n, s in DEFAULT_REPOS]

    print(f"Tracking {len(repos)} repos:")
    for r in repos:
        print(f"  - {r.display_name} ({r.project_slug})")

    print("Fetching latest runs from E2E Runner...")
    today = _today_date_str()
    print(f"Today (TZ={DASHBOARD_TZ}): {today}")

    history = load_history()
    existing_ids = {r.run_id for r in history if r.run_id}

    added: list[RunSummary] = []
    skipped_not_today: list[RunSummary] = []
    skipped_duplicate: list[RunSummary] = []
    errors: list[RunSummary] = []

    observed_at = datetime.now(timezone.utc).isoformat()
    auth_failures = 0
    for repo in repos:
        fresh = fetch_latest_run(repo)
        if fresh.error:
            errors.append(fresh)
            if fresh.error.startswith("AUTH_FAILED"):
                auth_failures += 1
            continue
        run_date = _run_date_str(fresh.created_at)
        if run_date != today:
            skipped_not_today.append(fresh)
            continue
        if fresh.run_id and fresh.run_id in existing_ids:
            skipped_duplicate.append(fresh)
            continue
        fresh.observed_at = observed_at
        history.append(fresh)
        if fresh.run_id:
            existing_ids.add(fresh.run_id)
        added.append(fresh)

    # Short-circuit: if every repo returned a 401/403, the E2E Runner token
    # has almost certainly expired. Fail the workflow loudly so the default
    # GitHub "workflow failed" email lands in the user's inbox -- the fix is
    # to rotate E2E_RUNNER_TOKEN (see README: "Rotating the E2E Runner token").
    if auth_failures == len(repos) and len(repos) > 0:
        print(
            "\n!!! E2E_RUNNER_TOKEN appears to be expired or invalid !!!\n"
            f"All {len(repos)} repos returned 401/403 from the E2E Runner API.\n"
            "Rotate the token: https://e2e-test-runner.procore.com/ (grab a fresh\n"
            "bearer from the Network tab) and update the GitHub secret at\n"
            f"{GITHUB_REPO_URL}/settings/secrets/actions (E2E_RUNNER_TOKEN).\n"
            "Aborting without touching Confluence or history.json.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Sort history: newest first (by created_at; None goes last)
    history.sort(
        key=lambda r: r.created_at or "",
        reverse=True,
    )

    new_body = build_page_body(history, repos, previous_body_html=body_html)
    if dry_run:
        print("\n--- DRY RUN: would write the following HTML ---\n")
        print(new_body)
        return

    save_history(history)
    new_version = _next_version(page)
    update_page(title, new_body, new_version)
    print(f"\nUpdated Confluence page to version {new_version}.")
    print(f"History entries: {len(history)}")
    print(f"  Added today: {len(added)}")
    for r in added:
        print(f"    + {r.display_name}: {r.status} ({r.passed}P/{r.failed}F/{r.skipped}S)")
    if skipped_duplicate:
        print(f"  Skipped (already in history): {len(skipped_duplicate)}")
        for r in skipped_duplicate:
            print(f"    = {r.display_name} (run_id {r.run_id[:8] if r.run_id else '?'}...)")
    if skipped_not_today:
        print(f"  Skipped (latest run not today): {len(skipped_not_today)}")
        for r in skipped_not_today:
            print(f"    - {r.display_name} (latest was {_run_date_str(r.created_at)})")
    if errors:
        print(f"  Errors: {len(errors)}")
        for r in errors:
            print(f"    ! {r.display_name}: {r.error}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--init", action="store_true", help="Seed page with default config")
    group.add_argument("--run", action="store_true", help="Fetch and update results (default)")
    parser.add_argument("--dry-run", action="store_true", help="Print HTML without writing")
    args = parser.parse_args()

    if args.init:
        cmd_init(args.dry_run)
    else:
        cmd_run(args.dry_run)


if __name__ == "__main__":
    main()
