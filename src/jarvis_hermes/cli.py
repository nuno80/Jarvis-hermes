"""Read-only local diagnostics. No network, shell execution or credential access."""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
from pathlib import Path


def diagnose(disk_path: Path, vault_path: str | None) -> dict:
    usage = shutil.disk_usage(disk_path)
    release = platform.release().lower()
    vault_status = "not_configured"
    if vault_path:
        vault = Path(vault_path).expanduser()
        vault_status = "available" if vault.is_absolute() and vault.is_dir() else "unavailable"
    return {
        "schema_version": "1.0",
        "scope": "current_host_only",
        "host": {
            "system": platform.system(),
            "python": platform.python_version(),
            "is_wsl": platform.system() == "Linux" and "microsoft" in release,
        },
        "disk": {"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free},
        "vault": {"status": vault_status, "contents_read": False},
        "integrations": {
            "hermes": "not_verified",
            "telegram": "not_verified",
            "windows_gui": "not_verified",
            "mcp": "stdio_available",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="JARVIS local read-only diagnostics")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="Inspect only the current host, without connections")
    doctor.add_argument("--json", action="store_true", dest="as_json")
    doctor.add_argument("--disk-path", type=Path, default=Path.cwd())
    sub.add_parser("serve", help="Run the local read-only MCP server over stdio")
    args = parser.parse_args()
    if args.command == "serve":
        from .server import main as serve
        serve()
        return 0
    try:
        report = diagnose(args.disk_path, os.environ.get("JARVIS_VAULT_PATH"))
    except OSError:
        # Do not echo paths or exception strings that could contain private data.
        parser.exit(2, "Cannot inspect the requested disk path. Check that it exists and is accessible.\n")
    if args.as_json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Host: {report['host']['system']}; Python: {report['host']['python']}")
        print(f"Free disk: {report['disk']['free_bytes'] / (1024**3):.1f} GiB")
        print(f"Vault: {report['vault']['status']}")
        print("Hermes, Telegram and Windows GUI: not verified. MCP: local stdio server available; host connection not verified.")
        print("This report describes the current host only.")
    return 0
