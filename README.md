# Colab MCP (Enhanced Fork)

An MCP server for controlling Google Colab from any AI coding agent. This fork fixes critical bugs in the [official repo](https://github.com/googlecolab/colab-mcp) and adds features that were removed upstream.

## Why This Fork?

The official `googlecolab/colab-mcp` has two major issues:

1. **Invisible tools** ([#54](https://github.com/googlecolab/colab-mcp/discussions/54), [#67](https://github.com/googlecolab/colab-mcp/discussions/67)) — Only `open_colab_browser_connection` appears in most MCP clients. The 4 notebook tools (add_code_cell, execute_cell, etc.) are hidden until a browser connects, because the server relies on `notifications/tools/list_changed` which many clients don't support (OpenAI Codex, some Claude Code versions, Kiro IDE).

2. **No programmatic GPU control** — Google [removed](https://github.com/googlecolab/colab-mcp/discussions/41) the `--enable-runtime` feature entirely. You can't assign a GPU without manually clicking in the browser.

This fork fixes both. All 6 tools appear immediately, and you can assign T4/L4/A100 GPUs with a single tool call.

## What's Different

| Feature | Official | This Fork |
|---------|----------|-----------|
| Notebook tools visible at startup | No (needs browser + list_changed) | Yes (pre-registered, works with any client) |
| `change_runtime` tool (GPU control) | Removed | Working via OAuth |
| OAuth token caching | N/A | Yes (authorize once, cached forever) |
| Windows compatibility | Port 53919 blocked | Fixed (port 8085) |
| ColabClient initialization | N/A | Fixed (Prod() env argument) |

## Available Tools

| Tool | Requires Browser | Requires OAuth | Description |
|------|:---:|:---:|-------------|
| `change_runtime` | | Yes | Assign GPU: T4, L4, A100, or NONE |
| `open_colab_browser_connection` | Yes | | Connect to a Colab notebook in your browser |
| `add_code_cell` | Yes | | Add a code cell to the notebook |
| `add_text_cell` | Yes | | Add a markdown cell |
| `execute_cell` | Yes | | Run a cell |
| `update_cell` | Yes | | Edit an existing cell |

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
2. All 5 tools should appear immediately
3. Call `open_colab_browser_connection` — a Colab notebook opens in your browser
4. Use `add_code_cell`, `execute_cell`, etc. to control the notebook

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
> Connected. Available tools: add_code_cell, execute_cell, ...

Agent: add_code_cell(code="!nvidia-smi")
Agent: execute_cell(cellIndex=0)
> Tesla T4, 15GB memory...
```

---

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

- **`f70c00d`** Register all 5 notebook tools directly on the FastMCP server at startup (fixes invisible tools)
- **`cae498b`** Add `change_runtime` tool with OAuth for programmatic GPU assignment
- **`440e3bc`** Fix `ColabClient` initialization (missing `Prod()` env arg) + change OAuth port to 8085 for Windows
- **`e66ee69`** Match real Colab API signatures (language param, cellId, run_code_cell)

Google [does not accept external contributions](https://github.com/googlecolab/colab-mcp/blob/main/CONTRIBUTING.md) to the official repo, so these fixes live here.

---

## License

Apache 2.0 (same as upstream)
