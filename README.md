# Colab MCP (Enhanced Fork)

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](pyproject.toml)
[![MCP](https://img.shields.io/badge/protocol-MCP-purple.svg)](https://modelcontextprotocol.io)
[![Stars](https://img.shields.io/github/stars/SebastianGilPinzon/colab-mcp?style=social)](https://github.com/SebastianGilPinzon/colab-mcp)

An MCP server for controlling Google Colab from any AI coding agent. This fork fixes the bugs in the [official repo](https://github.com/googlecolab/colab-mcp) that block real day-to-day use and restores features Google removed upstream.

## Why This Fork?

Three concrete dolores that the official `googlecolab/colab-mcp` doesn't solve — and that this fork does:

1. **Invisible tools** ([#54](https://github.com/googlecolab/colab-mcp/discussions/54), [#67](https://github.com/googlecolab/colab-mcp/discussions/67), [#69](https://github.com/googlecolab/colab-mcp/discussions/69)) — only `open_colab_browser_connection` appears in most MCP clients (Claude Code, Codex, Kiro IDE). The notebook tools rely on `notifications/tools/list_changed`, which these clients ignore. Without `get_cells` in particular, the bridge is effectively write-only: an agent can add cells but can't read state back.
2. **"Disconnected from the local Colab MCP server"** ([#84](https://github.com/googlecolab/colab-mcp/discussions/84)) — orphaned servers from prior Claude Code sessions hold ports that your browser tab still points at. Reconnecting from the tab silently fails.
3. **No programmatic GPU control** — Google [removed](https://github.com/googlecolab/colab-mcp/discussions/41) the `--enable-runtime` feature entirely. You can't assign T4 / L4 / A100 without clicking in the browser.

This fork fixes all three. All 9 tools (1 connection + 7 notebook + 1 GPU control) appear immediately, stale servers are auto-detected and clean-uppable, and GPUs are assignable from a single tool call.

> _Demo coming soon: `docs/demo.gif` (TODO — short asciinema of `change_runtime` → `add_code_cell` → `run_code_cell`)._

## What's Different

| Feature | Official | This Fork |
|---------|----------|-----------|
| Notebook tools visible at startup | No (needs browser + list_changed) | Yes (pre-registered, works with any client) |
| `change_runtime` tool (GPU control) | Removed | Working via OAuth |
| OAuth token caching | N/A | Yes (authorize once, cached forever) |
| Windows compatibility | Port 53919 blocked | Fixed (port 8085) |
| ColabClient initialization | N/A | Fixed (Prod() env argument) |
| Stale-server detection / cleanup | None — silent "Disconnected" | `--list-running` + `--kill-stale`, registry pruning on startup |

## Available Tools

| Tool | Requires Browser | Requires OAuth | Description |
|------|:---:|:---:|-------------|
| `change_runtime` | | Yes | Assign GPU: T4, L4, A100, or NONE |
| `open_colab_browser_connection` | Yes | | Connect to a Colab notebook in your browser |
| `add_code_cell` | Yes | | Add a code cell to the notebook |
| `add_text_cell` | Yes | | Add a markdown cell |
| `get_cells` | Yes | | Read current notebook state (cells, IDs, contents, outputs) |
| `run_code_cell` | Yes | | Execute a code cell by `cellId` |
| `update_cell` | Yes | | Edit an existing cell by `cellId` |
| `delete_cell` | Yes | | Delete a cell by `cellId` |
| `move_cell` | Yes | | Move a cell to a new position by `cellId` |

> **Note:** `execute_cell` was renamed to `run_code_cell` in 2026-06-16 to match the browser-side handler name. Pass a `cellId` (from `add_code_cell` or `get_cells`) — the old `cellIndex` fallback was removed.

## Quick Start (Without OAuth)

If you just want the notebook tools (no `change_runtime`):

### 1. Install uv

```bash
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# Mac/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Important:** Do NOT use `pip install uv` — that version lacks required features.

### 2. Clone this repo

```bash
git clone https://github.com/SebastianGilPinzon/colab-mcp.git
```

### 3. Configure your MCP client

Add to your `.mcp.json` (Claude Code, Cursor, etc.):

```json
{
  "mcpServers": {
    "colab-proxy-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/colab-mcp", "colab-mcp"],
      "timeout": 30000
    }
  }
}
```

### 4. Use it

1. Restart your editor / reload window
2. All 8 tools should appear immediately (`open_colab_browser_connection` + 7 notebook tools)
3. Call `open_colab_browser_connection` — a Colab notebook opens in your browser
4. Use `add_code_cell`, `run_code_cell`, `get_cells`, etc. to control the notebook

---

## Full Setup (With OAuth + GPU Control)

This enables the `change_runtime` tool, which lets your agent assign GPUs without you touching the browser.

### 1. Create OAuth Credentials

You need a Google Cloud project with OAuth configured. This is a one-time setup (~5 minutes):

1. **Create a GCP project** (or use an existing one):
   ```bash
   gcloud projects create colab-mcp-oauth --name="Colab MCP OAuth"
   ```

2. **Configure OAuth consent screen:**
   - Go to [OAuth consent screen](https://console.cloud.google.com/apis/credentials/consent)
   - Select "External" > Create
   - App name: `Colab MCP`, add your email as support + developer contact
   - Save through all steps

3. **Add yourself as test user:**
   - On the consent screen page > "Test users" > Add your Google email

4. **Create OAuth client ID:**
   - Go to [Credentials](https://console.cloud.google.com/apis/credentials)
   - Create Credentials > OAuth client ID > Desktop app
   - Download the JSON file
   - Save it somewhere safe (e.g., `~/.config/colab-oauth.json`)

> **Note:** OAuth Client IDs can only be created via the Cloud Console web UI. There is no CLI or API for this.

### 2. Configure MCP with OAuth

```json
{
  "mcpServers": {
    "colab-proxy-mcp": {
      "command": "uv",
      "args": [
        "run", "--directory", "/path/to/colab-mcp",
        "colab-mcp",
        "--client-oauth-config", "/path/to/colab-oauth.json"
      ],
      "timeout": 30000
    }
  }
}
```

### 3. Authorize (first time only)

The first time the server starts, it opens your browser for Google OAuth consent. Sign in, click Allow, done. The token is cached at `~/.colab-mcp-auth-token.json` and auto-refreshes — you won't be asked again.

### 4. Use it

```
Agent: change_runtime(accelerator="T4")
> Runtime changed to T4. Endpoint: gpu-t4-s-xxx

Agent: open_colab_browser_connection()
> Connected. Available notebook tools: add_code_cell, add_text_cell, get_cells, run_code_cell, update_cell, delete_cell, move_cell

Agent: add_code_cell(code="!nvidia-smi")
> {"cellId": "abc123", ...}

Agent: run_code_cell(cellId="abc123")
> Tesla T4, 15GB memory...

Agent: get_cells()
> [{"cellId": "abc123", "code": "!nvidia-smi", "outputs": [...]}]
```

---

## CLI Reference

Once installed (via `uv run` or `uvx git+https://github.com/SebastianGilPinzon/colab-mcp`), the `colab-mcp` command supports these flags:

| Flag | Description |
|------|-------------|
| _(none)_ | Start the MCP server (default — reads/writes JSON-RPC on stdin/stdout) |
| `-l DIR`, `--log DIR` | Write logs to `DIR`. Defaults to a temp dir under `%TEMP%` / `$TMPDIR` |
| `-p`, `--enable-proxy` | Enable the runtime proxy that exposes browser-based notebook tools. On by default |
| `--client-oauth-config PATH` | Path to OAuth client-secrets JSON. Enables the `change_runtime` tool for programmatic GPU assignment |
| `--list-running` | Print every currently-running `colab-mcp` server (pid, port, host, start time) and exit. Useful when "Disconnected from the local Colab MCP server" appears |
| `--kill-stale` | Terminate every running `colab-mcp` server, clear its registry entry, and exit. Use this from a regular shell (NOT from inside Claude Code) before starting a fresh session |

The server maintains a tiny registry at `%LOCALAPPDATA%\colab-mcp\registry.json` (Windows) or `~/.colab-mcp/registry.json` (macOS/Linux). Each running instance writes a `{pid, port, host, started_at}` entry on startup and removes it on clean shutdown. Stale entries from crashed processes are pruned automatically the next time `colab-mcp` starts.

## Troubleshooting

### Tools don't appear after setup
- Make sure you're using this fork, not the official repo
- Only define `colab-proxy-mcp` in ONE `.mcp.json` file (not both global and project — dual definitions spawn two server instances and one dies silently)
- Restart your editor after changing `.mcp.json`

### `change_runtime` returns "Runtime API not initialized"
- Check that `--client-oauth-config` is in your `.mcp.json` args
- Check that the OAuth JSON file exists at the specified path
- Look at the server logs for the specific error:
  ```bash
  # Find the latest log
  ls -t $TMPDIR/colab-mcp-logs-*/colab-mcp.*.log | head -1 | xargs cat
  ```
- A healthy log shows: `INFO:Colab API client ready`
- If you see `WARNING:Failed to initialize Colab API client`, check the error message

### Windows: Port blocked error (WinError 10013)
Already fixed in this fork (changed to port 8085). If you still hit it, edit `src/colab_mcp/auth.py` and change `OAUTH_SERVER_PORT` to any open port.

### OAuth says "Access denied"
Add your Google email as a test user in Cloud Console > OAuth consent screen > Test users.

### Browser opens but connection times out
Make sure you have a Colab notebook open in the browser tab that opened. Click "Connect" if prompted.

### Chrome reused an old Colab tab pointing at a dead port

Chrome dedupes tabs by URL canonical (ignoring the `#fragment`), so when an old Colab tab is still open with a fragment pointing at a previous server's port, calling `open_colab_browser_connection` again may silently focus the old tab instead of opening a fresh one. The old tab shows "Disconnected from the local Colab MCP server" and the new server times out.

This fork mitigates that by appending the current port as a query param (`?p=<port>`) to the Colab URL, so each server instance produces a unique URL that Chrome can't dedupe. If you still hit it after upgrading:

1. Close every `colab.research.google.com` tab in your browser.
2. Retry `open_colab_browser_connection` — it will open a fresh tab pointed at the live server.

### Chrome asks for "Permission to access other services and apps on this device" (or Colab says "Disconnected")

When the Colab tab loads, Chrome shows a permission prompt:

> **colab.research.google.com wants — Permission to access other services and apps on this device**

**Click _Allow_.** If you block it, the WebSocket connection from the Colab tab to your local `colab-mcp` server is blocked, the tab shows "Disconnected from the local Colab MCP server", and `open_colab_browser_connection` will time out.

This prompt is Chrome's **Local Network Access** policy: a public site (`https://colab.research.google.com`) is asking to talk to a resource on your local network (`ws://localhost:<port>` where `colab-mcp` is listening). Chrome blocks this by default and asks the user. The "other service" in the prompt is **your own `colab-mcp` server running on your machine** — not external access. The connection is scoped to a one-time token in the URL fragment (`#mcpProxyToken=...`), so even on the same machine other processes can't piggy-back on it.

Chrome remembers the choice per-site, so you only need to allow it once for `colab.research.google.com`.

### "Disconnected from the local Colab MCP server" (orphaned servers)

If a Colab tab in your browser shows **"Disconnected from the local Colab MCP server"** and re-clicking *Connect* doesn't help, the cause is almost always one or more **orphaned colab-mcp processes** from previous Claude Code sessions. Each instance picks a random ephemeral port, but your Colab tab only remembers the port from the URL fragment used when it first opened — when that server dies (or you spawn a new Claude Code session with a new server on a different port), the tab keeps trying to reach a dead address.

This fork ships with built-in diagnostics. Run any of these from a **regular shell** (not from inside Claude Code, which is itself running an MCP instance):

```bash
# Show every colab-mcp server currently registered as running
uv run --directory /path/to/colab-mcp colab-mcp --list-running

# Terminate orphaned colab-mcp servers, then exit
uv run --directory /path/to/colab-mcp colab-mcp --kill-stale
```

The server writes a small registry file at `%LOCALAPPDATA%\colab-mcp\registry.json` (Windows) or `~/.colab-mcp/registry.json` (macOS/Linux) listing pid + port for each running instance. On every startup it prunes dead entries automatically, and on clean shutdown it removes its own. If `open_colab_browser_connection` times out from inside Claude Code, the new error message also includes the ports + pids of any peer servers so you can identify which one your browser tab is actually pointed at.

After cleaning up, re-run `open_colab_browser_connection` — it will open a fresh Colab tab pointed at the current (only) server's port + token.

Fixes [upstream issue #84](https://github.com/googlecolab/colab-mcp/discussions/84).

---

## Compatibility

Tested with:
- Claude Code (VS Code extension + CLI)
- Should work with any MCP client that supports the standard tool protocol (Cursor, Windsurf, Codex, etc.)

Supported platforms:
- Windows 10/11
- macOS
- Linux

---

## Changes from Upstream

This fork is based on [`googlecolab/colab-mcp`](https://github.com/googlecolab/colab-mcp) with these changes:

- **`f70c00d`** Register notebook tools directly on the FastMCP server at startup (fixes invisible tools)
- **`cae498b`** Add `change_runtime` tool with OAuth for programmatic GPU assignment
- **`440e3bc`** Fix `ColabClient` initialization (missing `Prod()` env arg) + change OAuth port to 8085 for Windows
- **`e66ee69`** Match real Colab API signatures (language param, cellId, run_code_cell)
- **stale-server detection** Process registry + `--list-running` / `--kill-stale` flags + clearer timeout diagnostics — fixes [upstream #84](https://github.com/googlecolab/colab-mcp/discussions/84) "Disconnected from the local Colab MCP server"
- **full 7-tool notebook surface** — pre-register `get_cells`, `delete_cell`, `move_cell` (previously missing) and rename `execute_cell` → `run_code_cell` to match the browser-side handler. Closes [upstream #69](https://github.com/googlecolab/colab-mcp/discussions/69).

Google [does not accept external contributions](https://github.com/googlecolab/colab-mcp/blob/main/CONTRIBUTING.md) to the official repo, so these fixes live here.

## Verified fixes (accepted in upstream discussions)

- **[#67 → answered](https://github.com/googlecolab/colab-mcp/discussions/67)** — invisible-tools fix (this fork's pre-registration approach was accepted by the upstream community as the working solution).
- **[#69](https://github.com/googlecolab/colab-mcp/discussions/69)** — follow-up on `get_cells` and the remaining missing stubs — addressed in this fork on 2026-06-16.
- **[#84](https://github.com/googlecolab/colab-mcp/discussions/84)** — "Disconnected from the local Colab MCP server" — addressed via the stale-server registry + `--kill-stale` CLI.

---

## License

Apache 2.0 (same as upstream)

---

⭐ **If this fork saved you time, a star helps others find it.**
