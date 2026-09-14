#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import hashlib
import html
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


USER_AGENT = "dotnet-skills-upstream-watch"


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def resolve_config_paths(config_reference: str) -> list[Path]:
    if any(char in config_reference for char in "*?[]"):
        paths = [Path(path_str) for path_str in sorted(glob.glob(config_reference))]
        if not paths:
            raise ValueError(f"No config files matched pattern: {config_reference}")
        return paths

    base_path = Path(config_reference)
    if not base_path.exists():
        raise ValueError(f"Config file not found: {config_reference}")

    paths = [base_path]
    sibling_pattern = f"{base_path.stem}*{base_path.suffix}"
    excluded_names = {
        base_path.name,
        f"{base_path.stem}-state{base_path.suffix}",
    }
    for sibling in sorted(base_path.parent.glob(sibling_pattern)):
        if sibling == base_path:
            continue
        if sibling.name in excluded_names:
            continue
        paths.append(sibling)
    return paths


def merge_raw_configs(config_paths: list[Path]) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "github_releases": [],
        "documentation": [],
    }
    for path in config_paths:
        raw_config = load_json(path, default={})
        if not isinstance(raw_config, dict):
            raise ValueError(f"Config file {path} must contain a JSON object")

        for key in ("github_releases", "documentation"):
            value = raw_config.get(key, [])
            if value in (None, []):
                continue
            if not isinstance(value, list):
                raise ValueError(f"{key} in {path} must be a list")
            merged[key].extend(value)

    return merged


def slugify(value: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value.lower())).strip("-")


def parse_github_repo_reference(reference: str) -> tuple[str, str]:
    cleaned = reference.strip().removesuffix(".git").rstrip("/")
    if cleaned.startswith("https://") or cleaned.startswith("http://"):
        parsed = urlparse(cleaned)
        if parsed.netloc.lower() != "github.com":
            raise ValueError(f"Unsupported GitHub repository URL: {reference}")
        parts = [part for part in parsed.path.split("/") if part]
    else:
        parts = [part for part in cleaned.split("/") if part]

    if len(parts) < 2:
        raise ValueError(f"GitHub repository reference must be owner/repo or a GitHub repo URL: {reference}")

    owner, repo = parts[:2]
    return owner, repo


def is_http_url(reference: str) -> bool:
    return reference.startswith("https://") or reference.startswith("http://")


def classify_source(reference: str) -> str:
    if not is_http_url(reference):
        return "github_release"

    parsed = urlparse(reference)
    if parsed.netloc.lower() == "github.com":
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2:
            return "github_release"

    return "http_document"


def default_http_watch_id(url: str) -> str:
    parsed = urlparse(url)
    host = slugify(parsed.netloc)
    path = slugify(parsed.path.strip("/")) or "root"
    return f"{host}-{path}-docs"


def default_http_watch_name(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.strip("/") or "/"
    return f"{parsed.netloc}{path} documentation"


def normalize_github_release_watch(watch: dict[str, Any]) -> dict[str, Any]:
    repo_reference = watch.get("source") or watch.get("repo")
    if not isinstance(repo_reference, str) or not repo_reference.strip():
        raise ValueError("github_release watch requires a non-empty source field")

    owner, repo = parse_github_repo_reference(repo_reference)
    normalized: dict[str, Any] = {
        "id": watch.get("id") or f"{slugify(owner)}-{slugify(repo)}-release",
        "kind": "github_release",
        "name": watch.get("name") or f"{owner}/{repo} release",
        "owner": owner,
        "repo": repo,
        "notes": watch.get("notes") or f"Review the linked skills when {owner}/{repo} ships a new release.",
        "skills": watch.get("skills"),
    }

    for key in ("match_tag_regex", "exclude_tag_regex", "include_prereleases"):
        if key in watch:
            normalized[key] = watch[key]

    return normalized


def normalize_http_document_watch(watch: dict[str, Any]) -> dict[str, Any]:
    url = watch.get("source") or watch.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("http_document watch requires a non-empty source field")

    return {
        "id": watch.get("id") or default_http_watch_id(url),
        "kind": "http_document",
        "name": watch.get("name") or default_http_watch_name(url),
        "url": url,
        "notes": watch.get("notes") or f"Review the linked skills when {url} changes.",
        "skills": watch.get("skills"),
    }


def validate_skills(watch: dict[str, Any]) -> None:
    skills = watch.get("skills")
    if not isinstance(skills, list) or not skills or not all(isinstance(skill, str) and skill for skill in skills):
        raise ValueError(f"Watch {watch.get('id', '<unknown>')} must define a non-empty skills list")


def normalize_human_watch(watch: dict[str, Any], kind: str) -> dict[str, Any]:
    if not isinstance(watch, dict):
        raise ValueError(f"{kind} entries must be JSON objects")
    normalized = normalize_github_release_watch(watch) if kind == "github_release" else normalize_http_document_watch(watch)
    validate_skills(normalized)
    return normalized


def normalize_config(raw_config: dict[str, Any]) -> dict[str, Any]:
    github_releases = raw_config.get("github_releases", [])
    documentation = raw_config.get("documentation", [])
    if not isinstance(github_releases, list) or not isinstance(documentation, list):
        raise ValueError("github_releases and documentation must both be lists")

    watches: list[dict[str, Any]] = []
    watch_ids: set[str] = set()

    for watch in github_releases:
        normalized = normalize_human_watch(watch, "github_release")
        if normalized["id"] in watch_ids:
            raise ValueError(f"Duplicate watch id {normalized['id']!r} in upstream-watch config")
        watch_ids.add(normalized["id"])
        watches.append(normalized)

    for watch in documentation:
        normalized = normalize_human_watch(watch, "http_document")
        if normalized["id"] in watch_ids:
            raise ValueError(f"Duplicate watch id {normalized['id']!r} in upstream-watch config")
        watch_ids.add(normalized["id"])
        watches.append(normalized)

    return {
        "watches": watches,
    }


def run_curl(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    data: dict[str, Any] | None = None,
) -> tuple[dict[str, str], bytes]:
    headers = headers or {}

    with tempfile.TemporaryDirectory() as tmp:
        headers_path = Path(tmp) / "headers.txt"
        body_path = Path(tmp) / "body.bin"

        cmd = [
            "curl",
            "-fsSL",
            "--retry",
            "3",
            "--retry-delay",
            "2",
            "--retry-all-errors",
            "--connect-timeout",
            "20",
            "--max-time",
            "120",
            "-A",
            USER_AGENT,
            "-X",
            method,
            "-D",
            str(headers_path),
            "-o",
            str(body_path),
        ]

        for key, value in headers.items():
            cmd.extend(["-H", f"{key}: {value}"])

        if data is not None:
            cmd.extend(["-H", "Content-Type: application/json", "--data", json.dumps(data)])

        cmd.append(url)

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"curl failed for {url}")

        return parse_headers(headers_path.read_text()), body_path.read_bytes()


def parse_headers(raw_headers: str) -> dict[str, str]:
    blocks = re.split(r"\r?\n\r?\n", raw_headers.strip())
    for block in reversed(blocks):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        parsed: dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            parsed[key.strip().lower()] = value.strip()
        if parsed:
            return parsed
    return {}


def gh_api(
    path: str,
    *,
    token: str | None,
    method: str = "GET",
    data: dict[str, Any] | None = None,
) -> Any:
    env = os.environ.copy()
    if token:
        env["GH_TOKEN"] = token

    cmd = [
        "gh",
        "api",
        path.lstrip("/"),
        "--method",
        method,
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2022-11-28",
    ]

    payload = None
    if data is not None:
        cmd.extend(["--input", "-"])
        payload = json.dumps(data)

    result = subprocess.run(cmd, capture_output=True, text=True, input=payload, env=env)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        if stderr and stdout and stdout not in stderr:
            raise RuntimeError(f"{stderr}\n{stdout}")
        raise RuntimeError(stderr or stdout or f"gh api failed for {path}")

    stdout = result.stdout.strip()
    if not stdout:
        return {}
    return json.loads(stdout)


def human_release(release: dict[str, Any]) -> str:
    tag = release.get("tag_name") or release.get("name") or str(release.get("id"))
    published_at = release.get("published_at")
    if published_at:
        return f"{tag} ({published_at})"
    return tag


def fetch_github_release(watch: dict[str, Any], token: str | None) -> dict[str, Any]:
    releases: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = gh_api(
            f"/repos/{watch['owner']}/{watch['repo']}/releases?per_page=100&page={page}",
            token=token,
        )
        if not isinstance(batch, list):
            raise RuntimeError(f"Unexpected release payload for {watch['id']}")
        releases.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    include_prereleases = bool(watch.get("include_prereleases", False))
    match_tag_regex = watch.get("match_tag_regex")
    exclude_tag_regex = watch.get("exclude_tag_regex")
    candidates: list[dict[str, Any]] = []
    for release in releases:
        if release.get("draft"):
            continue
        if release.get("prerelease") and not include_prereleases:
            continue
        tag_name = release.get("tag_name") or ""
        if match_tag_regex and not re.search(match_tag_regex, tag_name):
            continue
        if exclude_tag_regex and re.search(exclude_tag_regex, tag_name):
            continue
        candidates.append(release)

    if not candidates:
        raise RuntimeError(f"No matching release found for {watch['owner']}/{watch['repo']}")

    # GitHub's releases endpoint is ordered by creation time, which can put a
    # servicing release from an older support line ahead of a newer release
    # that was published later. Track the newest published matching release so
    # watches do not move backwards when repositories publish several lines.
    selected = max(
        candidates,
        key=lambda release: (
            release.get("published_at") or "",
            release.get("created_at") or "",
            release.get("id") or 0,
        ),
    )

    return {
        "kind": "github_release",
        "value": selected.get("tag_name") or selected.get("name") or str(selected.get("id")),
        "human": human_release(selected),
        "source_url": selected.get("html_url") or watch.get("source_url"),
        "published_at": selected.get("published_at"),
    }


def extract_title(body: bytes) -> str | None:
    match = re.search(rb"<title>(.*?)</title>", body, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    title = re.sub(r"\s+", " ", html.unescape(match.group(1).decode("utf-8", errors="ignore"))).strip()
    return title or None


def fetch_http_document(watch: dict[str, Any]) -> dict[str, Any]:
    headers, body = run_curl(watch["url"])
    sha256 = hashlib.sha256(body).hexdigest()
    etag = headers.get("etag")
    last_modified = headers.get("last-modified")
    identifier = etag or last_modified or sha256
    title = extract_title(body)

    detail = []
    if title:
        detail.append(title)
    if etag:
        detail.append(f"ETag {etag}")
    elif last_modified:
        detail.append(f"Last-Modified {last_modified}")
    else:
        detail.append(f"SHA {sha256[:12]}")

    return {
        "kind": "http_document",
        "value": identifier,
        "human": " | ".join(detail),
        "source_url": watch["url"],
        "etag": etag,
        "last_modified": last_modified,
        "title": title,
    }


def fetch_snapshot(watch: dict[str, Any], token: str | None) -> dict[str, Any]:
    kind = watch["kind"]
    if kind == "github_release":
        return fetch_github_release(watch, token)
    if kind == "http_document":
        return fetch_http_document(watch)
    raise RuntimeError(f"Unsupported watch kind: {kind}")


def write_summary(lines: list[str]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    Path(summary_path).write_text("\n".join(lines) + "\n")


def detect_changes(config: dict, state: dict, token: str | None) -> dict:
    snapshots: dict[str, dict] = {}
    pending: list[str] = []
    errors: list[str] = []
    for entry in config["watches"]:
        try:
            current = fetch_snapshot(entry, token)
            snapshots[entry["id"]] = current
            if state.get("watches", {}).get(entry["id"], {}).get("value") != current["value"]:
                pending.append(entry["id"])
        except Exception as exc:
            errors.append(f"{entry['id']}: {exc}")
    return {"watches": snapshots, "pending_watch_ids": pending, "errors": errors}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detect upstream changes without creating issues.")
    parser.add_argument("--config", default=".github/upstream-watch.json")
    parser.add_argument("--state", default=".github/upstream-watch-state.json")
    parser.add_argument("--report-dir", type=Path, default=Path("artifacts/nightly-refresh"))
    parser.add_argument("--validate-config", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sync-state-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = normalize_config(merge_raw_configs(resolve_config_paths(args.config)))
    if args.validate_config:
        print(f"Validated {len(config['watches'])} upstream watches.")
        return 0
    state_path = Path(args.state)
    result = detect_changes(config, load_json(state_path, {"watches": {}}),
                            os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))
    dump_json(args.report_dir / "pending.json", result)
    # Candidate state is only promoted by the workflow after a successful merge
    # or a successfully validated no-op. Failed runs keep the applied baseline.
    dump_json(args.report_dir / "next-state.json", {"watches": result["watches"]})
    if args.sync_state_only and not args.dry_run and not result["errors"]:
        dump_json(state_path, {"watches": result["watches"]})
    summary = [f"Upstream watch: {len(config['watches'])} sources, {len(result['pending_watch_ids'])} changes, {len(result['errors'])} errors."]
    summary.extend(result["errors"][:20])
    print("\n".join(summary))
    write_summary(summary)
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
