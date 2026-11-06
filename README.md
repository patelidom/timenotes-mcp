# timenotes-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server for the
official [Timenotes Platform API](https://docs.timenotes.io). It gives an LLM
agent 23 tools: dedicated ones for daily time tracking and reporting, plus
`timenotes_api`, which reaches every other documented endpoint. Works with
Claude.ai (remote connector), Claude Desktop, Claude Code,
[Hermes Agent](https://hermes-agent.nousresearch.com/) and any other MCP client.

## Install

```bash
git clone https://github.com/patelidom/timenotes-mcp.git
cd timenotes-mcp
uv venv --python 3.12 && uv pip install -e ".[http]"
```

Requires Python ≥ 3.10 and `mcp` 2.x.

## Credentials

Everything runs on one **API token**: Timenotes → *Settings → Profile*. The
token is scoped to one workspace.

| Variable | Purpose |
| --- | --- |
| `TIMENOTES_API_TOKEN` | API token. Required for stdio; optional for http (seeds the OAuth store) |
| `TIMENOTES_TIMEZONE` | `TimeZone` header + "now" for `timenotes_log_time` (default `Europe/Prague`) |
| `TIMENOTES_BASE_URL` | Override the API base (default `https://platform.timenotes.io/v2`) |

## Local clients (stdio)

```bash
claude mcp add timenotes --env TIMENOTES_API_TOKEN=... -- /abs/path/.venv/bin/timenotes-mcp
```

Claude Desktop / Hermes: same command, with `TIMENOTES_API_TOKEN` in the
server's `env`.

## Claude.ai — self-hosted remote connector

The `http` transport serves Streamable HTTP at `/mcp` behind a minimal OAuth
2.0 server (Dynamic Client Registration, PKCE, refresh-token rotation). The
OAuth login form asks for the Timenotes API token, validates it against the
API and stores it encrypted (Fernet) in a SQLite file in the `/data` volume.
The server is single-owner: the first account to authorize (or the one behind
`TIMENOTES_API_TOKEN`) owns it, and tokens of any other Timenotes account are
rejected. Everyone else should run their own instance.

1. **Deploy** with the bundled `docker-compose.yml` (Portainer: *Stacks → Add
   stack → Repository*). Set `TIMENOTES_MCP_PUBLIC_URL`; optionally
   `TIMENOTES_OAUTH_SECRET` (fixed Fernet key) and `TIMENOTES_API_TOKEN`
   (pre-seeds the token so connected clients keep working without re-auth).
2. **Reverse proxy** the public HTTPS subdomain to `http://127.0.0.1:8765`
   (Plesk: subdomain + Let's Encrypt + nginx proxy rule).
3. **Connect**: Claude.ai → Settings → Connectors → Add custom connector →
   `https://<your-subdomain>/mcp`, then paste the API token into the form.

Check it: `curl https://<your-subdomain>/healthz`.

## Tools

| Area | Tools |
| --- | --- |
| Who / lookups | `timenotes_whoami`, `_list_projects`, `_list_tasks` (with `search`), `_list_clients`, `_list_tags`, `_list_members` |
| Time logs | `timenotes_log_time` (by name, creates missing task), `_create_time_log`, `_update_time_log`, `_delete_time_log`, `_list_time_logs` |
| Tracker | `timenotes_get_active_tracker`, `_start_tracker`, `_update_active_tracker`, `_stop_tracker` |
| Reports | `timenotes_report_detailed` (filters + grouping), `_time_per_client`, `_time_per_project`, `_time_per_task`, `_time_per_day` |
| Tasks | `timenotes_create_task`, `_update_task` |
| Everything else | `timenotes_api(method, path, query, body)` — project/client/tag CRUD, absences, approvals, comments, saved reports, members groups… |

## API notes

- Auth header `ApiToken`; optional `TimeZone`. Rate limit ~20 requests / 10 s
  — the client retries 429s using `Retry-After`.
- `start_at` in responses ends with `Z` but is local wall-clock time.
- `/time_logs` filters only by date. `/reports/detailed` also filters by
  project / client / member, but only by `hash_id`; the report tools convert
  UUIDs for you. `grouping[primary]` gives per-group totals server-side.
- Lists max out at 200 per page; archived projects/tasks are not listed.

## Tests

```bash
.venv/bin/python tools_test.py                         # offline, no token
TIMENOTES_API_TOKEN=... .venv/bin/python stdio_test.py # JSON-RPC over stdio, live
TIMENOTES_API_TOKEN=... .venv/bin/python oauth_test.py # full OAuth + MCP over HTTP, live
```

## License

[MIT](LICENSE). Not affiliated with Timenotes.io.
