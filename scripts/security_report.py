#!/usr/bin/env python3
"""Combine Grype and Trivy JSON and enforce the image vulnerability policy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

SEVERE = {"critical", "high"}


def findings(paths: list[Path]) -> dict[tuple[str, str, str], dict[str, str]]:
    found: dict[tuple[str, str, str], dict[str, str]] = {}
    for path in paths:
        data = json.loads(path.read_text())
        if "matches" in data:
            rows = (
                {
                    "id": match["vulnerability"]["id"],
                    "severity": match["vulnerability"].get("severity", "Unknown").title(),
                    "package": match["artifact"].get("name", "unknown"),
                    "installed": match["artifact"].get("version", "unknown"),
                    "fixed": ", ".join(match["vulnerability"].get("fix", {}).get("versions", [])) or "unfixed",
                }
                for match in data.get("matches", [])
            )
        else:
            rows = (
                {
                    "id": vulnerability["VulnerabilityID"],
                    "severity": vulnerability.get("Severity", "Unknown").title(),
                    "package": vulnerability.get("PkgName", "unknown"),
                    "installed": vulnerability.get("InstalledVersion", "unknown"),
                    "fixed": vulnerability.get("FixedVersion") or "unfixed",
                }
                for result in data.get("Results", [])
                for vulnerability in result.get("Vulnerabilities") or []
            )
        for row in rows:
            found[(row["id"], row["package"], row["installed"])] = row
    return found


def render_table(title: str, rows: list[dict[str, str]]) -> list[str]:
    lines = [f"## {title}", ""]
    if not rows:
        return lines + ["None.", ""]
    lines += [
        "| Severity | Package | Installed | Fixed | Vulnerability |",
        "|---|---|---|---|---|",
    ]
    order = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "Negligible": 4, "Unknown": 5}
    for row in sorted(rows, key=lambda item: (order.get(item["severity"], 6), item["id"], item["package"])):
        lines.append(
            f"| {row['severity']} | `{row['package']}` | `{row['installed']}` | "
            f"`{row['fixed']}` | {row['id']} |"
        )
    return lines + [""]


parser = argparse.ArgumentParser()
commands = parser.add_subparsers(dest="command", required=True)
check = commands.add_parser("check")
check.add_argument("files", nargs="+", type=Path)
report = commands.add_parser("report")
report.add_argument("--before", nargs="+", type=Path, default=[])
report.add_argument("--after", nargs="+", required=True, type=Path)
report.add_argument("--image", required=True)
report.add_argument("--output", required=True, type=Path)
args = parser.parse_args()

current = findings(args.files if args.command == "check" else args.after)
severe = [row for row in current.values() if row["severity"].lower() in SEVERE]
if args.command == "check":
    for row in severe:
        print(f"{row['severity']}: {row['id']} in {row['package']} {row['installed']} (fixed: {row['fixed']})")
    if not severe:
        print("No critical or high vulnerabilities found.")
    raise SystemExit(bool(severe))

before = findings(args.before)
resolved = [row for key, row in before.items() if key not in current]
lines = [f"Security report for `{args.image}`", ""]
lines += render_table("Resolved vulnerabilities", resolved)
lines += render_table("Remaining vulnerabilities", list(current.values()))
lines += ["## Release decision", ""]
lines.append(
    "❌ Blocked: critical or high vulnerabilities remain."
    if severe
    else "✅ Eligible for release: no critical or high vulnerabilities remain."
)
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text("\n".join(lines) + "\n")
raise SystemExit(bool(severe))
