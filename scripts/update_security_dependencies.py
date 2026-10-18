#!/usr/bin/env python3
"""Add minimum Python versions for fixable vulnerabilities found in image scans."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def canonical(value: str) -> str:
    return value.strip().lower().replace("_", "-")


def version_key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", value)[:4])


def add(candidates: dict[str, str], package: str, fixed: str) -> None:
    package = canonical(package)
    fixed = fixed.split(",", 1)[0].strip()
    if package and fixed and (package not in candidates or version_key(fixed) > version_key(candidates[package])):
        candidates[package] = fixed


*scan_names, requirements_name = sys.argv[1:]
candidates: dict[str, str] = {}
for scan_name in scan_names:
    data = json.loads(Path(scan_name).read_text())
    for match in data.get("matches", []):
        artifact = match.get("artifact", {})
        fixes = match.get("vulnerability", {}).get("fix", {}).get("versions", [])
        if artifact.get("type", "").lower() in {"python", "pypi"} and fixes:
            add(candidates, artifact.get("name", ""), fixes[0])
    for result in data.get("Results", []):
        if result.get("Type", "").lower() not in {"python", "python-pkg", "pypi"}:
            continue
        for vulnerability in result.get("Vulnerabilities") or []:
            add(candidates, vulnerability.get("PkgName", ""), vulnerability.get("FixedVersion", ""))

path = Path(requirements_name)
lines = path.read_text().splitlines()
positions = {
    canonical(re.split(r"[<>=!~;\[]", line, maxsplit=1)[0]): index
    for index, line in enumerate(lines)
    if line.strip() and not line.lstrip().startswith("#")
}
for package, fixed in sorted(candidates.items()):
    if package in positions:
        index = positions[package]
        original = lines[index]
        display = re.split(r"[<>=!~;\[]", original, maxsplit=1)[0].strip()
        extras = (re.search(r"\[[^]]+\]", original) or [""])[0]
        lines[index] = f"{display}{extras}>={fixed}"
    else:
        lines.append(f"{package}>={fixed}")
path.write_text("\n".join(lines) + "\n")
