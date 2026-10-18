#!/usr/bin/env python3
"""Upsert a released image and its remaining vulnerabilities in README.md."""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

START = "<!-- released-images:start -->"
END = "<!-- released-images:end -->"

readme_path, report_path = map(Path, sys.argv[1:3])
image, version = sys.argv[3:5]
section = report_path.read_text().split("## Remaining vulnerabilities", 1)[-1].split("## Release decision", 1)[0]
rows = re.findall(r"\| (Critical|High|Medium|Low|Negligible|Unknown) \|.*\| ([A-Z]+-[A-Za-z0-9-]+) \|$", section, re.MULTILINE)
counts = Counter(severity for severity, _ in rows)
ids = sorted({identifier for _, identifier in rows})
links = []
for identifier in ids:
    url = f"https://nvd.nist.gov/vuln/detail/{identifier}" if identifier.startswith("CVE-") else f"https://github.com/advisories/{identifier}"
    links.append(f"[{identifier}]({url})")
finding_text = ", ".join(links) if links else "✅ None"
new_row = f"| {version} | `{image}` | " + " | ".join(str(counts.get(level, 0)) for level in ("Critical", "High", "Medium", "Low")) + f" | {finding_text} |"

text = readme_path.read_text()
generated = text.split(START, 1)[1].split(END, 1)[0] if START in text and END in text else ""
old_rows = [line for line in generated.splitlines() if line.startswith("| ") and not line.startswith("| Release") and not line.startswith("|---")]
old_rows = [line for line in old_rows if f"`{image}`" not in line]
table = "\n".join([
    START,
    "| Release | Image | Critical | High | Medium | Low | Current vulnerabilities |",
    "|---|---|---:|---:|---:|---:|---|",
    new_row,
    *old_rows[:9],
    END,
])
if START in text and END in text:
    text = re.sub(re.escape(START) + r".*?" + re.escape(END), table, text, flags=re.DOTALL)
else:
    text += "\n\n## Released images and current vulnerabilities\n\n" + table + "\n"
readme_path.write_text(text)
