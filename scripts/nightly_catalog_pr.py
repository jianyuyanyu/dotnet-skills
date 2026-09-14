#!/usr/bin/env python3
"""Publish and merge the nightly catalog candidate; never mutate watch state."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import Any

from upstream_watch import gh_api

BRANCH = "codex/nightly-catalog-refresh"
MARKER = "<!-- nightly-catalog-refresh -->"
FAILURE_LABEL = "nightly-refresh-failure"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr[-4096:]}")
    return result.stdout.strip()


def output(**values: str) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as stream:
            for key, value in values.items():
                stream.write(f"{key}={value}\n")


def allowed_path(path: str) -> bool:
    return path.startswith(("catalog/", "external-sources/upstreams/")) or path == "external-sources/vendir.lock.yml"


def candidate_paths() -> list[str]:
    changed = git("diff", "--name-only", "HEAD", "--").splitlines()
    untracked = git("ls-files", "--others", "--exclude-standard").splitlines()
    paths = sorted(set(changed + untracked))
    invalid = [p for p in paths if not allowed_path(p)]
    if invalid:
        raise ValueError(f"Refresh modified files outside the catalog scope: {invalid[:10]}")
    return paths


def find_pr(repo: str, token: str) -> dict | None:
    owner = repo.split("/")[0]
    prs = gh_api(f"/repos/{repo}/pulls?state=open&head={owner}:{BRANCH}&base=main", token=token)
    if not prs:
        return None
    if len(prs) != 1 or MARKER not in (prs[0].get("body") or ""):
        raise ValueError("The nightly branch has an unrecognized PR; refusing to overwrite it")
    return prs[0]


def publish(repo: str, token: str) -> None:
    paths = candidate_paths()
    pr = find_pr(repo, token)
    # Lock or transport-only changes are not a catalog update.
    if not any(p.startswith("catalog/") for p in paths):
        output(changed="false")
        print("No catalog changes; no PR or release needed.")
        return
    base = git("rev-parse", "HEAD")
    git("add", "--", *paths)
    tree = git("write-tree")
    remote = git("ls-remote", "--heads", "origin", f"refs/heads/{BRANCH}")
    previous = remote.split()[0] if remote else ""
    if previous:
        git("fetch", "origin", BRANCH)
        if git("show", "-s", "--format=%ae", previous) != BOT_EMAIL:
            raise ValueError("The nightly branch contains a human commit; refusing to overwrite it")
        same_tree = git("rev-parse", f"{previous}^{{tree}}") == tree
        same_base = git("rev-parse", f"{previous}^") == base
    else:
        same_tree = same_base = False
    if same_tree and same_base:
        head = previous
    else:
        git("-c", "user.name=github-actions[bot]", "-c", f"user.email={BOT_EMAIL}",
            "commit", "-m", "chore: refresh upstream skill catalog")
        head = git("rev-parse", "HEAD")
        git("push", f"--force-with-lease=refs/heads/{BRANCH}:{previous}", "origin", f"HEAD:refs/heads/{BRANCH}")
    body = (
        f"{MARKER}\nRefresh imported upstream sources and affected catalog skills.\n\n"
        f"Validation runs against `{head}` using the reusable PR Checks workflow. "
        "Automatic merge requires every check to succeed and the base to remain unchanged.\n\n"

    )
    fields = {"title": "chore: nightly upstream catalog refresh", "body": body}
    if pr:
        pr = gh_api(f"/repos/{repo}/pulls/{pr['number']}", token=token, method="PATCH", data=fields)
    else:
        pr = gh_api(f"/repos/{repo}/pulls", token=token, method="POST", data={**fields, "head": BRANCH, "base": "main"})
    output(changed="true", head=head, base=base, pr=str(pr["number"]))
    print(f"Catalog candidate: {pr['html_url']} ({head})")


def verify_merge_candidate(pr: dict, head: str, base: str, current_main: str) -> None:
    if pr.get("state") != "open" or pr.get("draft") or MARKER not in (pr.get("body") or ""):
        raise ValueError("PR is not an open nightly catalog candidate")
    if pr["head"]["ref"] != BRANCH or pr["head"]["sha"] != head:
        raise ValueError("PR head changed after validation")
    if pr["base"]["ref"] != "main" or current_main != base or pr["base"]["sha"] != base:
        raise ValueError("Main changed during validation; retry against the new base next night")
    if pr["head"]["repo"]["full_name"] != pr["base"]["repo"]["full_name"]:
        raise ValueError("Nightly PR must belong to this repository")


def merge(repo: str, token: str, number: int, head: str, base: str) -> None:
    pr = gh_api(f"/repos/{repo}/pulls/{number}", token=token)
    main = gh_api(f"/repos/{repo}/git/ref/heads/main", token=token)["object"]["sha"]
    verify_merge_candidate(pr, head, base, main)
    result = gh_api(f"/repos/{repo}/pulls/{number}/merge", token=token, method="PUT",
                    data={"sha": head, "merge_method": "squash"})
    if not result.get("merged"):
        raise RuntimeError(f"GitHub refused merge: {result.get('message')}")
    print(f"Merged PR #{number}: {result['sha']}")


def list_labeled_issues(repo: str, token: str, label: str, *, state: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = gh_api(
            f"/repos/{repo}/issues?state={state}&labels={label}&per_page=100&page={page}",
            token=token,
        )
        if not isinstance(batch, list):
            raise RuntimeError(f"Unexpected issue payload while listing failure issues for {repo}")
        if not batch:
            break
        issues.extend(issue for issue in batch if not issue.get("pull_request"))
        page += 1
    return issues


def report_status(repo: str, token: str, failed: bool, details: str) -> None:
    # List by marker as well as label: do not close unrelated issues.
    issues = list_labeled_issues(repo, token, FAILURE_LABEL, state="open")
    issues = [i for i in issues if MARKER in (i.get("body") or "")]
    if not failed:
        for issue in issues:
            gh_api(f"/repos/{repo}/issues/{issue['number']}", token=token, method="PATCH", data={"state": "closed"})
        return
    run = f"https://github.com/{repo}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    fields = {"title": "Nightly catalog refresh failed", "body": f"{MARKER}\nThe nightly refresh did not complete. Pending upstream work remains queued.\n\n[Failed run]({run})\n\n{details[:6000]}"}
    if issues:
        gh_api(f"/repos/{repo}/issues/{issues[0]['number']}", token=token, method="PATCH", data=fields)
    else:
        # Ensure the failure label exists without masking permission/network failures.
        labels = gh_api(f"/repos/{repo}/labels?per_page=100", token=token)
        if not any(label["name"] == FAILURE_LABEL for label in labels):
            gh_api(f"/repos/{repo}/labels", token=token, method="POST", data={"name": FAILURE_LABEL, "color": "B60205", "description": "Nightly catalog refresh needs attention"})
        gh_api(f"/repos/{repo}/issues", token=token, method="POST", data={**fields, "labels": [FAILURE_LABEL]})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["publish", "merge", "report"])
    parser.add_argument("--pr", type=int)
    parser.add_argument("--head")
    parser.add_argument("--base")
    parser.add_argument("--failed", action="store_true")
    parser.add_argument("--details", default="")
    args = parser.parse_args()
    repo, token = os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"]
    if args.action == "publish":
        publish(repo, token)
    elif args.action == "merge":
        if not args.pr or not args.head or not args.base:
            parser.error("merge requires --pr, --head and --base")
        merge(repo, token, args.pr, args.head, args.base)
    else:
        report_status(repo, token, args.failed, args.details)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        reason = " ".join(str(exc).split())[:1500]
        output(error=reason)
        print(f"Error: {reason}", file=sys.stderr)
        raise SystemExit(1)
