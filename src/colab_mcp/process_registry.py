# Copyright 2026 Sebastian Gil (fork).
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Process registry for colab-mcp servers.

Tracks running colab-mcp instances in a small JSON file so that:

1. A new server can detect & clean up stale ones from prior Claude Code sessions
   (fixes the "Disconnected from local Colab MCP server" symptom when the
   browser is still pointed at a dead port from a previous run).

2. Users can list running servers (--list-running) and kill stale ones
   (--kill-stale) for debugging.

3. On clean shutdown, the server removes its own entry.

The registry file lives at:
    Windows: %LOCALAPPDATA%\\colab-mcp\\registry.json
    macOS/Linux: ~/.colab-mcp/registry.json
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional


logger = logging.getLogger(__name__)


@dataclass
class ServerEntry:
    pid: int
    port: int
    started_at: float  # epoch seconds
    host: str = "localhost"


def _registry_dir() -> Path:
    """Cross-platform location for the registry file."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "colab-mcp"
    return Path(os.path.expanduser("~")) / ".colab-mcp"


def _registry_path() -> Path:
    return _registry_dir() / "registry.json"


def _load_registry() -> List[ServerEntry]:
    p = _registry_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        return [ServerEntry(**e) for e in data.get("servers", [])]
    except (json.JSONDecodeError, TypeError, KeyError) as exc:
        logger.warning(f"Registry at {p} is corrupt ({exc}); ignoring.")
        return []


def _save_registry(entries: List[ServerEntry]) -> None:
    d = _registry_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = _registry_path()
    payload = {"servers": [asdict(e) for e in entries]}
    p.write_text(json.dumps(payload, indent=2))


def _is_process_alive(pid: int) -> bool:
    """Cross-platform PID liveness check using stdlib only."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # On Windows, os.kill(pid, 0) raises if process doesn't exist or we
        # lack permission. We treat both as "not alive for our purposes".
        try:
            os.kill(pid, 0)
            return True
        except (OSError, PermissionError):
            return False
    else:
        # POSIX: signal 0 just checks existence + permission. We treat
        # PermissionError as "exists but not ours", which is still alive.
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True


def _kill_process(pid: int, *, force: bool = False) -> bool:
    """Best-effort terminate a stale colab-mcp process.

    Returns True if we believe the process is now gone.
    """
    if not _is_process_alive(pid):
        return True
    try:
        if sys.platform == "win32":
            # SIGTERM is mapped to TerminateProcess on Windows
            os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM if not force else signal.SIGKILL)
    except OSError as exc:
        logger.warning(f"Failed to signal pid={pid}: {exc}")
        return False
    # Give the process up to 3s to exit
    for _ in range(30):
        if not _is_process_alive(pid):
            return True
        time.sleep(0.1)
    if not force:
        return _kill_process(pid, force=True)
    return not _is_process_alive(pid)


def cleanup_stale(*, kill: bool = True) -> List[ServerEntry]:
    """Prune dead entries from the registry. If kill=True, also terminate any
    still-alive entries (used by --kill-stale, NOT by normal startup).

    Returns the list of entries that were removed.
    """
    entries = _load_registry()
    removed: List[ServerEntry] = []
    alive: List[ServerEntry] = []
    for e in entries:
        if not _is_process_alive(e.pid):
            removed.append(e)
            continue
        if kill:
            logger.info(f"Killing stale colab-mcp pid={e.pid} port={e.port}")
            if _kill_process(e.pid):
                removed.append(e)
                continue
        alive.append(e)
    _save_registry(alive)
    return removed


def prune_dead() -> int:
    """Remove only dead entries (don't touch alive ones).

    Returns the number of dead entries removed. Safe to call on startup.
    """
    entries = _load_registry()
    alive = [e for e in entries if _is_process_alive(e.pid)]
    dead_count = len(entries) - len(alive)
    if dead_count > 0:
        _save_registry(alive)
        logger.info(f"Pruned {dead_count} dead colab-mcp entries from registry")
    return dead_count


def register(port: int, host: str = "localhost") -> ServerEntry:
    """Add the current process to the registry. Prunes dead entries first."""
    prune_dead()
    entry = ServerEntry(
        pid=os.getpid(),
        port=port,
        started_at=time.time(),
        host=host,
    )
    entries = _load_registry()
    entries = [e for e in entries if e.pid != entry.pid] + [entry]
    _save_registry(entries)
    return entry


def unregister(pid: Optional[int] = None) -> None:
    """Remove an entry. Defaults to current process."""
    if pid is None:
        pid = os.getpid()
    entries = _load_registry()
    entries = [e for e in entries if e.pid != pid]
    _save_registry(entries)


def list_running() -> List[ServerEntry]:
    """Return all currently-registered (and still-alive) servers."""
    return [e for e in _load_registry() if _is_process_alive(e.pid)]
