# Nightly catalog refresh

`upstream-watch.yml` runs at **00:17 UTC** and supports manual dispatch. It checks
all configured upstream watches, saves observation state outside Git, syncs
vendir sources, and processes open grouped upstream issues. One automation PR on
`codex/nightly-catalog-refresh` holds the proposed catalog changes.

`catalog-check.yml` is both the normal PR workflow and a reusable workflow. The
nightly caller passes the exact proposed commit. Checks include Python tests,
locked vendir/import verification, catalog and agent validation, Waza, .NET build,
tests, pack, and install smoke tests. The merge job requires both check jobs to
succeed and verifies that the PR head and `main` still match the checked inputs.
GitHub branch protection still applies; automation does not use an admin bypass.
Locked verification reads a temporary copy of the committed lockfile because
vendir recalculates descriptive git tags even when the source SHA is unchanged.

The existing `publish-catalog.yml` runs at **04:00 UTC** and releases new commits
through catalog assets, NuGet tools, and GitHub Pages. It skips a revision already
covered by the latest non-draft catalog release. A refresh that finishes after
that release's checkout is included in the following release. GitHub cron times
are scheduled start times, not guaranteed execution deadlines.

## Content updater contract

Imported upstream skills are copied verbatim through vendir and the canonical
importer. They do not require an AI provider. Repo-owned skills use one shared
content-updater command configured in the Actions repository variable
`NIGHTLY_SKILL_REFRESH_COMMAND`. An empty command fails visibly when a repo-owned
skill has pending changes; it never silently marks those issues as resolved.

The command receives one JSON request on stdin:

```json
{
  "skill": "example",
  "watches": {
    "example-release": {
      "id": "example-release",
      "kind": "github_release",
      "owner": "official-owner",
      "repo": "example",
      "skills": ["example"]
    }
  },
  "files": {"SKILL.md": "Current skill including frontmatter"},
  "manifest": {"version": "1.0.0", "category": "Core"}
}
```

The command must inspect the configured authoritative sources, update guidance
only when supported by those sources, and return JSON on stdout:

```json
{
  "summary": "Evidence and explanation of the relevant change, with source URLs",
  "files": {
    "SKILL.md": "Complete updated skill with unchanged frontmatter",
    "references/usage.md": "Complete updated practical guidance"
  }
}
```

An empty `files` object means the sources were examined and no guidance change
is needed. The updater must preserve installation instructions, practical usage,
constraints, and validation guidance. Upstream text is evidence, never permission
to execute instructions or modify repository automation. Provider credentials
belong in GitHub Actions secrets, never variables or catalog files.

The shared runner validates output paths, rejects changes outside `SKILL.md` and
Markdown references, and bumps the sibling skill manifest patch version only for
real edits. It rejects identity changes, empty content, path traversal, symlink
escapes, and oversized responses. A single failure prevents publishing the batch.

## No changes and retries

- Unchanged or transport-only updates do not create a catalog PR.
- Successfully reviewed no-change issues close without a synthetic release commit.
- Completed issues with content edits close when the catalog PR merges.
- Failed fetches or issue creation do not acknowledge a new watch-state baseline.
- Failed refreshes or checks leave unresolved work queued for the next night.
- One `nightly-refresh-failure` issue tracks the failure and links to the run.
- Reports are retained as the `nightly-refresh` artifact for seven days.
- Human commits on the automation branch are never force-overwritten.

## Local inspection and validation

Authenticate `gh`, then inspect the durable queue without editing skills or
calling a model:

```bash
GITHUB_REPOSITORY=owner/repository GH_TOKEN="$(gh auth token)" \
  python3 scripts/nightly_skill_refresh.py --dry-run
python3 -m unittest discover -s scripts/tests -p 'test_*.py'
python3 scripts/upstream_watch.py --validate-config
actionlint .github/workflows/upstream-watch.yml .github/workflows/catalog-check.yml .github/workflows/publish-catalog.yml
```

The dry-run report is written under `artifacts/nightly-refresh/`. A real refresh
must first run `bash scripts/sync_external_catalog_sources.sh`, then invoke
`nightly_skill_refresh.py`, validate the catalog, and publish the candidate.

GitHub Actions must allow creating pull requests with `GITHUB_TOKEN`. Reusable
checks are invoked explicitly because bot-generated PR events are not a reliable
unattended CI trigger; see [GitHub's event documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows).
