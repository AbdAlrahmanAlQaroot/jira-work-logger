# jira-work-logger

An MCP server that turns your git commits into Jira tickets. Point a coding agent such as Claude Code at it and ask "log my work for today": it scans your work repos for your commits on that day, groups them, and creates or updates Jira tickets with estimates and time spent.

## Tools

- `scan_daily_work(date)`: your commits on one day across all work repos, all branches, merges excluded. Worktrees and rewritten commits are listed once.
- `create_jira_ticket(service_name, title, summary, time_estimate, time_spent, task_type, side)`: creates a ticket assigned to you, titled `[BE] service-name: title` (or `[FE]` / `[BE-FE]`).
- `update_jira_ticket(ticket_key, summary, description, time_estimate, time_spent)`: edits a ticket and logs time.
- `get_my_tickets(status)`: your 20 most recently updated tickets in one status.

## Errors an agent can act on

Every failure starts with one line the agent can parse, then says what went wrong and what to do:

```
jira-work-logger · auth · not retryable
Problem: Jira rejected the API token while trying to create the ticket (401).
Fix: do not retry. Ask the user to create a new API token at id.atlassian.com and put it in config.yaml next to server.py.
```

Categories are `config`, `auth`, `permission`, `validation`, `network` and `transient`, and the first line always says whether a retry makes sense. Durations are checked before anything is sent to Jira, and titles or descriptions that mention AI authorship are refused.

## Which repos count as work

Every git clone directly under `git.repo_roots` whose origin belongs to one of `git.remote_orgs`, plus any paths in `git.repos`. Personal repos in the same folder are skipped.

## Setup

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Fill in `config.yaml`:

- `jira.url`, `jira.email`, `jira.api_token` (create a token at https://id.atlassian.com/manage-profile/security/api-tokens), `jira.project_key`
- `git.author_email`: the email on your commits
- `git.repo_roots` and `git.remote_orgs`: where your work clones live and which GitHub orgs count as work
- `defaults.issue_type`: usually `Task`
- `defaults.frontend_services`: repo names that get `[FE]` instead of `[BE]`
- `defaults.task_type_field` and `defaults.task_type_options`: optional, for a Jira select field such as "Task type". Example: `customfield_10100` with `{development: "10200", maintenance: "10201"}`

Register it with Claude Code:

```
claude mcp add jira-work-logger -- /path/to/jira-work-logger/.venv/bin/python /path/to/jira-work-logger/server.py
```

`config.yaml` is in `.gitignore`; keep your token out of the repo.

## License

MIT
