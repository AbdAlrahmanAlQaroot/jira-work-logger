import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import requests
import yaml
from mcp.server.fastmcp import FastMCP


def _valid_jira_time(value: str) -> bool:
    return bool(re.fullmatch(r"(\d+w\s*)?(\d+d\s*)?(\d+h\s*)?(\d+m\s*)?", value.strip()) and value.strip())

CONFIG_PATH = Path(__file__).parent / "config.yaml"

SIDES = ("BE", "FE", "BE-FE")
FRONTEND_SERVICES = ()

AI_AUTHORSHIP = re.compile(r"\bclaude\b|\banthropic\b|co-authored-by|generated (with|by) (ai|an ai|claude)", re.IGNORECASE)

mcp = FastMCP("jira-work-logger", instructions=(
    "Use this server to scan git repos for daily work and create/update Jira tickets. "
    "Typical flow: 1) call scan_daily_work to see what was done, "
    "2) call create_jira_ticket for each logical piece of work. "
    "Ticket title convention: [BE] service-name: description, or [FE] for frontend work (create_jira_ticket adds it). "
    "Failed calls return an error whose first line is 'jira-work-logger · <category> · retryable|not retryable', "
    "followed by Problem and Fix lines; follow the Fix line and only retry when it says retryable."
))


class ToolError(Exception):
    def __init__(self, category: str, retryable: bool, problem: str, fix: str):
        super().__init__(problem)
        self.category = category
        self.retryable = retryable
        self.problem = problem
        self.fix = fix

    def __str__(self):
        return "\n".join([
            f"jira-work-logger · {self.category} · {'retryable' if self.retryable else 'not retryable'}",
            f"Problem: {self.problem}",
            f"Fix: {self.fix}",
        ])


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        raise ToolError("config", False, f"could not read {CONFIG_PATH} ({type(e).__name__}).", "tell the user the jira-work-logger config is missing or broken.")


def jira_headers(config):
    return {
        "Authorization": f"Basic {_basic_auth(config)}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _basic_auth(config):
    import base64
    creds = f"{config['jira']['email']}:{config['jira']['api_token']}"
    return base64.b64encode(creds.encode()).decode()


def _jira_url(config, path):
    return f"{config['jira']['url'].rstrip('/')}/rest/api/3{path}"


def _jira(config, method, path, action, ok=(200, 201, 204), **kwargs):
    try:
        resp = requests.request(method, _jira_url(config, path), headers=jira_headers(config), timeout=30, **kwargs)
    except requests.RequestException as e:
        raise ToolError("network", True, f"could not reach Jira to {action} ({type(e).__name__}).", "retry once; if it fails again, tell the user Jira looks unreachable.")
    if resp.status_code not in ok:
        raise _jira_failure(resp, action)
    return resp


def _jira_detail(resp):
    try:
        body = resp.json()
        parts = list(body.get("errorMessages", [])) + [f"{k}: {v}" for k, v in body.get("errors", {}).items()]
        detail = "; ".join(parts)
    except ValueError:
        detail = resp.text
    return (detail or "no details").strip()[:300]


def _jira_failure(resp, action):
    status = resp.status_code
    detail = _jira_detail(resp)
    if status == 401:
        return ToolError("auth", False, f"Jira rejected the API token while trying to {action} (401).",
                         "do not retry. Ask the user to create a new API token at id.atlassian.com and put it in config.yaml next to server.py.")
    if status == 403:
        return ToolError("permission", False, f"this account may not {action} (403): {detail}", "do not retry; tell the user which permission is missing.")
    if status == 404:
        return ToolError("not_found", False, f"Jira found nothing to {action} (404): {detail}", "check the ticket key or project, then call again with the right one.")
    if status == 429 or status >= 500:
        return ToolError("transient", True, f"Jira failed to {action} ({status}): {detail}", "wait a few seconds and retry once.")
    return ToolError("validation", False, f"Jira refused to {action} ({status}): {detail}", "fix the fields Jira names, then call again.")


def _reject_ai_authorship(*texts):
    for text in texts:
        match = AI_AUTHORSHIP.search(text or "")
        if match:
            raise ToolError("validation", False, f"Jira text must not mention Claude/AI authorship; found \"{match.group(0)}\".",
                            "remove it from the title and description, then call again.")


def _get_my_account_id(config):
    return _jira(config, "GET", "/myself", "look up your account").json()["accountId"]


def _repo_name(repo_path):
    result = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo_path, capture_output=True, text=True, timeout=10)
    url = result.stdout.strip() if result.returncode == 0 else ""
    return url, (re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1]) if url else os.path.basename(repo_path))


def _work_repos(config):
    git = config["git"]
    candidates = []
    for root in git.get("repo_roots", []):
        root = os.path.expanduser(root)
        if os.path.isdir(root):
            candidates += [e.path for e in sorted(os.scandir(root), key=lambda e: e.name) if os.path.isdir(os.path.join(e.path, ".git"))]
    missing = []
    for repo in git.get("repos", []) or []:
        repo = os.path.expanduser(repo)
        if os.path.isdir(repo):
            candidates.append(repo)
        else:
            missing.append(repo)
    orgs = git.get("remote_orgs", [])
    repos = []
    for path in dict.fromkeys(candidates):
        try:
            url, name = _repo_name(path)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            missing.append(path)
            continue
        if orgs and not any(re.search(rf"[:/]{re.escape(org)}/", url) for org in orgs):
            continue
        repos.append((path, name))
    return repos, missing


def _get_commits(repo_path, author_email, date_str):
    """Get commits for a specific date from a repo."""
    since = f"{date_str}T00:00:00"
    until_date = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)
    until = until_date.strftime("%Y-%m-%dT00:00:00")

    result = subprocess.run(
        ["git", "log", f"--author={author_email}", f"--since={since}", f"--until={until}",
         "--all", "--pretty=format:%H|||%s|||%ai", "--no-merges"],
        cwd=repo_path, capture_output=True, text=True, timeout=20
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip().splitlines()[0] if result.stderr.strip() else f"git exited {result.returncode}")

    commits = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|||")
        if len(parts) == 3:
            commits.append({
                "hash": parts[0],
                "message": parts[1],
                "date": parts[2],
            })
    return commits


def _get_diff_stats(repo_path, commit_hash):
    """Get diff stats for a commit."""
    try:
        result = subprocess.run(
            ["git", "diff", "--stat", f"{commit_hash}~1", commit_hash],
            cwd=repo_path, capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


@mcp.tool()
def scan_daily_work(date: str = "") -> str:
    """List the user's own git commits (all branches, merges excluded) on one day across their work repos.

    Work repos are every git clone directly under the configured repo_roots whose origin is in
    a configured GitHub org, so personal repos never show up. Worktrees share their clone's history and
    are not scanned twice; a commit seen in two clones, or rewritten with the same message and author date, is
    listed once, under the repo name from the origin URL.
    Commits made on GitHub itself (web edits, squash merges) are not local; use `gh search commits` for those.

    Args:
        date: Day to scan, YYYY-MM-DD. Defaults to today (local time).

    Returns:
        Commits and diff stats grouped by repo, followed by any repos that could not be scanned.
    """
    config = load_config()
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise ToolError("validation", False, f"date '{date}' is not YYYY-MM-DD.", "call again with a date like 2026-10-08.")

    author_email = config["git"]["author_email"]
    repos, missing = _work_repos(config)
    if not repos:
        raise ToolError("config", False, "no work repos found under the configured repo_roots/repos.",
                        "tell the user to check the git section of config.yaml next to server.py.")

    seen = set()
    results = []
    failed = []
    for repo_path, service_name in repos:
        try:
            commits = []
            for c in _get_commits(repo_path, author_email, date):
                same = (service_name, c["message"], c["date"])
                if c["hash"] in seen or same in seen:
                    continue
                seen.update([c["hash"], same])
                commits.append(c)
        except (subprocess.TimeoutExpired, FileNotFoundError, RuntimeError) as e:
            failed.append(f"{service_name} ({repo_path}): {e}")
            continue
        if not commits:
            continue
        diff_stats = [_get_diff_stats(repo_path, c["hash"]) for c in commits]
        existing = next((r for r in results if r["service"] == service_name), None)
        if existing:
            existing["commits"] += commits
            existing["diff_stats"] += diff_stats
        else:
            results.append({"service": service_name, "repo_path": repo_path, "commits": commits, "diff_stats": diff_stats})

    output = [f"## Work summary for {date} ({len(repos)} repos scanned, author {author_email})\n"]
    if not results:
        output.append("No commits found.\n")
    for r in results:
        output.append(f"### {r['service']} ({len(r['commits'])} commits)")
        for i, c in enumerate(r["commits"]):
            output.append(f"- {c['message']}")
            if r["diff_stats"][i]:
                output.append(f"  ```\n  {r['diff_stats'][i]}\n  ```")
        output.append("")
    if failed or missing:
        output.append("### Not scanned (results may be incomplete)")
        output += [f"- could not scan {f}" for f in failed]
        output += [f"- configured repo not found: {m}" for m in missing]

    return "\n".join(output)


@mcp.tool()
def create_jira_ticket(
    service_name: str,
    title: str,
    summary: str,
    time_estimate: str = "3h",
    time_spent: str = "",
    task_type: str = "development",
    side: str = "",
) -> str:
    """Create a Jira ticket in the configured project (jira.project_key), assigned to the user, for work done on a service.

    The title is written as "[SIDE] service-name: title", e.g. "[BE] orders-service: ..." or "[FE] admin-web: ...".
    Pass only the description as title; the prefix and service name are added here.
    Title and summary must not mention Claude or AI authorship; such calls are refused.

    Args:
        service_name: Name of the service (e.g., 'orders-service').
        title: Short description of the work done.
        summary: What was done and why, in plain text.
        time_estimate: Time estimate in Jira format — e.g., '3h', '6h', '1d 4h', '3d'.
        time_spent: Time to log as spent — e.g., '2h', '1d' (optional).
        task_type: Task type, one of defaults.task_type_options when a task_type_field is configured. Defaults to 'development'.
        side: 'BE', 'FE' or 'BE-FE'. Leave empty to use FE for repos listed in defaults.frontend_services and BE otherwise.

    Returns:
        Created ticket key and URL. Errors start with 'jira-work-logger · <category> · retryable|not retryable'.
    """
    if not _valid_jira_time(time_estimate):
        raise ToolError("validation", False, f"time_estimate '{time_estimate}' is not a Jira duration.", "call again with e.g. '3h', '1d 4h' or '3d'.")
    if time_spent and not _valid_jira_time(time_spent):
        raise ToolError("validation", False, f"time_spent '{time_spent}' is not a Jira duration.", "call again with e.g. '2h' or '1d'.")

    _reject_ai_authorship(service_name, title, summary)

    config = load_config()
    defaults = config.get("defaults", {})
    task_field = defaults.get("task_type_field")
    task_type_map = {str(k).lower(): str(v) for k, v in (defaults.get("task_type_options") or {}).items()}
    if task_field and task_type.lower() not in task_type_map:
        raise ToolError("validation", False, f"task_type '{task_type}' is not allowed.", f"call again with one of {', '.join(task_type_map)}.")
    service = service_name.strip().lower()
    frontend = {s.lower() for s in config.get("defaults", {}).get("frontend_services", FRONTEND_SERVICES)}
    side = side.strip().upper() or ("FE" if service in frontend else "BE")
    if side not in SIDES:
        raise ToolError("validation", False, f"side '{side}' is not allowed.", f"call again with one of {', '.join(SIDES)}, or leave it empty.")
    prefix = re.match(rf"\s*\[[A-Z-]+\]\s*(?:{re.escape(service)}\s*:)?\s*", title, re.IGNORECASE)
    description = (title[prefix.end():] if prefix else title).strip()
    if not description:
        raise ToolError("validation", False, "title is empty after removing the prefix.", "call again with a short description of the work as title.")

    account_id = _get_my_account_id(config)

    full_title = f"[{side}] {service}: {description}"

    payload = {
        "fields": {
            "project": {"key": config["jira"]["project_key"]},
            "summary": full_title,
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": summary}]
                    }
                ]
            },
            "issuetype": {"name": config["defaults"]["issue_type"]},
            "assignee": {"accountId": account_id},
            "timetracking": {
                "originalEstimate": time_estimate,
            },
        }
    }
    if task_field:
        payload["fields"][task_field] = {"id": task_type_map[task_type.lower()]}

    resp = _jira(config, "POST", "/issue", "create the ticket", ok=(201, 400), json=payload)

    if resp.status_code == 400:
        try:
            field_errors = resp.json().get("errors", {})
        except ValueError:
            field_errors = {}
        unsettable = [
            f for f, msg in field_errors.items()
            if "not on the appropriate screen" in msg or "cannot be set" in msg
        ]
        if not unsettable:
            raise _jira_failure(resp, "create the ticket")
        for f in unsettable:
            payload["fields"].pop(f, None)
        resp = _jira(config, "POST", "/issue", "create the ticket", ok=(201,), json=payload)

    data = resp.json()
    key = data["key"]
    url = f"{config['jira']['url'].rstrip('/')}/browse/{key}"

    if time_spent:
        try:
            _jira(config, "POST", f"/issue/{key}/worklog", "log work", ok=(201,), json={"timeSpent": time_spent})
        except ToolError as e:
            e.problem = f"created {key} ({url}) but could not log work: {e.problem}"
            e.fix = f"do not create the ticket again. {e.fix} Use update_jira_ticket on {key} for the time."
            raise

    return f"Created ticket: {key}\nTitle: {full_title}\nEstimate: {time_estimate}\nSpent: {time_spent or '—'}\nType: {task_type}\nURL: {url}"


@mcp.tool()
def update_jira_ticket(
    ticket_key: str,
    summary: str = "",
    description: str = "",
    time_estimate: str = "",
    time_spent: str = "",
) -> str:
    """Update an existing Jira ticket's title, description or estimate, and/or log time spent on it.

    The description replaces the old one completely. Text must not mention Claude or AI authorship.

    Args:
        ticket_key: The Jira ticket key (e.g., 'PROJ-123').
        summary: New title/summary (optional).
        description: New description (optional).
        time_estimate: New time estimate in Jira format — e.g., '3h', '1d 4h', '3d' (optional).
        time_spent: Time to log as spent — e.g., '2h', '1d' (optional).

    Returns:
        Confirmation of the update. Errors start with 'jira-work-logger · <category> · retryable|not retryable'.
    """
    if not re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", ticket_key.strip()):
        raise ToolError("validation", False, f"'{ticket_key}' is not a ticket key.", "call again with a key like PROJ-123.")
    _reject_ai_authorship(summary, description)
    config = load_config()
    fields = {}

    if summary:
        fields["summary"] = summary

    if description:
        fields["description"] = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": description}]
                }
            ]
        }

    if time_estimate:
        if not _valid_jira_time(time_estimate):
            raise ToolError("validation", False, f"time_estimate '{time_estimate}' is not a Jira duration.", "call again with e.g. '3h', '1d 4h' or '3d'.")
        fields["timetracking"] = {"originalEstimate": time_estimate}

    if time_spent and not _valid_jira_time(time_spent):
        raise ToolError("validation", False, f"time_spent '{time_spent}' is not a Jira duration.", "call again with e.g. '2h' or '1d'.")

    if fields:
        _jira(config, "PUT", f"/issue/{ticket_key}", f"update {ticket_key}", ok=(200, 204), json={"fields": fields})

    if time_spent:
        try:
            _jira(config, "POST", f"/issue/{ticket_key}/worklog", f"log work on {ticket_key}", ok=(201,), json={"timeSpent": time_spent})
        except ToolError as e:
            if fields:
                e.problem = f"updated {ticket_key} but could not log work: {e.problem}"
                e.fix = f"do not repeat the field update. {e.fix} Call again with only time_spent."
            raise

    url = f"{config['jira']['url'].rstrip('/')}/browse/{ticket_key}"
    return f"Updated ticket: {ticket_key}\nURL: {url}"


@mcp.tool()
def get_my_tickets(status: str = "In Progress") -> str:
    """List up to 20 Jira tickets assigned to the user with one status, most recently updated first.

    Args:
        status: Exact Jira status name, e.g. 'To Do', 'In Progress', 'In Review', 'Done'. Defaults to 'In Progress'.

    Returns:
        Matching tickets with estimate and time spent. Errors start with 'jira-work-logger · <category> · retryable|not retryable'.
    """
    config = load_config()
    jql = f"assignee = currentUser() AND status = \"{status}\" ORDER BY updated DESC"

    resp = _jira(config, "GET", "/search/jql", f"list '{status}' tickets", ok=(200,),
                 params={"jql": jql, "maxResults": 20, "fields": "summary,status,timetracking"})

    issues = resp.json().get("issues", [])
    if not issues:
        return f"No tickets found with status '{status}'."

    output = [f"## My tickets ({status})\n"]
    for issue in issues:
        key = issue["key"]
        summary = issue["fields"]["summary"]
        tracking = issue["fields"].get("timetracking", {})
        estimate = tracking.get("originalEstimate", "—")
        spent = tracking.get("timeSpent", "—")
        output.append(f"- **{key}**: {summary} (estimate: {estimate}, spent: {spent})")

    return "\n".join(output)


if __name__ == "__main__":
    mcp.run()
