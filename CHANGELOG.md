# Changelog

All notable changes to this fork of [`googlecolab/colab-mcp`](https://github.com/googlecolab/colab-mcp) are documented here.

This fork follows the upstream `1.0.x` baseline and tags fork-specific work
with the date of the change. Upstream-merged work keeps its own commit
history.

## 2026-06-18 — Private Network Access (PNA) handshake — the REAL real fix

After the dual-stack bind fix (earlier today) the Colab tab still showed
"Disconnected from the local Colab MCP server" with a generic 60s timeout.
DevTools Network tab on the failing tab revealed the actual cause:

- The WebSocket request stayed at "Provisional headers are shown" with
  Connection Start → Stalled "2.3 days".
- Server log only saw `stream ends after 0 bytes` — Chrome was opening
  the TCP socket and then closing it before sending any HTTP bytes.

This is Chrome's [Private Network Access](https://developer.chrome.com/blog/private-network-access-preflight)
behavior. A public origin (`https://colab.research.google.com`) talking
to a local server (`ws://localhost`) is classified as a "private network
request" and Chrome blocks it unless the server confirms acceptance via
specific CORS headers — both on the CORS preflight AND on the final
WebSocket upgrade response.

The upstream `googlecolab/colab-mcp` does not send these headers, which
is why "Disconnected" hits every user who runs Chrome with default
security settings. The earlier theories (orphaned servers, stale tabs)
were correlated symptoms but not the cause.

### Fixed
- `_validate_authorization` (now also `process_request`) intercepts any
  non-WebSocket request (no `Upgrade: websocket` header) and responds
  204 No Content with:
  - `Access-Control-Allow-Origin: https://colab.research.google.com`
  - `Access-Control-Allow-Methods: GET, OPTIONS`
  - `Access-Control-Allow-Headers: authorization, content-type, sec-websocket-*`
  - `Access-Control-Allow-Private-Network: true`
  - `Access-Control-Allow-Credentials: true`
  - `Access-Control-Max-Age: 86400`
- New `_augment_handshake_response` (`process_response` callback) adds
  the same headers to the 101 Switching Protocols response. Chrome
  re-checks PNA on the upgrade response itself — preflight alone is not
  sufficient.

### Tests added
- `test_cors_preflight_responds_with_pna_headers` — sends a raw HTTP GET
  (no Upgrade) and asserts the 204 response carries PNA + CORS headers.
- `test_websocket_handshake_response_has_pna_header` — completes a real
  WebSocket handshake and asserts the 101 response carries the PNA
  header.

## 2026-06-18 — Root-cause fix for "Disconnected from the local Colab MCP server"

The real cause of [upstream #84](https://github.com/googlecolab/colab-mcp/discussions/84) was not stale servers or stale tabs — it was an **IPv4/IPv6 dual-stack bind bug** in the WebSocket server. With `host="localhost"` + `port=0`, the `websockets` library binds two sockets on **different ephemeral ports** (one IPv6 `::1:X`, one IPv4 `127.0.0.1:Y`), then we report only one of them. When Chrome resolves `ws://localhost:<reported>` to whichever family lost the lottery, it connects to a port with **no listener**, the TCP connection drops with "stream ends after 0 bytes", and the Colab tab shows "Disconnected from the local Colab MCP server" with no retry. The user then waits 60s for a generic timeout.

This was reproduced and root-caused by reading the server logs during a smoke E2E run: lines `server listening on [::1]:52319` and `server listening on 127.0.0.1:52320` for the SAME server instance.

### Fixed
- `ColabWebSocketServer.__init__` default host changed from `"localhost"` to
  `"127.0.0.1"`, forcing IPv4-only bind. Single socket, single port, no
  ambiguity. Chrome resolves `localhost` to `127.0.0.1` on Windows and most
  desktop OSes, so this is transparent for the Colab tab.
- `__aenter__` now validates that every bound socket shares the same port,
  raising `RuntimeError` with diagnostic detail if not. This is a hard
  guard against future regressions (e.g., someone changing `host` back to
  `"localhost"` or to a `(v4, v6)` tuple without picking a fixed port).
- Startup logging now lists every bound socket address (not just the first
  one) plus the URL the Colab tab will actually use.

### Tests added
- `test_single_socket_single_port` — fails if the server binds more than one
  port (the dual-stack regression).
- `test_default_host_is_ipv4` — fails if the default host is set to anything
  other than `"127.0.0.1"`.

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
