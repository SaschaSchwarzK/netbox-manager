from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]


def run(script: str, *args: Path | str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script), *map(str, args)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_security_report_combines_scanners_and_fails_on_high(tmp_path: Path) -> None:
    grype = tmp_path / "grype.json"
    trivy = tmp_path / "trivy.json"
    grype.write_text(json.dumps({"matches": [{
        "artifact": {"name": "demo", "version": "1.0"},
        "vulnerability": {"id": "CVE-2026-1", "severity": "High", "fix": {"versions": ["1.1"]}},
    }]}))
    trivy.write_text(json.dumps({"Results": []}))

    result = run("security_report.py", "check", grype, trivy)

    assert result.returncode == 1
    assert "CVE-2026-1" in result.stdout


def test_dependency_update_adds_python_security_floor(tmp_path: Path) -> None:
    scan = tmp_path / "scan.json"
    requirements = tmp_path / "requirements.txt"
    scan.write_text(json.dumps({"Results": [{
        "Type": "python-pkg",
        "Vulnerabilities": [{"PkgName": "urllib3", "FixedVersion": "2.6.1"}],
    }]}))
    requirements.write_text("fastapi==1.0\n")

    result = run("update_security_dependencies.py", scan, requirements)

    assert result.returncode == 0, result.stderr
    assert requirements.read_text() == "fastapi==1.0\nurllib3>=2.6.1\n"


def test_readme_update_upserts_release(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    report = tmp_path / "report.md"
    readme.write_text("# Project\n\n<!-- released-images:start -->\n<!-- released-images:end -->\n")
    report.write_text("""## Remaining vulnerabilities

| Severity | Package | Installed | Fixed | Vulnerability |
|---|---|---|---|---|
| Medium | `demo` | `1` | `2` | CVE-2026-2 |

## Release decision
""")

    result = run(
        "update_readme_cves.py",
        readme,
        report,
        "ghcr.io/example/project:v1.0.0",
        "v1.0.0",
    )

    assert result.returncode == 0, result.stderr
    text = readme.read_text()
    assert "| v1.0.0 |" in text
    assert "| 0 | 0 | 1 | 0 |" in text
    assert "https://nvd.nist.gov/vuln/detail/CVE-2026-2" in text
