#!/usr/bin/env python3
"""Install local Orca desktop packages on first use and inspect desktop state."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
from tempfile import TemporaryDirectory


PACKAGE_SUFFIXES = {".py", ".whl"}
PLUGIN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def seed(root, plugins):
    inbox = root / "packages"
    inbox.mkdir(parents=True, exist_ok=True)
    plugins.mkdir(parents=True, exist_ok=True)
    packages = {}
    for source in sorted(inbox.iterdir()):
        if source.suffix not in PACKAGE_SUFFIXES:
            continue
        if (not source.is_file() or source.is_symlink()
                or not PLUGIN_ID.fullmatch(source.stem) or source.stem == "plugin_data"):
            raise ValueError(f"Invalid local package: {source.name}")
        if source.stem in packages:
            raise ValueError(f"Multiple local packages use the name {source.stem}")
        packages[source.stem] = source

    for source in packages.values():
        target = plugins / source.name
        if target.exists() or target.is_symlink():
            continue
        with TemporaryDirectory(prefix=".laminus-", dir=plugins) as staging:
            package = Path(staging) / source.name
            package.mkdir()
            shutil.copyfile(source, package / source.name)
            (package / ".install_state.json").write_text(
                json.dumps({"installed_from": "local", "enabled": True}) + "\n", encoding="utf-8"
            )
            package.rename(target)
        print(f"[laminus] Seeded local Orca package {source.name}")


def inventory(plugins):
    directories = list(plugins.glob("*")) + list(plugins.glob("_subscribed/*/*"))
    print(f"Orca desktop package inventory ({plugins}):")
    for package in sorted(directories):
        if (not package.is_dir() or package.name in {"_subscribed", "plugin_data"}
                or package.name.startswith((".", "__"))):
            continue
        entries = sorted(file.name for file in package.iterdir()
                         if file.is_file() and file.suffix in PACKAGE_SUFFIXES)
        state_path = package / ".install_state.json"
        state_label = "disabled"
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state_label = "enabled" if state.get("enabled") is True else "disabled"
            except (OSError, ValueError, AttributeError):
                state_label = "invalid install state"
        entry = entries[0] if len(entries) == 1 else "expected exactly one .py or .whl"
        print(f"  {package.relative_to(plugins)}\t{state_label}\t{entry}")


def show_logs(log_dir, match):
    logs = sorted([*log_dir.glob("*.log"), *log_dir.glob("*.log.*")])
    if not logs:
        print(f"No Orca desktop logs found in {log_dir}", file=sys.stderr)
    needle = match.casefold()
    for log in logs:
        with log.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if needle in line.casefold():
                    print(f"{log.name}: {line.rstrip()}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("seed", help="install new local packages from the packages directory")
    commands.add_parser("plugins", help="list installed Orca desktop packages and their enable flags")
    logs = commands.add_parser("logs", help="print native Orca desktop logs")
    logs.add_argument("--match", default="", help="case-insensitive substring to match")
    args = parser.parse_args()

    root = Path(os.environ.get("ORCA_DESKTOP_ROOT", "/config/orca-desktop"))
    state = root / "state"
    try:
        if args.command == "seed":
            seed(root, state / "orca_plugins")
        elif args.command == "plugins":
            inventory(state / "orca_plugins")
        else:
            show_logs(state / "log", args.match)
    except (OSError, ValueError) as error:
        parser.exit(1, f"orca-diagnostics: {error}\n")


if __name__ == "__main__":
    main()
