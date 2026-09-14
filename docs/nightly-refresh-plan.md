# Nightly catalog refresh

## Contract

Run upstream monitoring and catalog refresh at 00:17 UTC. Keep the last successfully
applied watch baseline outside Git; issues are only for failures. Import external skills verbatim; refresh
repo-owned guidance only from configured authoritative sources. Create one
automation PR for real catalog changes. Validate its immutable head through the
same catalog, Waza, build, test, pack, and smoke checks as normal PRs. Merge only
that head, only while its base is still current. The 04:00 UTC release publishes
unreleased commits; an unchanged night produces neither a PR nor a release.

```mermaid
flowchart TD
  Watch[00:17 UTC upstream watch] --> Queue[Pending changes artifact]
  Queue --> Refresh[Refresh affected skills and vendir imports]
  Refresh --> Changed{Catalog changed?}
  Changed -->|No| Quiet[No PR or release]
  Changed -->|Yes| PR[One nightly refresh PR]
  PR --> Checks[Reusable PR Checks on exact head SHA]
  Checks -->|Pass and base unchanged| Merge[Merge verified head]
  Checks -->|Fail| Failure[Failure issue and retry next night]
  Refresh -->|Fail| Failure
  Merge --> Release[04:00 UTC catalog, NuGet and Pages release]
```

## Implementation checklist

- [x] Replace manual-only maintenance policy with nightly validated automation.
- [x] Implement durable pending-work selection and scoped skill refresh.
- [ ] Connect the selected content provider and verify a real repo-owned skill refresh; provider choice and credential are pending.
- [x] Implement idempotent PR creation, immutable-head merge and failure issues.
- [x] Reuse PR checks and lock vendir verification to committed snapshots.
- [x] Document updater contract, no-op behavior, retries and release ordering.
- [ ] Document and wire the selected provider credential.
- [x] Run regression tests, catalog/watch validation, workflow lint and live dry run.
  Live source validation and remote PR Checks passed before the failure-only
  issue correction; regression tests and CI are rerun for the revised flow.
- [x] Commit and push draft PR #1573.
- [ ] Verify final GitHub checks and a live nightly run after provider setup.

## Validation matrix

Regression tests cover no changes, state-only changes, repeated pending changes,
imported ownership, unknown skills, refresh failure, unsafe output paths, PR reuse,
changed PR head/base and deduplicated failure reporting. Check workflow syntax with
actionlint. Run Python regression tests and source validators. Run real PR Checks
remotely to cover Waza and .NET packaging. Provider-dependent work requires the
selected provider credential; never report mocked output as a live AI refresh.

## Design decisions

Use one shared runner instead of copying the same updater into every skill.
Skill-specific behavior, when necessary, belongs alongside that skill. Existing
watch mappings select affected skills. The applied baseline advances only after
success, so failed refreshes are detected again without an issue-based queue.
Bot PRs explicitly invoke reusable checks instead of relying on recursive
`GITHUB_TOKEN` events to start CI.
