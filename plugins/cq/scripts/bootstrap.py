#!/usr/bin/env python3
"""Bootstrap the cq MCP server for the Claude plugin.

Ensures the cq binary is available at the shared runtime cache path,
then replaces this process with `cq mcp` so Claude talks directly to
the Go MCP server over stdio.

The binary fetch, version, and cache logic live in the sibling
`cq_binary.py` module; this script is a thin Claude-facing launcher.
Claude runs it via `python ${CLAUDE_PLUGIN_ROOT}/scripts/bootstrap.py`,
which puts the script's directory on `sys.path`, so the bare
`import cq_binary` resolves to the sibling file.
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

import cq_binary

REMOTE_BOOTSTRAP_JSON_URL = f"https://raw.githubusercontent.com/{cq_binary.REPO}/main/plugins/cq/scripts/bootstrap.json"


def _load_remote_min_version() -> str:
    """Best-effort fetch of minimum CLI version from the repository metadata."""
    try:
        with urllib.request.urlopen(REMOTE_BOOTSTRAP_JSON_URL, timeout=3) as response:
            raw = response.read().decode("utf-8")
        config = json.loads(raw)
    except Exception:
        return ""

    value = config.get("cli_min_version", "")
    return value if isinstance(value, str) else ""


def _resolve_min_version(metadata_path: Path) -> str:
    """Resolve minimum version, preferring a newer remotely published value."""
    local_min = cq_binary.load_min_version(metadata_path)
    remote_min = _load_remote_min_version()

    if remote_min and cq_binary.parse_semver(remote_min) > cq_binary.parse_semver(local_min):
        return remote_min

    return local_min


def main() -> None:
    """Ensure the cq binary is cached, then exec into the MCP server."""
    metadata_path = Path(__file__).resolve().with_name("bootstrap.json")
    min_version = _resolve_min_version(metadata_path)
    if not min_version:
        print("Error: minimum CLI version not set in bootstrap metadata", file=sys.stderr)
        sys.exit(1)

    bin_dir = cq_binary.shared_bin_dir()
    binary = bin_dir / cq_binary.cq_binary_name()

    cq_binary.ensure_binary(binary, min_version, bin_dir)

    os.execvp(str(binary), [str(binary), "mcp"])


if __name__ == "__main__":
    main()
