# TUI Polish — Tables, Toolbars, Graphs

Status: Proposed
Owner: nickprotop
Last updated: 2026-05-25
Follow-up to: PR #735 (SharpConsoleUI command center)

## Why

PR #735 landed a native SharpConsoleUI shell. After living with it, the recurring shape of the browse pages reads as spaghetti:

- 6 of 8 pages are the same stack: `tall PropertyPanel header → search chip → single 1-column ListControl → bottom Buttons`.
- Each list row crams 3 dimensions into a single string (`{marker} {alias}  [dim]{status}[/]  [grey]{reasons}[/]`).
- Primary actions are full-width `Button` controls *below* a 16-row scrollable list — off-screen on default terminals, visually indistinguishable from list rows.
- The PropertyPanel header duplicates information already in the top StatusBar.
- The two pages that did break the pattern (Installed = Table, Collections = master-detail) read substantially better. The unevenness is itself a signal.

This polish PR converts the rest of the shell to the patterns that already work, adds graph affordances we have but barely use, and splits the 2178-line shell file.

## Non-goals

- **No API changes** to SharpConsoleUI. This is a consumer-side polish; we use what's already published in v2.4.61.
- **No removal of modal flows.** Skills / Bundles / Packages / Agents keep their existing modal-on-Enter detail. Collections keeps its existing inline master-detail.
- **No NavigationView replacement.** The left rail stays. Tabs only appear *inside* a surface where they're additive (none in this PR — keeping scope tight).
- **No manifest changes.** `navigation-surfaces.json` is untouched; the surface map is preserved.
- **No backend changes.** `SkillInstaller`, `AgentInstaller`, `ProjectSkillRecommender`, catalog sync stay as-is.

## Scope summary

| # | Change | Why |
|---|---|---|
| 1 | Convert 5 ListControl pages to TableControl | Multi-column data wants columns + sorting, not markup-salad rows |
| 2 | Replace bottom Buttons with top ToolbarControl | Verbs above data, selection-aware enable/disable, standard convention |
| 3 | Thin one-line identity strip instead of tall PropertyPanel header | Reclaim ~4 vertical rows; remove duplication with StatusBar |
| 4 | Pin NavigationView PaneDisplayMode based on terminal width | Free horizontal space for detail on wide terminals |
| 5 | Split `InteractiveConsoleApp.Shell.cs` into 4 partials | File is 2178 lines, well past the 800-line ceiling |
| 6 | Add graphs everywhere they help (Home / Project / Analysis / Settings) | Underused control surface; gradients carry information glanceably |
| 7 | Section rhythm: RuleControl separators, recolor non-severity gradients | Cheap polish |

## Per-page conversion table

### Pages that change

| Page | Today | After |
|---|---|---|
| **Skills** | 1-col ListControl, escaped markup salad, modal on Enter | TableControl `Status / Skill / Collection / Lane / Version / Tokens`, sortable, modal on Enter, Toolbar `[Install ↵] [Force]` |
| **Bundles** | 1-col ListControl, `name [N skills, Xk tokens]`, modal on Enter | TableControl `Bundle / Title / Skills / Tokens`, sortable, modal on Enter, Toolbar `[Install ↵] [Details]` |
| **Packages** | 1-col ListControl, `signal [kind] -> skill [stack/lane] (Xk)`, modal on Enter | TableControl `Signal / Kind / Skill / Collection / Lane / Tokens`, sortable, modal on Enter (opens linked skill), Toolbar `[Open skill ↵]` |
| **Agents** | 1-col ListControl, `✓/○ alias [dim description]`, modal on Enter | TableControl `Status / Agent / Description / Skills`, sortable, modal on Enter, Toolbar `[Install ↵] [Remove] [Install all detected]` |
| **Project** | 1-col ListControl, `●●● alias [status] [reasons]`, install on Enter | TableControl `Confidence / Status / Skill / Reasons`, sortable, install on Enter, Toolbar `[Install selected ↵] [Install all ⌃I]`, header gets BarGraph confidence trio |
| **Installed** | Already TableControl ✓ | No table change. Header collapses to thin strip. Bottom Update/Remove buttons move to top Toolbar `[Install ↵] [Update ⌃U] [Reinstall] [Remove ⌃⌫]` (selection-aware) |
| **Home** | 5 metric cards + bullet panel | Same 5 metric cards, each with a SparklineControl footer. Adds one LineGraph "catalog over time" below the cards |
| **Analysis** | 2 BarGraph charts | Keep both, recolor heaviest-skills with smooth `cool` gradient (heavy ≠ severity). Add third chart: LineGraph token distribution across catalog |
| **Settings** | Inline form (dropdowns + refresh button) | Same form + BarGraph disk footprint per collection at the bottom |

### Pages that don't change

| Page | Why kept as-is |
|---|---|
| **Collections** | Already master-detail with HorizontalGrid + inline two-stage install — works. Left rail stays ListControl (narrow, single string per row) |
| **Command palette** | Popover of fuzzy-ranked single strings — that's literally what ListControl is for |
| **About** | Static info page; no interactive data |
| **Update All / Remove All confirmation pages** | Already minimal; benefit from the new thin header but no table conversion |

## Architecture & guiding rules

**The list-vs-table rule** — apply consistently across the shell:

> If the row has more than one logical column of data, it's a Table. If it's a single string with optional decoration, it's a List.

**The header rule:**

> The page's top-of-content identity strip is **one row** of markup with the page name + key counts. The StatusBar carries session identity (project / scope / platform / catalog version). Do not duplicate.

**The verbs rule:**

> Primary actions live in a `ToolbarControl` directly above the data, not as full-width buttons below it. Toolbar buttons enable/disable based on selection state. Bulk shortcuts (`⌃U`, `⌃I`, `⌃⌫`) keep working and fire the same handlers as the toolbar buttons.

**The graph rule:**

> Use BarGraphControl for single-metric weights, SparklineControl for inline trends (one row tall), LineGraphControl for multi-point trends. Gradients carry meaning: `cool`/`blue→cyan` for "magnitude, not severity"; threshold `green→yellow→red` reserved for "actual severity" (outdated counts, scan confidence).

## Detailed design per change

### 1. TableControl conversions

For each of Skills / Bundles / Packages / Agents / Project:

- Replace `StyledList(...)` builder call with `Controls.Table()` with the columns listed above.
- Tag preservation: `new TableRow(...) { Tag = entry }` to keep the `OnRowActivated` handler shape identical to today's `OnItemActivated`.
- Sortable by all columns via `WithSorting()`. Default sort:
  - Skills: by Collection then Skill (matches today's order)
  - Bundles: by Skills count desc
  - Packages: by Signal asc
  - Agents: by Status (installed first) then Agent
  - Project: by Confidence desc (matches today's order)
- Per-row foreground color for state: outdated → `AccentYellow`, installed → default fg. Same pattern as Installed page today.
- Narrow-terminal column hiding via `WithColumnVisibility` predicate — drop `Lane`, `Description`, `Reasons` at <100 cols to keep Status + Name + key metric visible.
- Right-justify all numeric columns (`Tokens`, `Skills`, `Version`).
- Filter (`/`) keeps working: the existing `MatchesFilter` predicate filters the source array before rows are added.

### 2. ToolbarControl

For each browse page, the page builder produces (in this order):

```
[ thin identity strip ]    ← one row markup
[ search chip if filter ]  ← optional
[ ToolbarControl ]         ← new — verbs go here
[ TableControl ]           ← data
```

ToolbarControl spec:
- Buttons with text + optional shortcut hint (e.g. `Update ⌃U`).
- Enabled state computed from current row selection:
  - **No selection** (table empty / no focus): only "Install" enabled (opens detail modal or no-op).
  - **Row selected, current**: Reinstall + Remove enabled; Update disabled.
  - **Row selected, outdated**: Update + Reinstall + Remove enabled.
- Re-evaluate on `OnSelectionChanged` (TableControl event).
- Click handlers reuse the existing methods (`UpdateSkillRecords`, `SkillInstaller.Install`, etc.) — no behavior change, just relocation.
- Bulk shortcuts `⌃U` / `⌃I` / `⌃⌫` still bound in StatusBar bottom bar and still call the bulk paths regardless of selection.

### 3. Thin identity strip

Replace every:

```csharp
panel.AddControl(BuildPropertyPanel("installed skills", AccentGreen,
    ("target", "..."),
    ("installed", "..."),
    ("outdated", "..."),
    ("tokens", "...")));
```

…with:

```csharp
panel.AddControl(new MarkupControl(new List<string>
{
    $"[bold {AccentGreenHex}]installed skills[/]  [grey50]·[/]  {installedCount}/{total}  [grey50]·[/]  {outdated} outdated  [grey50]·[/]  {tokens} tokens"
}));
```

One row instead of six. Same information density (counts come along), same accent color via inline markup.

Where the page has a sparkline or graph as part of identity (Home / Installed), the strip lives above and the graph immediately below.

### 4. NavigationView PaneDisplayMode

In `BuildShell`:

```csharp
var width = SafeConsole(() => Console.WindowWidth, 120);
var mode = width >= 130 ? NavigationViewDisplayMode.LeftCompact
         : width >=  90 ? NavigationViewDisplayMode.Top
                        : NavigationViewDisplayMode.Minimal;
nav.WithPaneDisplayMode(mode);
```

Today's `Auto` shows full pane labels at all widths, eating ~22 columns of horizontal space. `LeftCompact` shows icons-only; users can still hover/click. Recompute on terminal resize (subscribe to driver resize event if not already).

### 5. File split

`InteractiveConsoleApp.Shell.cs` (2178 lines) splits into:

| File | Contents | Approx lines |
|---|---|---|
| `InteractiveConsoleApp.Shell.cs` | Shell entry (`RunInteractiveShellAsync`), NavigationView setup, theme, status bars, palette, helpers (`BuildSectionPanel`, `BuildPropertyPanel`, `FormatRow`, `Toast`, `Escape`, `ConfirmModal`, `ShowModalNative`, `BuildCardGrid`) | ~600 |
| `InteractiveConsoleApp.Home.cs` | `BuildHomePage`, `BuildMetricCard`, metric-card sparkline helpers, "catalog over time" LineGraph builder | ~400 |
| `InteractiveConsoleApp.Catalog.cs` | `BuildSkillBrowserPage`, `BuildCollectionsPage` + `BuildCollectionDetail`, `BuildBundlesPage`, `BuildPackagesPage`, `BuildAgentsPage`, related modals | ~700 |
| `InteractiveConsoleApp.Workspace.cs` | `BuildInstalledPage`, `BuildProjectPage`, `BuildAnalysisPage`, `BuildSettingsPage`, `BuildRemoveAllPage`, `BuildUpdateAllPage`, `BuildAboutPage` | ~600 |

All partials of the same `InteractiveConsoleApp` class. No public API change.

### 6. Graphs

All four graph controls have full gradient APIs (verified against `SharpConsoleUI/Builders/`):

- `BarGraphControl`: `WithGradient(thresholds[])`, `WithStandardGradient()` (green→yellow→red), `WithSmoothGradient(ColorGradient | string | Color[])`
- `LineGraphControl`: per-series `AddSeries(name, color, gradient | gradientSpec)`, reference lines, value markers, high/low labels
- `SparklineControl`: `WithBarColor`, secondary/bidirectional data series

#### Home page

Each metric card grows a footer SparklineControl (1 row, inside the card padding):

| Card | Sparkline data | Gradient hint |
|---|---|---|
| skills | Skill count over recent catalog releases (from `GetReleasesAsync`) | bar color `AccentDeepSkyBlue` |
| bundles | Bundle count over recent catalog releases | `AccentTurquoise` |
| installed | Local install activity — count of installs per recent session (fall back to flat fill if no telemetry) | `AccentGreen` |
| outdated | Bidirectional sparkline: primary = outdated count, secondary = current count | primary `AccentYellow`, secondary `AccentGreen` |
| agents | Agent count over recent catalog releases | `AccentMediumPurple` |

Below the metric grid, a single full-width LineGraph (~6 rows):

```csharp
Controls.LineGraph()
    .WithTitle("catalog growth")
    .WithHeight(6)
    .AddSeries("skills", AccentDeepSkyBlue, "blue→cyan")
    .AddSeries("tokens", AccentMediumPurple, "purple→magenta")
    .WithData("skills", skillsByRelease)
    .WithData("tokens", tokensByRelease)
    .WithHighLowLabels(true)
    .WithYAxisLabels(true)
    .Build()
```

Data source: `GetReleasesAsync` returns historical releases; we cache the last N (e.g. 20) and extract `Skills.Count` + `TotalTokens` per release. If history is unavailable (offline, first run), the LineGraph falls back to a 1-point degenerate plot showing only the current value — no error, no empty box.

#### Project page

- Replace the inline `recommendations (N high · N med · N low)` markup line with a 3-bar `BarGraphControl` stack with `WithGradient` thresholds: `0% = grey, 33% = yellow, 66% = green` (high-confidence = green, lots of high = mostly green).
- Per-row Confidence column: a 6-cell BarGraphControl (no label, no value text) inside the TableControl cell, gradient `red→yellow→green` reversed (high = green). Falls back to the existing `●●●` glyph at <100 cols where the cell is too narrow.

#### Analysis page

- Keep "tokens by skill (top 12)" chart but swap `WithStandardGradient()` (severity gradient) for `WithSmoothGradient("blue→purple")`. Heavy ≠ unsafe.
- Keep "skills per collection (top 8)" chart; switch from flat `AccentTurquoise` to `WithSmoothGradient("teal→cyan")`.
- **New third chart**: full-width LineGraph showing token-count distribution across the entire catalog, X = skill index sorted by tokens desc, Y = tokens. Shows the long-tail shape. Use `"cool"` gradient.

#### Settings page

- After the existing form, add a BarGraph stack showing disk footprint per collection (sum of installed skill bytes — computable from `InstalledSkillRecord`). Uses `WithSmoothGradient("cool")`. Helps users see where their token budget is going.

### 7. Rhythm fixes

- Add `RuleControl(title: "Actions")` between detail-pane data and toolbar inside modals (the modal verbs today sit flush against the property panel).
- Recolor the "outdated row" foreground from `AccentYellow` to `Color(200,180,80)` so it stays warm but doesn't fight the new BarGraph threshold yellows on Project/Analysis pages.

## Commit ordering

Same structure as PR #735's branch — 6 self-contained commits, each reviewable independently.

1. **`refactor: split InteractiveConsoleApp.Shell.cs into 4 partials`** — pure file move, zero behavior change. Verify by running tests after; must still be 613/613.
2. **`feat(shell): thin identity strip replaces tall PropertyPanel headers`** — single-row markup header on every page; PropertyPanel is now detail-pane-only. Reclaims vertical space, sets up rooms for graphs.
3. **`feat(shell): ToolbarControl replaces bottom buttons across pages`** — selection-aware verbs at top of data area. Bulk shortcuts unchanged. Buttons removed from page bottoms.
4. **`feat(shell): TableControl on Skills / Bundles / Packages / Agents / Project`** — five list-to-table conversions. Modal-on-Enter preserved. Default sorts set per table.
5. **`feat(shell): graphs on Home / Project / Analysis / Settings`** — Sparklines on metric cards, LineGraph catalog growth, BarGraph confidence trio on Project, per-row confidence BarGraph cells, third Analysis chart, Settings disk-footprint chart, recolor existing Analysis charts.
6. **`feat(shell): responsive NavigationView pane mode + rhythm polish`** — pin `PaneDisplayMode` by terminal width, add RuleControl separators inside modals, recolor outdated foreground.

## Verification

After each commit:
- `dotnet build` clean across all four projects, 0 warnings (matches PR #735's bar).
- `dotnet test` — all 613 tests still pass.
- Manual: `dotnet run --project cli/ManagedCode.DotnetSkills -- ` and walk every NavigationView surface. Check at 3 terminal widths: 80 (narrow), 130 (medium), 200 (wide).
- Redirected stdio still falls through to `RunClassicShellAsync` (PR #735's behavior preserved).

## Open questions

None blocking. All structural questions resolved in conversation:
- ✅ NavigationView stays; no surface map changes
- ✅ Modals stay on Skills/Bundles/Packages/Agents
- ✅ TableControl everywhere it makes sense (5 list pages); ListControl stays on Collections-left-rail and Command Palette
- ✅ Graphs are in scope, with gradients
- ✅ Catalog history is available via `GetReleasesAsync` for the "catalog growth" LineGraph; if offline, degrade to a 1-point plot

## File map

| New / changed file | Status | Approx delta |
|---|---|---|
| `cli/ManagedCode.DotnetSkills/InteractiveConsoleApp.Shell.cs` | Split + reworked | −~1500 / +~600 |
| `cli/ManagedCode.DotnetSkills/InteractiveConsoleApp.Home.cs` | New | +~400 |
| `cli/ManagedCode.DotnetSkills/InteractiveConsoleApp.Catalog.cs` | New | +~700 |
| `cli/ManagedCode.DotnetSkills/InteractiveConsoleApp.Workspace.cs` | New | +~600 |
| `cli/ManagedCode.DotnetSkills/ManagedCode.DotnetSkills.csproj` | No change | 0 |

Net diff: roughly +800 lines additive (the graphs and the toolbars), and a large internal redistribution. No public API change to dotnet-skills or to SharpConsoleUI.
