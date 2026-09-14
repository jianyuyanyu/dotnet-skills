#!/usr/bin/env python3
"""Refresh pending watched skills through the configured content updater."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path, PurePosixPath

import import_external_catalog_sources as importer
import upstream_watch as watch

ROOT = Path(__file__).resolve().parents[1]


def imported_skills() -> set[str]:
    names: set[str] = set()
    for config_path in sorted((ROOT / "external-sources/imports").glob("*.json")):
        config = importer.load_json(config_path)
        plugins = importer.discover_upstream_plugins(importer.resolve_source_root(config["sourceRoot"]))
        for plugin_name in plugins:
            policy = importer.resolve_plugin_policy(config_path, config, plugin_name)
            package = ROOT / "catalog" / policy["type"] / policy["package"]
            names.update(path.parent.name for path in package.glob("skills/*/SKILL.md"))
    return names


def select_work(pending: dict, config: dict) -> dict:
    if pending.get("errors"):
        raise ValueError("Cannot refresh after upstream detection failed")
    index = {entry["id"]: entry for entry in config["watches"]}
    tasks: dict[str, dict] = {}
    for key in pending["pending_watch_ids"]:
        if key not in index:
            raise ValueError(f"Unknown pending watch: {key}")
        entry = index[key]
        for skill in entry["skills"]:
            task = tasks.setdefault(skill, {"skill": skill, "watches": {}})
            task["watches"][key] = entry
    return tasks


def apply_response(skill_dir: Path, response: dict) -> bool:
    files = response.get("files")
    if not isinstance(files, dict) or not isinstance(response.get("summary"), str) or not response["summary"].strip():
        raise ValueError("Updater must return files and a nonempty evidence summary")
    changes: dict[Path, str] = {}
    total = 0
    for name, content in files.items():
        path = PurePosixPath(name)
        if (str(path) != name or path.is_absolute() or ".." in path.parts or "\\" in name
                or not (name == "SKILL.md" or (name.startswith("references/") and name.endswith(".md")))):
            raise ValueError(f"Updater returned a disallowed path: {name}")
        target = skill_dir / name
        if not target.resolve().is_relative_to(skill_dir.resolve()) or target.is_symlink():
            raise ValueError(f"Updater path escapes the skill: {name}")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"Updater returned empty or invalid content: {name}")
        total += len(content.encode())
        if total > 256_000:
            raise ValueError("Updater output exceeds the per-skill size budget")
        if name == "SKILL.md":
            original = (skill_dir / name).read_text()
            original_front = original.split("---", 2)[:2]
            if content.split("---", 2)[:2] != original_front:
                raise ValueError("Updater must preserve skill identity and frontmatter")
        if not target.exists() or target.read_text() != content:
            changes[target] = content
    if not changes:
        return False
    manifest_path = skill_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    version = manifest["version"]
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        raise ValueError(f"Cannot bump non-stable skill version {version}")
    major, minor, patch = map(int, match.groups())
    manifest["version"] = f"{major}.{minor}.{patch + 1}"
    for path, content in changes.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return True


def refresh_skill(skill_dir: Path, task: dict, command: list[str]) -> dict:
    if not command:
        raise RuntimeError("NIGHTLY_SKILL_REFRESH_COMMAND is not configured for repo-owned guidance")
    documents = {"SKILL.md": (skill_dir / "SKILL.md").read_text()}
    documents.update({str(p.relative_to(skill_dir)): p.read_text() for p in sorted((skill_dir / "references").rglob("*.md"))})
    request = {**task, "files": documents, "manifest": json.loads((skill_dir / "manifest.json").read_text())}
    # Updaters receive data through stdin and return JSON, never shell patches.
    # The shared runner applies returned Markdown and validates the final scope.
    result = subprocess.run(command, input=json.dumps(request), text=True, capture_output=True, timeout=600)
    if result.returncode:
        raise RuntimeError(f"Skill updater failed ({result.returncode}): {result.stderr[-2000:]}")
    if len(result.stdout.encode()) > 300_000:
        raise ValueError("Skill updater response exceeds its size budget")
    response = json.loads(result.stdout)
    changed = apply_response(skill_dir, response)
    return {"status": "updated" if changed else "unchanged", "summary": response["summary"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pending", type=Path, default=ROOT / "artifacts/nightly-refresh/pending.json")
    parser.add_argument("--report", type=Path, default=ROOT / "artifacts/nightly-refresh/report.json")
    parser.add_argument("--dry-run", action="store_true", help="List pending skills without updates or model calls")
    args = parser.parse_args()
    config = watch.normalize_config(watch.merge_raw_configs(watch.resolve_config_paths(str(ROOT / ".github/upstream-watch.json"))))
    tasks = select_work(json.loads(args.pending.read_text()), config)
    imported = imported_skills()
    paths = {path.parent.name: path.parent for path in (ROOT / "catalog").glob("*/*/skills/*/SKILL.md")}
    results: dict[str, dict] = {}
    command = shlex.split(os.environ.get("NIGHTLY_SKILL_REFRESH_COMMAND", ""))
    for skill, task in sorted(tasks.items()):
        try:
            if skill not in paths:
                raise ValueError(f"Watched skill is missing from the catalog: {skill}")
            if args.dry_run:
                results[skill] = {"status": "planned", "updater": "vendir" if skill in imported else "content"}
            elif skill in imported:
                # The caller must finish vendir sync/import before running this.
                results[skill] = {"status": "imported", "summary": "Refreshed through vendir and the canonical importer"}
            else:
                results[skill] = refresh_skill(paths[skill], task, command)
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            results[skill] = {"status": "failed", "error": str(exc)[:2000]}
    report = {"skills": results, "failed": any(r["status"] == "failed" for r in results.values())}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Nightly refresh: {len(tasks)} skills, {sum(r['status'] == 'failed' for r in results.values())} failures")
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
