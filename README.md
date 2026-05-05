# timenotes-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server that exposes
the [Timenotes.io](https://timenotes.io) time-tracking API as **80 tools** an
LLM agent can call. Works with any MCP-compatible client — Claude Desktop,
Claude Code, [Hermes Agent](https://hermes-agent.nousresearch.com/), and
others.

> Timenotes does not publish an official API. This server was built by
> reverse-engineering the official Chrome extension and probing the live API
> against a real account. Endpoints, payload shapes, and quirks are documented
> as discovered. Some endpoints live on `v1`, others on `v2`; the client
> routes each call to the version that actually works today.

## What can the agent do?

| Capability | Examples of what an LLM can ask for |
| --- | --- |
| **Time tracking** | "Start a tracker on the *aquashop* project, *bug-fixes* task." "How much have I tracked this week?" |
| **Historic edits** | "Change yesterday's 3pm log to 90 minutes." "Move all logs marked *misc* to the *internal* project." |
| **Reports & exports** | "Export last month's report as PDF, grouped by client." |
| **Project / client / task / tag CRUD** | "Create a new client called Acme and a project under it." |
| **Team management** | "Invite alice@x.com as a member." "List all pending invitations." |
| **Vacation / absences** | "Submit a vacation request for next Monday." "Approve the request from Bob." |
| **Analytics** | "How many hours per client did I log this quarter?" |

See the [Tool catalog](#tool-catalog-80-tools) below for the full list.

## Install

```bash
git clone https://github.com/patelidom/timenotes-mcp.git
cd timenotes-mcp
uv venv --python 3.12      # or: python3.10+ -m venv .venv
uv pip install -e .         # or: .venv/bin/pip install -e .
```

Requires Python ≥ 3.10.

## Credentials

The server has **two ways** to obtain Timenotes credentials, depending on how
you're running it:

* **`http` transport (self-hosted, OAuth — recommended)**
  No local config needed. The first time you connect from Claude.ai, the
  server's login page opens, you sign in with your Timenotes email and
  password, and the resulting session token is stored encrypted in a
  Docker volume. See [Claude.ai (web) — self-hosted remote connector](#claudeai-web--self-hosted-remote-connector).

* **`stdio` transport (local Claude Desktop / Code / Hermes)**
  Pass credentials as environment variables in the MCP client config that
  launches the server (see snippets below). The server has no file-based
  credential storage.

| Variable | Purpose |
| --- | --- |
| `TIMENOTES_EMAIL` + `TIMENOTES_PASSWORD` | Auto-login at startup (stdio) |
| `TIMENOTES_TOKEN` | Pre-obtained access token (stdio, skips login) |
| `TIMENOTES_ACCOUNT_ID` | Pre-select a workspace (default: first) |
| `TIMENOTES_BASE_URL` | Override the API base (default `https://api.timenotes.io/v1`) |

If none are set in `stdio` mode, call the `timenotes_login` tool as the
first action.

## Wiring the server into a client

### Claude Desktop

`~/.config/Claude/claude_desktop_config.json` (Linux) or
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "timenotes": {
      "command": "/absolute/path/to/timenotes-mcp/.venv/bin/timenotes-mcp",
      "env": {
        "TIMENOTES_EMAIL": "you@example.com",
        "TIMENOTES_PASSWORD": "..."
      }
    }
  }
}
```

Restart Claude Desktop afterwards.

### Claude Code

```bash
claude mcp add timenotes \
  --env TIMENOTES_EMAIL=you@example.com \
  --env TIMENOTES_PASSWORD=... \
  -- /absolute/path/to/timenotes-mcp/.venv/bin/timenotes-mcp
```

### Hermes Agent

`~/.hermes/config.yaml`:

```yaml
mcp_servers:
  timenotes:
    command: "/absolute/path/to/timenotes-mcp/.venv/bin/timenotes-mcp"
    env:
      TIMENOTES_EMAIL: "you@example.com"
      TIMENOTES_PASSWORD: "..."
```

Then `hermes chat`.

### Claude.ai (web) — self-hosted remote connector

Claude.ai's web app supports custom MCP connectors over HTTPS with OAuth 2.0.
This repo ships a Dockerised HTTP transport that:

* runs the same 80 tools over Streamable HTTP at `/mcp`
* exposes a minimal OAuth 2.0 authorization server (Dynamic Client Registration,
  PKCE, authorization code flow)
* the OAuth login form **is** the Timenotes login — no separate password.
  Credentials are validated against `POST /sessions` on the Timenotes API,
  the resulting session token is encrypted at rest with a Fernet key
* survives container restarts via a SQLite + key file in a Docker volume

**1. Deploy with Docker Compose (Portainer Stack via git):**

```yaml
# docker-compose.yml — already in this repo
services:
  timenotes-mcp:
    build: .
    restart: unless-stopped
    environment:
      TIMENOTES_MCP_PUBLIC_URL: "https://timenotes-mcp.your-domain.example"
    volumes:
      - timenotes-mcp-data:/data
    ports:
      - "127.0.0.1:8000:8000"
volumes:
  timenotes-mcp-data:
```

In Portainer: *Stacks → Add stack → Repository*, point at this repo, set the
``TIMENOTES_MCP_PUBLIC_URL`` env var to your subdomain. Optional but
recommended: set ``TIMENOTES_OAUTH_SECRET`` to a fixed Fernet key (generate
with ``python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'``)
so the encrypted DB survives volume rebuilds.

**2. Set up a subdomain in Plesk:**

* Domains → Add subdomain (e.g. ``timenotes-mcp.your-domain.example``)
* Apache & nginx settings → enable **HTTP/2 + nginx as reverse proxy**
* SSL/TLS Certificates → Let's Encrypt → enable
* Hosting Settings → uncheck *Apache + Nginx* if you want pure nginx;
  add a *Reverse Proxy* entry pointing to ``http://127.0.0.1:8000``

**3. Add the connector in Claude.ai:**

Claude.ai → Settings → Connectors → Add custom connector → paste:

```
https://timenotes-mcp.your-domain.example/mcp
```

Claude redirects you to the login form; sign in with your Timenotes email
and password; control returns to Claude.ai which now has tool access. The
OAuth token is stored by Claude.ai per device, so the same connector works
on web, mobile, and desktop.

**Verify the deployment from a shell:**

```bash
curl https://timenotes-mcp.your-domain.example/healthz
curl https://timenotes-mcp.your-domain.example/.well-known/oauth-authorization-server
```

The repo also ships [`oauth_test.py`](oauth_test.py) which spawns the server
locally and runs the entire OAuth + MCP handshake using credentials from
the `TIMENOTES_EMAIL` / `TIMENOTES_PASSWORD` env vars.

## Verifying it works

The test scripts read your Timenotes credentials from `TIMENOTES_EMAIL` and
`TIMENOTES_PASSWORD` env vars. The cleanest pattern (no shell history of the
password):

```bash
read -p   "Email: "    TIMENOTES_EMAIL
read -s -p "Password: " TIMENOTES_PASSWORD; echo
export TIMENOTES_EMAIL TIMENOTES_PASSWORD

# Exercises every tool against the real API; cleans up after itself.
.venv/bin/python integration_test.py

# Real JSON-RPC handshake over stdio (the same wire protocol agents use).
.venv/bin/python stdio_test.py

# End-to-end OAuth + remote MCP handshake (spawns the http server locally).
.venv/bin/python oauth_test.py

unset TIMENOTES_PASSWORD
```

The integration test creates and immediately deletes its own client, project,
task, tag, time log, and tracker. It never modifies pre-existing data.
Cleanup is idempotent and runs even if the test crashes (`atexit`).

## Tool catalog (80 tools)

### Session & account
- `timenotes_login`, `timenotes_logout`, `timenotes_set_account`
- `timenotes_whoami`, `timenotes_list_accounts`

### Lookups
- `timenotes_list_projects`, `timenotes_get_project`
- `timenotes_list_tasks`, `timenotes_get_task`
- `timenotes_list_tags`
- `timenotes_list_clients`, `timenotes_get_client`
- `timenotes_list_members`

### Projects, tasks, clients, tags — full CRUD
- Projects: `_create_project`, `_update_project`, `_delete_project`
- Tasks: `_create_task`, `_update_task`, `_delete_task`, `_bookmark_task`, `_unbookmark_task`
- Clients: `_create_client`, `_update_client`, `_delete_client`
- Tags: `_create_tag`, `_update_tag`, `_delete_tag`

### Tracker (live timer)
- `timenotes_get_active_tracker`
- `timenotes_start_tracker`
- `timenotes_update_active_tracker` — change project / task / desc *while running*
- `timenotes_stop_tracker`

### Time logs (CRUD)
- `timenotes_list_time_logs`
- `timenotes_create_time_log`, `timenotes_update_time_log`, `timenotes_delete_time_log`

### Bulk time-log operations
- `timenotes_bulk_modify_time_logs`, `timenotes_bulk_remove_time_logs`,
  `timenotes_bulk_copy_time_logs`
- `timenotes_bulk_update_rates`, `timenotes_bulk_recalculate_rates`

### Reports & timesheets (read)
- `timenotes_report_detailed`, `timenotes_report_chart`
- `timenotes_report_export_columns`
- `timenotes_get_timesheet`

### File exports (csv / xlsx / pdf)
- `timenotes_export_report_detailed` — saves under `output_dir`
- `timenotes_export_timesheet` — saves under `output_dir`

### Aggregate analytics (computed locally)
- `timenotes_time_per_client`, `timenotes_time_per_project`,
  `timenotes_time_per_task`, `timenotes_time_per_day`

### Holidays / absences
- Requests: `_list_absence_requests`, `_create_absence_request`,
  `_update_absence_request`, `_delete_absence_request`
- Approval: `_approve_absence_request`, `_reject_absence_request`
- Read: `_list_absences`, `_list_absence_types`, `_list_free_days`

### Team / invitations / groups
- Invitations: `_list_invitations`, `_invite_member`, `_bulk_invite_members`,
  `_resend_invitation`, `_delete_invitation`
- Member groups: `_list_members_groups`, `_create_members_group`,
  `_update_members_group`, `_delete_members_group`

### Alerts / dashboard
- `timenotes_list_alerts`, `timenotes_update_alert`
- `timenotes_get_dashboard` — who is currently active, totals, top projects

### Integrations
- `timenotes_list_integrations`
- `timenotes_list_available_integrations`
- `timenotes_list_integration_accounts`

### Settings, plans, storage
- `timenotes_get_setting`, `timenotes_update_setting`
- `timenotes_list_plans`
- `timenotes_current_subscription_period`, `timenotes_list_subscription_periods`
- `timenotes_get_storage`

## API quirks (reference)

These are non-obvious things that bit me during reverse-engineering and may
matter if you extend the client:

- **Auth headers are non-standard:** `AuthorizationToken` (not Bearer) and
  `AccountId` (workspace context, required on every scoped call).
- **`AccountId` is the *workspace* id** (`users_account.account.id`), not the
  membership id (`users_account.id`).
- **v1 vs v2 split:** the client routes each call automatically. Roughly:
  v2 hosts everything modern (tasks, clients, holidays, invitations, alerts,
  reports, timesheets, settings, plans, dashboard, tracker writes, tag CRUD);
  v1 hosts time_logs, projects list, members, sessions, integrations.
- **Time-log create payload** uses `start_at` (`HH:MM`) + `date` (`YYYY-MM-DD`).
  The `started_at` field appears in responses but is always `null`.
- **Reports export body** wraps `columns` *inside* the `export` object:
  `{from, to, export: {type: "csv", columns: [...]}}`. Putting `columns` at
  the top level returns *"No columns selected"*.
- **`/clients/{id}` GET does not exist** — `get_client` filters the list
  internally.
- **`/timesheets/cell` is no longer reachable**; use `get_timesheet` for the
  full grid instead.

## Intentionally not implemented

- **Billing endpoints** (`/payment_method/*`, `POST /subscriptions`,
  `POST /billing_info`) — too risky for an autonomous agent to call.
- **`POST /imports`** — bulk-import via multipart upload; would need careful
  schema definition.
- **`POST /permissions/check`**, `DELETE /setting/remove_account_logo` — niche
  admin endpoints with low value for an agent.

PRs welcome if you need any of these.

## Contributing

Issues and PRs welcome. Before opening a PR:

```bash
.venv/bin/python integration_test.py    # must pass against your account
.venv/bin/python stdio_test.py          # MCP wire protocol smoke test
```

The integration test is the source of truth — if your change touches the
client, extend the test to cover it.

## License

[MIT](LICENSE).

## Disclaimer

Not affiliated with Timenotes.io. Use at your own risk; the upstream API is
undocumented and may change without warning.
