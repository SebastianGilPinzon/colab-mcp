# Changelog

All notable changes to this fork of [`googlecolab/colab-mcp`](https://github.com/googlecolab/colab-mcp) are documented here.

This fork follows the upstream `1.0.x` baseline and tags fork-specific work
with the date of the change. Upstream-merged work keeps its own commit
history.

## 2026-06-16 — Stale-tab dedup fix + better timeout diagnostics

Chrome dedupes browser tabs by URL canonical, ignoring the `#fragment`. That
meant calling `open_colab_browser_connection` a second time (after the first
server died) would silently reuse the old Colab tab whose fragment pointed at
the dead port, instead of opening a fresh tab for the live server. The old
tab showed "Disconnected from the local Colab MCP server" and the new server
timed out 60s later with no actionable feedback.

### Fixed
- `open_colab_browser_connection` now appends `?p=<port>` to the Colab URL as
  a query param. Because the port is ephemeral and changes per server
  instance, the URL is unique → Chrome cannot dedupe → a fresh tab opens
  every time. The `#fragment` remains the source of truth for the Colab
  browser-side code (token + port), so this is purely a Chrome-side fix.

### Changed
- The timeout error returned by `open_colab_browser_connection` now lists the
  three actual causes (stale tabs, Local Network Access denied, browser not
  opening) with specific remediation, instead of a generic "make sure you
  have a Colab notebook open" message.

### Docs
- README troubleshooting: new entries for **Chrome reused an old Colab tab**
  and **Chrome asks for "Permission to access other services"** (Local
  Network Access prompt), with explanations of why each is required.

## 2026-06-16 — Pre-register full 7-tool notebook surface

Closes the gap reported in [discussion #69](https://github.com/googlecolab/colab-mcp/discussions/69): the post-connection success message advertised seven notebook tools, but only four were pre-registered as stubs. `get_cells`, `delete_cell`, and `move_cell` were unreachable on clients that snapshot tools at startup (Claude Code, Codex). Without `get_cells` the bridge was effectively write-only — an agent could create cells but never read state back.

### Added
- Three new pre-registered tools matching the browser-side handler names:
  - `get_cells()` — read current notebook state (cells, IDs, contents, outputs).
  - `delete_cell(cellId)` — delete a cell by ID.
  - `move_cell(cellId, toIndex)` — move a cell to a new position.
- Matching stubs in `_make_injected_tools` (`session.py`) so all seven tools are
  visible at startup with `NOT_CONNECTED_MSG` until the browser connects.

### Changed (breaking)
- `execute_cell` → renamed to `run_code_cell` to match the browser-side tool
  name directly (no more wrapper). The old `execute_cell` no longer exists.
  Callers must migrate to `run_code_cell(cellId)`. The `cellIndex` fallback
  argument was removed — pass a `cellId` from `add_code_cell` or `get_cells`.

### Caveat
- Parameter signatures for `delete_cell` and `move_cell` are best-guess (the
  browser-side JS handlers are not in this repo). Signatures follow the
  established `cellId`-first pattern used by `update_cell` and `run_code_cell`.
  Adjust in review if the real browser handlers differ.

## 2026-05-14 — Stale-server detection and cleanup

Adds a process registry that solves the "Disconnected from the local Colab MCP server" issue ([upstream discussion #84](https://github.com/googlecolab/colab-mcp/discussions/84)) caused by orphaned servers from prior Claude Code sessions.

### Added
- `src/colab_mcp/process_registry.py` — cross-platform process registry (stdlib only).
  Tracks `{pid, port, host, started_at}` for each running server in:
  - `%LOCALAPPDATA%\colab-mcp\registry.json` on Windows
  - `~/.colab-mcp/registry.json` on macOS/Linux
- CLI flag `--list-running` — print every currently-running `colab-mcp` server.
- CLI flag `--kill-stale` — terminate orphaned `colab-mcp` servers and exit.
- Automatic registration on server startup (after the WebSocket port is bound)
  and unregistration on clean shutdown.
- Automatic pruning of dead registry entries on every startup.

### Changed
- `open_colab_browser_connection` timeout error now reports this server's port
  *and* the PIDs/ports of any peer `colab-mcp` processes, so users can
  immediately recognize the "old browser tab pointed at a dead port" case
  instead of staring at a generic "timed out" message.

### Docs
- README: new **CLI Reference** section, new troubleshooting entry, new row in
  "What's Different" table.
- Changes from Upstream: stale-server detection bullet added.

### Verification
Verified end-to-end on Windows 10 with `uv run` and `uvx`:
- Module loads under uv-managed venv (no import errors).
- `--list-running` correctly reports no servers when none registered.
- `--kill-stale` removes a synthetic registry entry whose PID is dead
  (`Terminated 1 stale colab-mcp server(s): pid=888888 port=55555`) and
  empties the registry file.
- Real `colab-mcp` server startup logs `Registered colab-mcp pid=X port=Y`.
- Clean shutdown removes the server's own entry (subsequent `--list-running`
  shows none).

## Earlier (upstream + previous fork commits)

See `git log` for the full history. Highlights:

- `f70c00d` — pre-register notebook tools at startup (fixes invisible tools in
  MCP clients that don't support `notifications/tools/list_changed`). Originally
  4 stubs; expanded to 7 in the 2026-06-16 entry above.
- `cae498b` — add `change_runtime` tool with OAuth for programmatic GPU
  assignment.
- `e66ee69` — match real Colab API signatures (language param, cellId,
  run_code_cell).
- `440e3bc` — fix `ColabClient` initialization (missing `Prod()` env arg) +
  change OAuth port to 8085 for Windows.
