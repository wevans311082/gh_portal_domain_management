"""Resolve and audit a complete application dependency profile without installing it.

Usage: python scripts/audit_dependencies.py --profile development --output-dir PATH
Requires pip-audit. Exit 1 preserves known findings; nothing is silently ignored.
"""
import argparse
from datetime import datetime, timezone
from importlib.metadata import version
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile


def normalize_report(raw, scope):
    dependencies = []
    for dependency in raw["dependencies"]:
        vulnerabilities = {}
        for vulnerability in dependency.get("vulns", []):
            identifier = vulnerability["id"]
            entry = vulnerabilities.setdefault(identifier, {"id": identifier, "aliases": set(), "fix_versions": set()})
            entry["aliases"].update(vulnerability.get("aliases", []))
            entry["fix_versions"].update(vulnerability.get("fix_versions", []))
        item = {"name": dependency["name"], "version": dependency.get("version"),
                "vulnerabilities": [{"id": entry["id"], "aliases": sorted(entry["aliases"]), "fix_versions": sorted(entry["fix_versions"])}
                                    for entry in sorted(vulnerabilities.values(), key=lambda entry: entry["id"])]}
        if dependency.get("skip_reason"):
            item["skip_reason"] = dependency["skip_reason"]
        dependencies.append(item)
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "scope": scope,
            "python": platform.python_version(), "platform": platform.platform(), "pip_audit": version("pip-audit"),
            "dependencies": sorted(dependencies, key=lambda item: item["name"].lower())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("production", "development"), default="production")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="portal-dependency-audit-") as temporary:
        work = Path(temporary)
        resolution = work / "resolution.json"
        command = [sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed", "--quiet",
                   "--disable-pip-version-check", "--report", str(resolution), "-r", str(root / "requirements/production.txt")]
        if args.profile == "development":
            command += ["-r", str(root / "requirements/development.txt")]
        subprocess.run(command, check=True, cwd=root)
        resolved = json.loads(resolution.read_text(encoding="utf-8"))
        pins = sorted(f"{item['metadata']['name']}=={item['metadata']['version']}" for item in resolved["install"])
        pin_file = args.output_dir / "resolved-pins.txt"
        pin_file.write_text("\n".join(pins) + "\n", encoding="utf-8")
        raw_report = work / "audit.json"
        result = subprocess.run([sys.executable, "-m", "pip_audit", "-r", str(pin_file.resolve()),
                                 "--no-deps", "--disable-pip", "--progress-spinner", "off", "--format", "json",
                                 "--output", str(raw_report)], cwd=root)
        if result.returncode not in {0, 1} or not raw_report.is_file():
            raise SystemExit("Dependency scan failed; no complete report was produced.")
        report = normalize_report(json.loads(raw_report.read_text(encoding="utf-8")), f"resolved-{args.profile}")
        (args.output_dir / "audit.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        skipped = [item for item in report["dependencies"] if "skip_reason" in item]
        findings = sum(len(item["vulnerabilities"]) for item in report["dependencies"])
        print(f"profile={args.profile} packages={len(pins)} advisory_findings={findings} skipped={len(skipped)}")
        return 1 if findings or skipped else result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
