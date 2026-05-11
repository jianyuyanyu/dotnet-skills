// -----------------------------------------------------------------------------
// SharpConsoleUI command center — the retained-mode, windowed interactive shell.
//
// This is the default surface for the bare `dotnet skills` (and `agents`)
// invocation. It replaces the prompt-first Spectre loop in
// InteractiveConsoleApp.cs with a NavigationView-driven shell:
//   * each former Show* screen is a NavigationView page
//   * Spectre renderables built by the existing BuildRich* helpers are hosted
//     in SpectreRenderableControl
//   * SelectionPrompt/Confirm flows become ListControl activation + modal
//     windows with ButtonControls
//   * mutating actions call the Runtime installers directly and re-render the
//     affected page in place
//
// The classic prompt loop survives as RunClassicShellAsync and is used as a
// fallback when stdin/stdout is redirected (CI, pipes, dumb terminals).
// -----------------------------------------------------------------------------

using ManagedCode.DotnetSkills.Runtime;
using SharpConsoleUI;
using SharpConsoleUI.Builders;
using SharpConsoleUI.Controls;
using SharpConsoleUI.Drivers;
using SharpConsoleUI.Helpers;
using SharpConsoleUI.Layout;
using SharpConsoleUI.Themes;
using SpectreRendering = Spectre.Console.Rendering;

namespace ManagedCode.DotnetSkills;

internal sealed partial class InteractiveConsoleApp
{
    // One selection treatment for every list mode (keyboard-highlight, mouse-hover, click).
    // The list control otherwise renders three subtly different states: HighlightBackgroundColor
    // for the focused selection, the theme's ListHoverBackgroundColor for mouse hover, and the
    // theme's ListUnfocusedHighlightBackgroundColor when the list does not hold focus. We pin all
    // of them so the bar looks the same regardless of how the row was reached.
    private static readonly Color SelectionBg = new(150, 205, 255);
    private static readonly Color SelectionFg = Color.Black;
    private static readonly Color UnfocusedSelectionBg = new(44, 62, 92);
    private static readonly Color UnfocusedSelectionFg = new(205, 218, 236);
    private static readonly Color ShortcutAccent = new(130, 205, 255);

    // Live shell state for the dynamic status bar.
    private ConsoleWindowSystem? _ws;
    private ScrollablePanelControl? _activePanel;
    private HomeAction? _currentPage;
    private StatusBarControl? _statusBar;
    private StatusBarItem? _clockItem;
    private StatusBarItem? _statusMessage;

    private static readonly Color[] SectionPalette =
    {
        new(120, 180, 255),
        new(120, 220, 160),
        new(220, 170, 110),
        new(195, 150, 230),
        new(235, 150, 150),
        new(150, 210, 220),
    };

    /// <summary>
    /// Entry point for the bare interactive invocation. Launches the SharpConsoleUI
    /// command center; falls back to the classic prompt loop when there is no real terminal.
    /// </summary>
    public async Task<int> RunAsync()
    {
        if (Console.IsInputRedirected || Console.IsOutputRedirected)
        {
            return await RunClassicShellAsync();
        }

        try
        {
            toolUpdateStatus = await getToolUpdateStatusAsync(cachePath);
            await LoadCatalogsAsync(refreshCatalog: false);
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine($"Failed to load the skill catalog: {exception.Message}");
            return 1;
        }

        try
        {
            var windowSystem = new ConsoleWindowSystem(new NetConsoleDriver(RenderMode.Buffer), BuildTheme());
            windowSystem.PanelStateService.ShowTopPanel = true;
            windowSystem.PanelStateService.ShowBottomPanel = false; // replaced by the interactive StatusBarControl
            windowSystem.PanelStateService.TopStatus = $"dotnet skills v{ToolVersionInfo.CurrentVersion} · command center";

            CreateCommandCenter(windowSystem);
            windowSystem.Run();
            return 0;
        }
        catch (Exception exception)
        {
            Console.Clear();
            ExceptionFormatter.WriteException(exception);
            return 1;
        }
    }

    private void CreateCommandCenter(ConsoleWindowSystem ws)
    {
        _ws = ws;

        var installedCount = SafeCount(GetInstalledSkillCount);
        var outdatedCount = SafeCount(GetOutdatedSkillCount);
        var actions = GetHomeActions(installedCount, outdatedCount)
            .Where(action => action.Action != HomeAction.Exit)
            .ToArray();

        var nav = Controls.NavigationView()
            .WithNavWidth(30)
            .WithPaneHeader("[bold rgb(120,180,255)]  ◆  dotnet skills[/]")
            .WithPaneDisplayMode(NavigationViewDisplayMode.Auto)
            .WithExpandedThreshold(96)
            .WithCompactThreshold(54)
            .WithContentBorder(BorderStyle.Rounded)
            .WithContentBorderColor(new Color(70, 100, 150))
            .WithContentPadding(1, 0, 1, 0)
            .WithContentHeader(true)
            .WithSelectedColors(Color.White, new Color(40, 80, 160))
            .AddItem(new NavigationItem("Home", icon: "◈", subtitle: "Session & telemetry"), panel => BuildHomePage(ws, panel));

        var sectionIndex = 0;
        foreach (var section in actions.GroupBy(action => action.Section))
        {
            var color = SectionPalette[sectionIndex++ % SectionPalette.Length];
            nav = nav.AddHeader(section.Key, color, header =>
            {
                foreach (var action in section)
                {
                    var captured = action;
                    header.AddItem(
                        new NavigationItem(captured.Label, icon: "›", subtitle: captured.Summary) { Tag = captured.Action },
                        panel => BuildActionPage(ws, panel, captured.Action));
                }
            });
        }

        var navView = nav
            .OnSelectedItemChanged((_, e) => RebuildStatusBar(e.NewItem?.Tag as HomeAction?))
            .WithAlignment(HorizontalAlignment.Stretch)
            .WithVerticalAlignment(VerticalAlignment.Fill)
            .Build();

        _statusBar = new StatusBarControl(stickyBottom: true)
        {
            HorizontalAlignment = HorizontalAlignment.Stretch,
            BackgroundColor = Color.Transparent,
            ShortcutForegroundColor = ShortcutAccent,
            SeparatorChar = "·",
            ShortcutLabelSeparator = " ",
        };

        new WindowBuilder(ws)
            .WithTitle("dotnet skills — command center")
            .HideTitle()
            .Maximized()
            .Movable(false)
            .Resizable(false)
            .HideTitleButtons()
            .WithBorderStyle(BorderStyle.Rounded)
            .WithBorderColor(new Color(70, 88, 116))
            .WithAsyncWindowThread(ClockLoopAsync)
            .OnKeyPressed((_, e) => HandleGlobalKey(e))
            .OnClosed((_, _) => ws.Shutdown(0))
            .AddControl(navView)
            .AddControl(_statusBar)
            .BuildAndShow();

        RebuildStatusBar(null);
    }

    private void HandleGlobalKey(KeyPressedEventArgs e)
    {
        var key = e.KeyInfo;
        if (key.Key == ConsoleKey.Escape)
        {
            // Root window: Esc ends the session rather than dismissing the window.
            _ws?.Shutdown(0);
            e.Handled = true;
            return;
        }

        if ((key.Modifiers & ConsoleModifiers.Control) == 0)
        {
            return;
        }

        switch (key.Key)
        {
            case ConsoleKey.R:
                RefreshCatalogFromUi();
                e.Handled = true;
                break;
            case ConsoleKey.U when _currentPage == HomeAction.ManageInstalled:
                UpdateAllOutdatedFromUi();
                e.Handled = true;
                break;
            case ConsoleKey.I when _currentPage == HomeAction.SyncProject:
                InstallAllRecommendedFromUi();
                e.Handled = true;
                break;
        }
    }

    // -------------------------------------------------------------------------
    // Page dispatch
    // -------------------------------------------------------------------------

    private void BuildActionPage(ConsoleWindowSystem ws, ScrollablePanelControl panel, HomeAction action)
    {
        _activePanel = panel;
        _currentPage = action;
        switch (action)
        {
            case HomeAction.BrowseSkills: BuildSkillBrowserPage(ws, panel); break;
            case HomeAction.ManageInstalled: BuildInstalledPage(ws, panel); break;
            case HomeAction.BrowseCollections: BuildCollectionsPage(ws, panel); break;
            case HomeAction.BrowseBundles: BuildBundlesPage(ws, panel, primaryOnly: true); break;
            case HomeAction.BrowsePackages: BuildBundlesPage(ws, panel, primaryOnly: false); break;
            case HomeAction.BrowseAgents: BuildAgentsPage(ws, panel); break;
            case HomeAction.SyncProject: BuildProjectPage(ws, panel); break;
            case HomeAction.Analysis: BuildAnalysisPage(ws, panel); break;
            case HomeAction.RemoveAll: BuildRemoveAllPage(ws, panel); break;
            case HomeAction.UpdateAll: BuildUpdateAllPage(ws, panel); break;
            case HomeAction.Workspace: BuildSettingsPage(ws, panel); break;
            case HomeAction.About: BuildAboutPage(panel); break;
            default:
                panel.ClearContents();
                panel.AddControl(new SpectreRenderableControl(BuildRichShellPanel(action.ToString(), new Spectre.Console.Markup("[dim]Not available in this surface.[/]"))));
                break;
        }
    }

    // -------------------------------------------------------------------------
    // Home
    // -------------------------------------------------------------------------

    private void BuildHomePage(ConsoleWindowSystem ws, ScrollablePanelControl panel)
    {
        _activePanel = panel;
        _currentPage = null;
        panel.ClearContents();

        var layout = ResolveSkillLayout();
        var installer = new SkillInstaller(skillCatalog);
        var installed = SafeGet(() => installer.GetInstalledSkills(layout), Array.Empty<InstalledSkillRecord>());
        var outdated = installed.Count(record => !record.IsCurrent);

        var session = BuildRichPropertyGrid(
            ("catalog", $"{Escape(skillCatalog.SourceLabel)} [dim]({Escape(skillCatalog.CatalogVersion)})[/]"),
            ("platform", Escape(Session.Agent.ToString())),
            ("scope", Escape(Session.Scope.ToString())),
            ("project", Escape(CompactPath(Session.ProjectDirectory ?? Environment.CurrentDirectory))),
            ("target", $"[dim]{Escape(CompactPath(layout.PrimaryRoot.FullName))}[/]"));

        var telemetry = BuildRichCardGrid(new SpectreRendering.IRenderable[]
        {
            BuildRichMetricCard("skills", skillCatalog.Skills.Count.ToString(), "in catalog", "deepskyblue1"),
            BuildRichMetricCard("bundles", GetPrimaryBundles().Count.ToString(), "focused", "turquoise2"),
            BuildRichMetricCard("installed", $"{installed.Count}/{skillCatalog.Skills.Count}", "in current target", installed.Count > 0 ? "green" : "grey"),
            BuildRichMetricCard("outdated", outdated.ToString(), outdated == 0 ? "all current" : "need update", outdated == 0 ? "green" : "yellow"),
            BuildRichMetricCard("agents", agentCatalog.Agents.Count.ToString(), "orchestration", "mediumpurple2"),
        }, maxColumns: 3);

        var quickStart = BuildRichDetailCard("quick start", "deepskyblue1",
            "[dim]Use the rail on the left to browse and install.[/]",
            "[grey]Skills[/] [dim]browse and install individual catalog skills[/]",
            "[grey]Installed[/] [dim]update or remove what is already installed[/]",
            "[grey]Project[/] [dim]scan the current solution and install recommended skills[/]",
            "[grey]Agents[/] [dim]install orchestration agents into native agent directories[/]");

        var parts = new List<SpectreRendering.IRenderable>
        {
            BuildRichShellPanel("session", session),
            BuildRichShellPanel("catalog telemetry", telemetry),
        };

        var update = BuildToolUpdatePanel(toolUpdateStatus);
        if (update is not null)
        {
            parts.Add(update);
        }

        parts.Add(quickStart);

        panel.AddControl(new SpectreRenderableControl(BuildRichStack(parts.ToArray())));
    }

    // -------------------------------------------------------------------------
    // Skill browser
    // -------------------------------------------------------------------------

    private void BuildSkillBrowserPage(ConsoleWindowSystem ws, ScrollablePanelControl panel)
    {
        panel.ClearContents();

        var layout = ResolveSkillLayout();
        var installer = new SkillInstaller(skillCatalog);
        var installed = SafeGet(() => installer.GetInstalledSkills(layout), Array.Empty<InstalledSkillRecord>());
        var available = skillCatalog.Skills
            .Where(skill => installed.All(record => !string.Equals(record.Skill.Name, skill.Name, StringComparison.OrdinalIgnoreCase)))
            .OrderBy(skill => CatalogOrganization.GetStackRank(skill.Stack))
            .ThenBy(skill => skill.Stack, StringComparer.Ordinal)
            .ThenBy(skill => skill.Name, StringComparer.Ordinal)
            .ToArray();

        var summary = BuildRichPropertyGrid(
            ("catalog", $"{Escape(skillCatalog.SourceLabel)} [dim]({Escape(skillCatalog.CatalogVersion)})[/]"),
            ("target", $"[dim]{Escape(CompactPath(layout.PrimaryRoot.FullName))}[/]"),
            ("available", available.Length.ToString()),
            ("installed", $"{installed.Count}/{skillCatalog.Skills.Count}"));
        panel.AddControl(new SpectreRenderableControl(BuildRichShellPanel("skill browser", summary, "turquoise2")));

        if (available.Length == 0)
        {
            panel.AddControl(new SpectreRenderableControl(BuildRichShellPanel("available", new Spectre.Console.Markup("[dim]Every catalog skill is already installed in this target.[/]"))));
            return;
        }

        var list = StyledList("Available skills (Enter for details)")
            .MaxVisibleItems(16)
            .WithScrollbarVisibility(ScrollbarVisibility.Auto);
        foreach (var skill in available)
        {
            list.AddItem(BuildSkillChoiceLabel(skill, installed), skill);
        }
        list.OnItemActivated((_, item) =>
        {
            if (item.Tag is SkillEntry skill)
            {
                ShowSkillDetailModal(ws, panel, skill);
            }
        });
        panel.AddControl(list.Build());
    }

    private void ShowSkillDetailModal(ConsoleWindowSystem ws, ScrollablePanelControl owner, SkillEntry skill)
    {
        var detail = BuildRichStack(
            BuildRichShellPanel(ToAlias(skill.Name), BuildRichPropertyGrid(
                ("skill", Escape(skill.Name)),
                ("collection", Escape(skill.Stack)),
                ("lane", Escape(skill.Lane)),
                ("version", Escape(skill.Version)),
                ("tokens", FormatTokenCount(skill.TokenCount))), "turquoise2"),
            BuildRichShellPanel("summary", new Spectre.Console.Markup(Escape(skill.Description))),
            BuildRichShellPanel("preview", new Spectre.Console.Markup(Escape(LoadSkillPreview(skill)))));

        ShowModal(ws, $"Skill · {ToAlias(skill.Name)}", detail,
            ("Install into current target", () =>
            {
                var summary = SafeGet(() => new SkillInstaller(skillCatalog).Install(new[] { skill }, ResolveSkillLayout(), force: false), default(SkillInstallSummary));
                Toast(summary is null
                    ? $"Install failed for {ToAlias(skill.Name)}"
                    : $"{ToAlias(skill.Name)}: {summary.InstalledCount} written, {summary.SkippedExisting.Count} skipped");
                BuildSkillBrowserPage(ws, owner);
            }),
            ("Force reinstall", () =>
            {
                var summary = SafeGet(() => new SkillInstaller(skillCatalog).Install(new[] { skill }, ResolveSkillLayout(), force: true), default(SkillInstallSummary));
                Toast(summary is null ? $"Install failed for {ToAlias(skill.Name)}" : $"{ToAlias(skill.Name)}: reinstalled ({summary.InstalledCount} written)");
                BuildSkillBrowserPage(ws, owner);
            }));
    }

    // -------------------------------------------------------------------------
    // Installed skills
    // -------------------------------------------------------------------------

    private void BuildInstalledPage(ConsoleWindowSystem ws, ScrollablePanelControl panel)
    {
        panel.ClearContents();

        var layout = ResolveSkillLayout();
        var installer = new SkillInstaller(skillCatalog);
        var installed = SafeGet(() => installer.GetInstalledSkills(layout), Array.Empty<InstalledSkillRecord>())
            .OrderBy(record => record.Skill.Name, StringComparer.Ordinal)
            .ToArray();
        var outdated = installed.Where(record => !record.IsCurrent).ToArray();

        var summary = BuildRichPropertyGrid(
            ("target", $"[dim]{Escape(CompactPath(layout.PrimaryRoot.FullName))}[/]"),
            ("installed", installed.Length.ToString()),
            ("outdated", outdated.Length == 0 ? "[green]0[/]" : $"[yellow]{outdated.Length}[/]"),
            ("tokens", FormatTokenCount(installed.Sum(record => record.Skill.TokenCount))));
        panel.AddControl(new SpectreRenderableControl(BuildRichShellPanel("installed skills", summary, "green")));

        if (installed.Length == 0)
        {
            panel.AddControl(new SpectreRenderableControl(BuildRichShellPanel("installed", new Spectre.Console.Markup("[dim]No catalog skills are installed in this target yet. Visit the Skills page to add some.[/]"))));
            return;
        }

        var list = StyledList("Installed skills (Enter for details)")
            .MaxVisibleItems(14)
            .WithScrollbarVisibility(ScrollbarVisibility.Auto);
        foreach (var record in installed)
        {
            list.AddItem((record.IsCurrent ? "✓ " : "↻ ") + BuildInstalledSkillChoiceLabel(record), record);
        }
        list.OnItemActivated((_, item) =>
        {
            if (item.Tag is InstalledSkillRecord record)
            {
                ShowInstalledSkillModal(ws, panel, record);
            }
        });
        panel.AddControl(list.Build());

        if (outdated.Length > 0)
        {
            panel.AddControl(Controls.Button($"Update all {outdated.Length} outdated skill(s)")
                .OnClick((_, _) =>
                {
                    var summaryText = UpdateSkillRecords(outdated);
                    Toast(summaryText);
                    BuildInstalledPage(ws, panel);
                }).Build());
        }

        panel.AddControl(Controls.Button($"Remove all {installed.Length} installed skill(s)")
            .OnClick((_, _) => ConfirmModal(ws, "Remove all installed skills?",
                $"This removes every catalog skill from {layout.PrimaryRoot.FullName}.",
                () =>
                {
                    var summary = SafeGet(() => new SkillInstaller(skillCatalog).Remove(installed.Select(r => r.Skill).ToArray(), layout), default(SkillRemoveSummary));
                    Toast(summary is null ? "Remove failed" : $"Removed {summary.RemovedCount} skill(s)");
                    BuildInstalledPage(ws, panel);
                })).Build());
    }

    private void ShowInstalledSkillModal(ConsoleWindowSystem ws, ScrollablePanelControl owner, InstalledSkillRecord record)
    {
        var detail = BuildRichStack(
            BuildRichShellPanel(ToAlias(record.Skill.Name), BuildRichPropertyGrid(
                ("skill", Escape(record.Skill.Name)),
                ("collection", Escape($"{record.Skill.Stack} / {record.Skill.Lane}")),
                ("installed", Escape(record.InstalledVersion)),
                ("latest", Escape(record.Skill.Version)),
                ("status", record.IsCurrent ? "[green]✓ current[/]" : "[yellow]↻ update available[/]"),
                ("tokens", FormatTokenCount(record.Skill.TokenCount))), "green"),
            BuildRichShellPanel("summary", new Spectre.Console.Markup(Escape(record.Skill.Description))));

        var buttons = new List<(string, Action)>();
        if (!record.IsCurrent)
        {
            buttons.Add(($"Update to {record.Skill.Version}", () =>
            {
                Toast(UpdateSkillRecords(new[] { record }));
                BuildInstalledPage(ws, owner);
            }));
        }
        buttons.Add(("Reinstall (force)", () =>
        {
            var summary = SafeGet(() => new SkillInstaller(skillCatalog).Install(new[] { record.Skill }, ResolveSkillLayout(), force: true), default(SkillInstallSummary));
            Toast(summary is null ? "Reinstall failed" : $"{ToAlias(record.Skill.Name)}: reinstalled");
            BuildInstalledPage(ws, owner);
        }));
        buttons.Add(("Remove", () => ConfirmModal(ws, $"Remove {ToAlias(record.Skill.Name)}?", $"Deletes the skill directory from {ResolveSkillLayout().PrimaryRoot.FullName}.", () =>
        {
            var summary = SafeGet(() => new SkillInstaller(skillCatalog).Remove(new[] { record.Skill }, ResolveSkillLayout()), default(SkillRemoveSummary));
            Toast(summary is null ? "Remove failed" : $"Removed {ToAlias(record.Skill.Name)}");
            BuildInstalledPage(ws, owner);
        })));

        ShowModal(ws, $"Installed · {ToAlias(record.Skill.Name)}", detail, buttons.ToArray());
    }

    private string UpdateSkillRecords(IReadOnlyList<InstalledSkillRecord> records)
    {
        var layout = ResolveSkillLayout();
        var skills = records.Select(record => record.Skill).ToArray();
        var summary = SafeGet(() => new SkillInstaller(skillCatalog).Install(skills, layout, force: true), default(SkillInstallSummary));
        return summary is null ? "Update failed" : $"Updated {summary.InstalledCount} skill(s)";
    }

    // -------------------------------------------------------------------------
    // Collections
    // -------------------------------------------------------------------------

    private void BuildCollectionsPage(ConsoleWindowSystem ws, ScrollablePanelControl panel)
    {
        panel.ClearContents();

        var layout = ResolveSkillLayout();
        var installer = new SkillInstaller(skillCatalog);
        var installed = SafeGet(() => installer.GetInstalledSkills(layout), Array.Empty<InstalledSkillRecord>());
        var views = BuildCollectionViews(installed)
            .OrderBy(view => CatalogOrganization.GetStackRank(view.Collection))
            .ThenBy(view => view.Collection, StringComparer.Ordinal)
            .ToArray();

        var summary = BuildRichPropertyGrid(
            ("catalog", $"{Escape(skillCatalog.SourceLabel)} [dim]({Escape(skillCatalog.CatalogVersion)})[/]"),
            ("collections", views.Length.ToString()),
            ("skills", skillCatalog.Skills.Count.ToString()),
            ("installed", $"{installed.Count}/{skillCatalog.Skills.Count}"));
        panel.AddControl(new SpectreRenderableControl(BuildRichShellPanel("collection browser", summary)));

        if (views.Length == 0)
        {
            panel.AddControl(new SpectreRenderableControl(BuildRichShellPanel("collections", new Spectre.Console.Markup("[dim]No collections in this catalog version.[/]"))));
            return;
        }

        var cards = views.Select(view => (SpectreRendering.IRenderable)BuildRichDetailCard(
            view.Collection, "deepskyblue1",
            $"[dim]lanes[/] {view.Lanes.Count}  [dim]skills[/] {view.InstalledCount}/{view.SkillCount}  [dim]tokens[/] {FormatTokenCount(view.TokenCount)}",
            $"[grey]{Escape(string.Join(", ", view.Lanes.Take(6).Select(lane => lane.Lane)))}[/]")).ToArray();
        panel.AddControl(new SpectreRenderableControl(BuildRichShellPanel("overview", BuildRichCardGrid(cards, maxColumns: 2))));

        var list = StyledList("Collections (Enter to install the whole collection)")
            .MaxVisibleItems(14)
            .WithScrollbarVisibility(ScrollbarVisibility.Auto);
        foreach (var view in views)
        {
            list.AddItem(BuildCollectionChoiceLabel(view), view);
        }
        list.OnItemActivated((_, item) =>
        {
            if (item.Tag is CollectionCatalogView view)
            {
                ConfirmModal(ws, $"Install collection {view.Collection}?",
                    $"Installs all {view.SkillCount} skill(s) from this collection into {ResolveSkillLayout().PrimaryRoot.FullName}.",
                    () =>
                    {
                        var skills = SafeGet(() => new SkillInstaller(skillCatalog).SelectSkillsFromCollections(new[] { view.Collection }), Array.Empty<SkillEntry>());
                        var summary = skills.Count == 0 ? null : SafeGet(() => new SkillInstaller(skillCatalog).Install(skills, ResolveSkillLayout(), force: false), default(SkillInstallSummary));
                        Toast(summary is null ? $"Could not install collection {view.Collection}" : $"{view.Collection}: {summary.InstalledCount} written, {summary.SkippedExisting.Count} skipped");
                        BuildCollectionsPage(ws, panel);
                    });
            }
        });
        panel.AddControl(list.Build());
    }

    // -------------------------------------------------------------------------
    // Bundles / packages
    // -------------------------------------------------------------------------

    private void BuildBundlesPage(ConsoleWindowSystem ws, ScrollablePanelControl panel, bool primaryOnly)
    {
        panel.ClearContents();

        var packages = (primaryOnly
                ? GetPrimaryBundles()
                : skillCatalog.Packages.OrderBy(p => p.Name, StringComparer.Ordinal).ToArray())
            .ToArray();
        var title = primaryOnly ? "focused bundles" : "catalog packages";
        var skillTokens = skillCatalog.Skills.ToDictionary(skill => skill.Name, skill => skill.TokenCount, StringComparer.OrdinalIgnoreCase);

        var summary = BuildRichPropertyGrid(
            ("catalog", $"{Escape(skillCatalog.SourceLabel)} [dim]({Escape(skillCatalog.CatalogVersion)})[/]"),
            (primaryOnly ? "bundles" : "packages", packages.Length.ToString()),
            ("skills covered", skillCatalog.Skills.Count.ToString()));
        panel.AddControl(new SpectreRenderableControl(BuildRichShellPanel(title, summary)));

        if (packages.Length == 0)
        {
            panel.AddControl(new SpectreRenderableControl(BuildRichShellPanel(title, new Spectre.Console.Markup("[dim]Nothing available in this catalog version.[/]"))));
            return;
        }

        var list = StyledList($"{(primaryOnly ? "Bundles" : "Packages")} (Enter for details)")
            .MaxVisibleItems(16)
            .WithScrollbarVisibility(ScrollbarVisibility.Auto);
        foreach (var package in packages)
        {
            var tokenCount = package.Skills.Sum(name => skillTokens.TryGetValue(name, out var value) ? value : 0);
            list.AddItem($"{package.Name}  [dim]({package.Skills.Count} skills, {FormatTokenCount(tokenCount)} tokens)[/]", package);
        }
        list.OnItemActivated((_, item) =>
        {
            if (item.Tag is SkillPackageEntry package)
            {
                ShowBundleModal(ws, panel, package, primaryOnly);
            }
        });
        panel.AddControl(list.Build());
    }

    private void ShowBundleModal(ConsoleWindowSystem ws, ScrollablePanelControl owner, SkillPackageEntry package, bool primaryOnly)
    {
        var detail = BuildRichStack(
            BuildRichShellPanel(package.Name, BuildRichPropertyGrid(
                ("package", Escape(package.Name)),
                ("title", Escape(package.Title)),
                ("skills", package.Skills.Count.ToString()),
                ("includes", Escape(string.Join(", ", package.Skills.Take(10).Select(ToAlias))))), "turquoise2"),
            BuildRichShellPanel("summary", new Spectre.Console.Markup(Escape(package.Description))));

        ShowModal(ws, $"Bundle · {package.Name}", detail,
            ("Install bundle into current target", () =>
            {
                var skills = SafeGet(() => new SkillInstaller(skillCatalog).SelectSkillsFromPackages(new[] { package.Name }), Array.Empty<SkillEntry>());
                var summary = skills.Count == 0 ? null : SafeGet(() => new SkillInstaller(skillCatalog).Install(skills, ResolveSkillLayout(), force: false), default(SkillInstallSummary));
                Toast(summary is null ? $"Could not install bundle {package.Name}" : $"{package.Name}: {summary.InstalledCount} written, {summary.SkippedExisting.Count} skipped");
                BuildBundlesPage(ws, owner, primaryOnly);
            }));
    }

    // -------------------------------------------------------------------------
    // Agents
    // -------------------------------------------------------------------------

    private void BuildAgentsPage(ConsoleWindowSystem ws, ScrollablePanelControl panel)
    {
        panel.ClearContents();

        var layout = TryResolveAgentLayout(out var layoutError);
        var installer = new AgentInstaller(agentCatalog);
        var installed = layout is null
            ? Array.Empty<InstalledAgentRecord>()
            : SafeGet(() => installer.GetInstalledAgents(layout), Array.Empty<InstalledAgentRecord>());

        var summary = BuildRichPropertyGrid(
            ("agents", agentCatalog.Agents.Count.ToString()),
            ("platform", Escape(Session.Agent.ToString())),
            ("target", layout is null ? $"[red]{Escape(layoutError ?? "unresolved")}[/]" : $"[dim]{Escape(CompactPath(layout.PrimaryRoot.FullName))}[/]"),
            ("installed", layout is null ? "[grey]-[/]" : $"{installed.Count}/{agentCatalog.Agents.Count}"));
        panel.AddControl(new SpectreRenderableControl(BuildRichShellPanel("orchestration agents", summary, "mediumpurple2")));

        if (agentCatalog.Agents.Count == 0)
        {
            panel.AddControl(new SpectreRenderableControl(BuildRichShellPanel("agents", new Spectre.Console.Markup("[dim]No agents available in the catalog.[/]"))));
            return;
        }

        var list = StyledList("Agents (Enter for details)")
            .MaxVisibleItems(14)
            .WithScrollbarVisibility(ScrollbarVisibility.Auto);
        foreach (var agent in agentCatalog.Agents.OrderBy(a => a.Name, StringComparer.Ordinal))
        {
            var isInstalled = installed.Any(i => string.Equals(i.Agent.Name, agent.Name, StringComparison.OrdinalIgnoreCase));
            list.AddItem($"{(isInstalled ? "✓ " : "○ ")}{ToAlias(agent.Name)}  [dim]{Escape(CompactDescription(agent.Description))}[/]", agent);
        }
        list.OnItemActivated((_, item) =>
        {
            if (item.Tag is AgentEntry agent)
            {
                ShowAgentModal(ws, panel, agent);
            }
        });
        panel.AddControl(list.Build());

        if (layout is null)
        {
            panel.AddControl(new SpectreRenderableControl(BuildRichShellPanel("note", new Spectre.Console.Markup("[yellow]No native agent directory resolved. Set the platform on the Settings page, or create one of .codex/.claude/.github/.gemini/.junie.[/]"))));
            return;
        }

        panel.AddControl(Controls.Button("Install all agents into detected native directories")
            .OnClick((_, _) =>
            {
                var detected = SafeGet(() => AgentInstallTarget.ResolveAllDetected(Session.ProjectDirectory, Session.Scope), Array.Empty<AgentInstallLayout>());
                if (detected.Count == 0)
                {
                    Toast("No native agent directories detected");
                    return;
                }
                var summary2 = SafeGet(() => new AgentInstaller(agentCatalog).InstallToMultiple(agentCatalog.Agents, detected, force: false), default(AgentInstallSummary));
                Toast(summary2 is null ? "Install failed" : $"Installed {summary2.InstalledCount} agent file(s) across {detected.Count} platform(s)");
                BuildAgentsPage(ws, panel);
            }).Build());
    }

    private void ShowAgentModal(ConsoleWindowSystem ws, ScrollablePanelControl owner, AgentEntry agent)
    {
        var detail = BuildRichStack(
            BuildRichShellPanel(ToAlias(agent.Name), BuildRichPropertyGrid(
                ("agent", Escape(agent.Name)),
                ("skills", agent.Skills.Count == 0 ? "[dim]-[/]" : Escape(string.Join(", ", agent.Skills.Select(ToAlias)))),
                ("platform", Escape(Session.Agent.ToString()))), "mediumpurple2"),
            BuildRichShellPanel("summary", new Spectre.Console.Markup(Escape(agent.Description))));

        var buttons = new List<(string, Action)>();
        var layout = TryResolveAgentLayout(out _);
        if (layout is not null)
        {
            buttons.Add(("Install into current target", () =>
            {
                var summary = SafeGet(() => new AgentInstaller(agentCatalog).Install(new[] { agent }, layout, force: false), default(AgentInstallSummary));
                Toast(summary is null ? "Install failed" : $"{ToAlias(agent.Name)}: {summary.InstalledCount} written, {summary.SkippedExisting.Count} skipped");
                BuildAgentsPage(ws, owner);
            }));
            buttons.Add(("Remove from current target", () =>
            {
                var summary = SafeGet(() => new AgentInstaller(agentCatalog).Remove(new[] { agent }, layout), default(AgentRemoveSummary));
                Toast(summary is null ? "Remove failed" : $"Removed {ToAlias(agent.Name)} ({summary.RemovedCount} file(s))");
                BuildAgentsPage(ws, owner);
            }));
        }

        ShowModal(ws, $"Agent · {ToAlias(agent.Name)}", detail, buttons.ToArray());
    }

    // -------------------------------------------------------------------------
    // Project sync / recommend
    // -------------------------------------------------------------------------

    private void BuildProjectPage(ConsoleWindowSystem ws, ScrollablePanelControl panel)
    {
        panel.ClearContents();

        var layout = ResolveSkillLayout();
        var scan = SafeGet(() => new ProjectSkillRecommender(skillCatalog).Analyze(Session.ProjectDirectory), null);
        if (scan is null)
        {
            panel.AddControl(new SpectreRenderableControl(BuildRichShellPanel("project scan", new Spectre.Console.Markup("[red]Could not scan the project directory.[/]"))));
            return;
        }

        var installer = new SkillInstaller(skillCatalog);
        var installedByName = SafeGet(() => installer.GetInstalledSkills(layout), Array.Empty<InstalledSkillRecord>())
            .ToDictionary(record => record.Skill.Name, StringComparer.OrdinalIgnoreCase);

        var high = scan.Recommendations.Count(r => r.Confidence == RecommendationConfidence.High);
        var med = scan.Recommendations.Count(r => r.Confidence == RecommendationConfidence.Medium);
        var low = scan.Recommendations.Count(r => r.Confidence == RecommendationConfidence.Low);

        var summary = BuildRichPropertyGrid(
            ("project", $"[dim]{Escape(CompactPath(scan.ProjectRoot.FullName))}[/]"),
            ("scanned", $"{scan.ProjectFiles.Count} project file(s)"),
            ("frameworks", scan.TargetFrameworks.Count == 0 ? "[dim]unknown[/]" : Escape(string.Join(", ", scan.TargetFrameworks))),
            ("target", $"[dim]{Escape(CompactPath(layout.PrimaryRoot.FullName))}[/]"),
            ("recommendations", $"{scan.Recommendations.Count}  [dim]([/][green]{high} high[/][dim] · [/][yellow]{med} med[/][dim] · [/][grey]{low} low[/][dim])[/]"));
        panel.AddControl(new SpectreRenderableControl(BuildRichShellPanel("project scan", summary)));

        if (scan.Recommendations.Count == 0)
        {
            panel.AddControl(new SpectreRenderableControl(BuildRichShellPanel("recommendations", new Spectre.Console.Markup("[dim]No package or framework signals matched the catalog. Start with the[/] [green]dotnet[/] [dim]and[/] [green]modern-csharp[/] [dim]skills from the Skills page.[/]"))));
            return;
        }

        var list = StyledList("Recommended skills (Enter to install)")
            .MaxVisibleItems(16)
            .WithScrollbarVisibility(ScrollbarVisibility.Auto);
        foreach (var recommendation in scan.Recommendations
                     .OrderByDescending(r => r.Confidence)
                     .ThenBy(r => r.Skill.Name, StringComparer.Ordinal))
        {
            var marker = recommendation.Confidence switch
            {
                RecommendationConfidence.High => "[green]●●●[/]",
                RecommendationConfidence.Medium => "[yellow]●●○[/]",
                _ => "[grey]●○○[/]",
            };
            installedByName.TryGetValue(recommendation.Skill.Name, out var record);
            var status = record is null ? "[deepskyblue1]new[/]" : record.IsCurrent ? "[green]installed[/]" : "[yellow]update[/]";
            list.AddItem($"{marker} {ToAlias(recommendation.Skill.Name)}  [dim]{status}[/]  [grey]{Escape(string.Join("; ", recommendation.Reasons.Take(2)))}[/]", recommendation);
        }
        list.OnItemActivated((_, item) =>
        {
            if (item.Tag is ProjectSkillRecommendation recommendation)
            {
                var summary2 = SafeGet(() => new SkillInstaller(skillCatalog).Install(new[] { recommendation.Skill }, ResolveSkillLayout(), force: false), default(SkillInstallSummary));
                Toast(summary2 is null ? $"Install failed for {ToAlias(recommendation.Skill.Name)}" : $"{ToAlias(recommendation.Skill.Name)}: {summary2.InstalledCount} written, {summary2.SkippedExisting.Count} skipped");
                BuildProjectPage(ws, panel);
            }
        });
        panel.AddControl(list.Build());

        var installable = scan.Recommendations
            .Where(r => !installedByName.TryGetValue(r.Skill.Name, out var rec) || !rec.IsCurrent)
            .Select(r => r.Skill)
            .GroupBy(s => s.Name, StringComparer.OrdinalIgnoreCase).Select(g => g.First())
            .ToArray();
        if (installable.Length > 0)
        {
            panel.AddControl(Controls.Button($"Install all {installable.Length} recommended skill(s)")
                .OnClick((_, _) =>
                {
                    var summary2 = SafeGet(() => new SkillInstaller(skillCatalog).Install(installable, ResolveSkillLayout(), force: false), default(SkillInstallSummary));
                    Toast(summary2 is null ? "Install failed" : $"Installed {summary2.InstalledCount}, skipped {summary2.SkippedExisting.Count}");
                    BuildProjectPage(ws, panel);
                }).Build());
        }
    }

    // -------------------------------------------------------------------------
    // Catalog analysis
    // -------------------------------------------------------------------------

    private void BuildAnalysisPage(ConsoleWindowSystem ws, ScrollablePanelControl panel)
    {
        panel.ClearContents();

        var layout = ResolveSkillLayout();
        var installer = new SkillInstaller(skillCatalog);
        var installed = SafeGet(() => installer.GetInstalledSkills(layout), Array.Empty<InstalledSkillRecord>());
        var views = BuildCollectionViews(installed)
            .OrderByDescending(view => view.SkillCount)
            .ToArray();
        var signals = SafeGet(BuildPackageSignals, Array.Empty<PackageSignalView>());
        var heaviest = skillCatalog.Skills.OrderByDescending(skill => skill.TokenCount).Take(12).ToArray();

        var summary = BuildRichPropertyGrid(
            ("catalog", $"{Escape(skillCatalog.SourceLabel)} [dim]({Escape(skillCatalog.CatalogVersion)})[/]"),
            ("collections", views.Length.ToString()),
            ("skills", skillCatalog.Skills.Count.ToString()),
            ("total tokens", FormatTokenCount(skillCatalog.Skills.Sum(skill => skill.TokenCount))),
            ("package signals", signals.Count.ToString()));
        panel.AddControl(new SpectreRenderableControl(BuildRichShellPanel("catalog analysis", summary)));

        var collectionCards = views.Take(12).Select(view => (SpectreRendering.IRenderable)BuildRichDetailCard(
            view.Collection, "deepskyblue1",
            $"[dim]skills[/] {view.SkillCount}  [dim]installed[/] {view.InstalledCount}  [dim]tokens[/] {FormatTokenCount(view.TokenCount)}")).ToArray();
        panel.AddControl(new SpectreRenderableControl(BuildRichShellPanel("collections by size", BuildRichCardGrid(collectionCards, maxColumns: 3))));

        var heavyList = StyledList("Heaviest skills (Enter for details)")
            .MaxVisibleItems(12)
            .WithScrollbarVisibility(ScrollbarVisibility.Auto);
        foreach (var skill in heaviest)
        {
            heavyList.AddItem($"{FormatTokenCount(skill.TokenCount)} tokens  ·  {ToAlias(skill.Name)}  [dim]{Escape(skill.Stack)}[/]", skill);
        }
        heavyList.OnItemActivated((_, item) =>
        {
            if (item.Tag is SkillEntry skill)
            {
                ShowSkillDetailModal(ws, panel, skill);
            }
        });
        panel.AddControl(heavyList.Build());

        if (signals.Count > 0)
        {
            var signalCards = signals.Take(18).Select(signal => (SpectreRendering.IRenderable)new Spectre.Console.Markup(
                $"[grey]{Escape(signal.Signal)}[/] [dim]({Escape(signal.Kind)})[/] [dim]→[/] {Escape(ToAlias(signal.Skill.Name))}")).ToArray();
            panel.AddControl(new SpectreRenderableControl(BuildRichShellPanel("package signals", BuildRichStack(signalCards))));
        }
    }

    // -------------------------------------------------------------------------
    // Remove all / Update all action pages
    // -------------------------------------------------------------------------

    private void BuildRemoveAllPage(ConsoleWindowSystem ws, ScrollablePanelControl panel)
    {
        panel.ClearContents();
        var layout = ResolveSkillLayout();
        var installer = new SkillInstaller(skillCatalog);
        var installed = SafeGet(() => installer.GetInstalledSkills(layout), Array.Empty<InstalledSkillRecord>());

        panel.AddControl(new SpectreRenderableControl(BuildRichShellPanel("remove all installed skills", BuildRichPropertyGrid(
            ("target", $"[dim]{Escape(CompactPath(layout.PrimaryRoot.FullName))}[/]"),
            ("installed", installed.Count.ToString())))));

        if (installed.Count == 0)
        {
            panel.AddControl(new SpectreRenderableControl(BuildRichShellPanel("status", new Spectre.Console.Markup("[dim]Nothing to remove in this target.[/]"))));
            return;
        }

        panel.AddControl(Controls.Button($"Remove all {installed.Count} skill(s) from this target")
            .OnClick((_, _) => ConfirmModal(ws, "Remove all installed skills?", $"Deletes every catalog skill directory under {layout.PrimaryRoot.FullName}.", () =>
            {
                var summary = SafeGet(() => new SkillInstaller(skillCatalog).Remove(installed.Select(r => r.Skill).ToArray(), layout), default(SkillRemoveSummary));
                Toast(summary is null ? "Remove failed" : $"Removed {summary.RemovedCount} skill(s)");
                BuildRemoveAllPage(ws, panel);
            })).Build());
    }

    private void BuildUpdateAllPage(ConsoleWindowSystem ws, ScrollablePanelControl panel)
    {
        panel.ClearContents();
        var layout = ResolveSkillLayout();
        var installer = new SkillInstaller(skillCatalog);
        var outdated = SafeGet(() => installer.GetInstalledSkills(layout), Array.Empty<InstalledSkillRecord>())
            .Where(record => !record.IsCurrent)
            .ToArray();

        panel.AddControl(new SpectreRenderableControl(BuildRichShellPanel("update all outdated skills", BuildRichPropertyGrid(
            ("target", $"[dim]{Escape(CompactPath(layout.PrimaryRoot.FullName))}[/]"),
            ("outdated", outdated.Length.ToString())))));

        if (outdated.Length == 0)
        {
            panel.AddControl(new SpectreRenderableControl(BuildRichShellPanel("status", new Spectre.Console.Markup("[green]All installed skills already match the catalog version.[/]"))));
            return;
        }

        var listCards = outdated.Select(record => (SpectreRendering.IRenderable)new Spectre.Console.Markup(
            $"[yellow]↻[/] {Escape(ToAlias(record.Skill.Name))}  [dim]{Escape(record.InstalledVersion)} → {Escape(record.Skill.Version)}[/]")).ToArray();
        panel.AddControl(new SpectreRenderableControl(BuildRichShellPanel("pending updates", BuildRichStack(listCards))));

        panel.AddControl(Controls.Button($"Update all {outdated.Length} skill(s)")
            .OnClick((_, _) =>
            {
                Toast(UpdateSkillRecords(outdated));
                BuildUpdateAllPage(ws, panel);
            }).Build());
    }

    // -------------------------------------------------------------------------
    // Settings / workspace
    // -------------------------------------------------------------------------

    private void BuildSettingsPage(ConsoleWindowSystem ws, ScrollablePanelControl panel)
    {
        panel.ClearContents();

        var layout = ResolveSkillLayout();
        var agentStatus = ResolveAgentStatus();
        var summary = BuildRichPropertyGrid(
            ("platform", Escape(Session.Agent.ToString())),
            ("scope", Escape(Session.Scope.ToString())),
            ("project", Escape(CompactPath(Session.ProjectDirectory ?? Environment.CurrentDirectory))),
            ("skill target", $"[dim]{Escape(CompactPath(layout.PrimaryRoot.FullName))}[/]"),
            ("agent target", agentStatus.Layout is null ? $"[red]{Escape(agentStatus.Summary)}[/]" : $"[dim]{Escape(CompactPath(agentStatus.Layout.PrimaryRoot.FullName))}[/]"),
            ("catalog", $"{Escape(skillCatalog.SourceLabel)} [dim]({Escape(skillCatalog.CatalogVersion)})[/]"),
            ("build", ToolVersionInfo.IsDevelopmentBuild ? "[dim]local development[/]" : "[green]published[/]"));
        panel.AddControl(new SpectreRenderableControl(BuildRichShellPanel("workspace", summary)));

        var list = StyledList("Settings (Enter to change)")
            .MaxVisibleItems(8);
        list.AddItem($"Platform: {Session.Agent}", "platform");
        list.AddItem($"Install scope: {Session.Scope}", "scope");
        list.AddItem("Refresh catalog now", "refresh");
        list.OnItemActivated((_, item) =>
        {
            switch (item.Tag as string)
            {
                case "platform":
                    ChooseEnumModal(ws, "Install platform", Enum.GetValues<AgentPlatform>(), Session.Agent, value =>
                    {
                        Session.Agent = value;
                        Toast($"Platform set to {value}");
                        BuildSettingsPage(ws, panel);
                    });
                    break;
                case "scope":
                    ChooseEnumModal(ws, "Install scope", Enum.GetValues<InstallScope>(), Session.Scope, value =>
                    {
                        Session.Scope = value;
                        Toast($"Scope set to {value}");
                        BuildSettingsPage(ws, panel);
                    });
                    break;
                case "refresh":
                    try
                    {
                        Toast("Refreshing catalog…");
                        LoadCatalogsAsync(refreshCatalog: true).GetAwaiter().GetResult();
                        Toast($"Catalog refreshed: {skillCatalog.CatalogVersion} ({skillCatalog.Skills.Count} skills)");
                    }
                    catch (Exception exception)
                    {
                        Toast($"Refresh failed: {exception.Message}");
                    }
                    BuildSettingsPage(ws, panel);
                    break;
            }
        });
        panel.AddControl(list.Build());
    }

    // -------------------------------------------------------------------------
    // About
    // -------------------------------------------------------------------------

    private void BuildAboutPage(ScrollablePanelControl panel)
    {
        panel.ClearContents();
        var about = BuildRichPropertyGrid(
            ("tool", $"{Escape(ToolIdentity.DisplayCommand)}"),
            ("package", Escape(ToolIdentity.PackageId)),
            ("version", Escape(ToolVersionInfo.CurrentVersion)),
            ("build", ToolVersionInfo.IsDevelopmentBuild ? "[dim]local development[/]" : "[green]published[/]"),
            ("catalog", $"{Escape(skillCatalog.SourceLabel)} [dim]({Escape(skillCatalog.CatalogVersion)})[/]"),
            ("skills", skillCatalog.Skills.Count.ToString()),
            ("agents", agentCatalog.Agents.Count.ToString()));

        var surface = BuildRichDetailCard("surface map", "deepskyblue1",
            "[grey]Home[/] [dim]session, catalog telemetry, update notice[/]",
            "[grey]Skills / Installed[/] [dim]browse, install, update, remove catalog skills[/]",
            "[grey]Collections / Bundles / Packages[/] [dim]install grouped surfaces[/]",
            "[grey]Agents[/] [dim]install orchestration agents into native agent directories[/]",
            "[grey]Project[/] [dim]scan .csproj signals and install recommended skills[/]",
            "[grey]Analysis[/] [dim]collection sizes, heaviest skills, package signals[/]");

        var notes = BuildRichDetailCard("notes", "grey",
            "[dim]This is the SharpConsoleUI command center. Run with redirected stdin/stdout to get the classic prompt shell instead.[/]",
            "[dim]CLI sub-commands (list, install, recommend, …) are unchanged — see[/] [green]dotnet skills help[/][dim].[/]");

        panel.AddControl(new SpectreRenderableControl(BuildRichStack(
            BuildRichShellPanel("about", about),
            surface,
            notes)));
    }

    // -------------------------------------------------------------------------
    // Modal + status helpers
    // -------------------------------------------------------------------------

    private void ShowModal(ConsoleWindowSystem ws, string title, SpectreRendering.IRenderable content, params (string Label, Action OnClick)[] buttons)
    {
        Window? modal = null;
        var width = Math.Clamp(SafeConsole(() => Console.WindowWidth, 120) - 10, 56, 116);
        var height = Math.Clamp(SafeConsole(() => Console.WindowHeight, 32) - 6, 14, 34);

        var body = Controls.ScrollablePanel().Build();
        body.AddControl(new SpectreRenderableControl(content));

        void Close()
        {
            if (modal is not null)
            {
                ws.CloseWindow(modal);
            }
        }

        var toolbar = Controls.Toolbar().WithSpacing(2).WithAlignment(HorizontalAlignment.Center);
        foreach (var (label, onClick) in buttons)
        {
            var captured = onClick;
            toolbar.AddButton(label, (_, _) => { Close(); captured(); });
        }
        toolbar.AddButton("Close", (_, _) => Close());

        modal = new WindowBuilder(ws)
            .WithTitle(title)
            .WithSize(width, height)
            .Centered()
            .AsModal()
            .Minimizable(false)
            .Maximizable(false)
            .WithBorderStyle(BorderStyle.Rounded)
            .WithBorderColor(new Color(90, 110, 142))
            .OnKeyPressed((_, e) =>
            {
                if (e.KeyInfo.Key == ConsoleKey.Escape)
                {
                    Close();
                    e.Handled = true;
                }
            })
            .AddControl(body)
            .AddControl(toolbar.StickyBottom().Build())
            .BuildAndShow();
    }

    private void ConfirmModal(ConsoleWindowSystem ws, string title, string message, Action onConfirm)
    {
        ShowModal(ws, title, BuildRichShellPanel("confirm", new Spectre.Console.Markup($"[yellow]{Escape(message)}[/]"), "yellow"),
            ("Yes, proceed", onConfirm));
    }

    private void ChooseEnumModal<TEnum>(ConsoleWindowSystem ws, string title, TEnum[] values, TEnum current, Action<TEnum> onPicked)
        where TEnum : struct, Enum
    {
        Window? modal = null;

        void Close()
        {
            if (modal is not null)
            {
                ws.CloseWindow(modal);
            }
        }

        var list = StyledList(title).MaxVisibleItems(Math.Min(values.Length, 10));
        foreach (var value in values)
        {
            list.AddItem((value.Equals(current) ? "● " : "  ") + value, value);
        }
        list.OnItemActivated((_, item) =>
        {
            Close();
            if (item.Tag is TEnum picked)
            {
                onPicked(picked);
            }
        });

        var toolbar = Controls.Toolbar().WithSpacing(2).WithAlignment(HorizontalAlignment.Center);
        toolbar.AddButton("Cancel", (_, _) => Close());

        modal = new WindowBuilder(ws)
            .WithTitle(title)
            .WithSize(Math.Clamp(values.Length == 0 ? 40 : values.Max(v => v.ToString().Length) + 24, 40, 70), Math.Min(values.Length + 8, 18))
            .Centered()
            .AsModal()
            .Minimizable(false)
            .Maximizable(false)
            .WithBorderStyle(BorderStyle.Rounded)
            .WithBorderColor(new Color(90, 110, 142))
            .OnKeyPressed((_, e) =>
            {
                if (e.KeyInfo.Key == ConsoleKey.Escape)
                {
                    Close();
                    e.Handled = true;
                }
            })
            .AddControl(list.Build())
            .AddControl(toolbar.StickyBottom().Build())
            .BuildAndShow();
    }

    private static ITheme BuildTheme() => new ModernGrayTheme
    {
        ListHoverBackgroundColor = SelectionBg,
        ListHoverForegroundColor = SelectionFg,
        ListUnfocusedHighlightBackgroundColor = UnfocusedSelectionBg,
        ListUnfocusedHighlightForegroundColor = UnfocusedSelectionFg,
    };

    /// <summary>
    /// A list control styled so the selected row is a solid inverted bar — the same bar whether
    /// the row was reached by keyboard, mouse hover, or click (see <see cref="BuildTheme"/>).
    /// </summary>
    private static ListBuilder StyledList(string? title = null) => Controls.List(title)
        .WithScrollbarVisibility(ScrollbarVisibility.Auto)
        .WithAutoHighlightOnFocus(true)
        .WithHoverHighlighting(true)
        .WithHighlightColors(SelectionFg, SelectionBg);

    // -------------------------------------------------------------------------
    // Interactive status bar (dynamic per page, clickable hints, highlighted keys)
    // -------------------------------------------------------------------------

    private void RebuildStatusBar(HomeAction? page)
    {
        var bar = _statusBar;
        if (bar is null)
        {
            return;
        }

        _currentPage = page;
        bar.BatchUpdate(() =>
        {
            bar.ClearAll();

            bar.AddLeft("↑↓", "Move");
            bar.AddLeft("←→", "Switch pane");
            bar.AddLeft("Enter", page is HomeAction.SyncProject ? "Install" : page is HomeAction.Workspace ? "Change" : "Open");
            foreach (var (key, label, action) in PageShortcuts(page))
            {
                bar.AddLeft(key, label, action);
            }
            bar.AddLeft("Ctrl+R", "Refresh", RefreshCatalogFromUi);
            bar.AddLeft("Esc", "Quit", () => _ws?.Shutdown(0));

            _statusMessage = bar.AddCenterText(string.Empty);

            bar.AddRightText($"[dim]v{Escape(skillCatalog.CatalogVersion)} · {skillCatalog.Skills.Count} skills[/]");
            bar.AddRightSeparator();
            _clockItem = bar.AddRightText(DateTime.Now.ToString("HH:mm:ss"));
        });
    }

    private IEnumerable<(string Key, string Label, Action OnClick)> PageShortcuts(HomeAction? page) => page switch
    {
        HomeAction.ManageInstalled => new (string, string, Action)[]
        {
            ("Ctrl+U", "Update outdated", UpdateAllOutdatedFromUi),
            ("Ctrl+Del", "Remove all", RemoveAllFromUi),
        },
        HomeAction.SyncProject => new (string, string, Action)[]
        {
            ("Ctrl+I", "Install recommended", InstallAllRecommendedFromUi),
        },
        _ => Array.Empty<(string, string, Action)>(),
    };

    private void RebuildActivePage()
    {
        if (_ws is null || _activePanel is null)
        {
            return;
        }

        if (_currentPage is HomeAction action)
        {
            BuildActionPage(_ws, _activePanel, action);
        }
        else
        {
            BuildHomePage(_ws, _activePanel);
        }
    }

    private void Toast(string message)
    {
        if (_statusMessage is not null)
        {
            _statusMessage.Label = string.IsNullOrEmpty(message) ? string.Empty : $"[grey70]{Escape(message)}[/]";
        }
    }

    private async Task ClockLoopAsync(Window window, CancellationToken cancellationToken)
    {
        while (!cancellationToken.IsCancellationRequested)
        {
            try
            {
                await Task.Delay(1000, cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return;
            }

            if (_clockItem is not null)
            {
                _clockItem.Label = DateTime.Now.ToString("HH:mm:ss");
                window.Invalidate(false);
            }
        }
    }

    private void RefreshCatalogFromUi()
    {
        try
        {
            Toast("Refreshing catalog…");
            LoadCatalogsAsync(refreshCatalog: true).GetAwaiter().GetResult();
            Toast($"Catalog refreshed: {skillCatalog.CatalogVersion} ({skillCatalog.Skills.Count} skills)");
        }
        catch (Exception exception)
        {
            Toast($"Refresh failed: {exception.Message}");
        }

        RebuildStatusBar(_currentPage);
        RebuildActivePage();
    }

    private void UpdateAllOutdatedFromUi()
    {
        var layout = ResolveSkillLayout();
        var outdated = SafeGet(() => new SkillInstaller(skillCatalog).GetInstalledSkills(layout), Array.Empty<InstalledSkillRecord>())
            .Where(record => !record.IsCurrent)
            .ToArray();
        if (outdated.Length == 0)
        {
            Toast("No outdated skills in this target");
            return;
        }

        Toast(UpdateSkillRecords(outdated));
        RebuildActivePage();
    }

    private void RemoveAllFromUi()
    {
        if (_ws is null)
        {
            return;
        }

        var layout = ResolveSkillLayout();
        var installed = SafeGet(() => new SkillInstaller(skillCatalog).GetInstalledSkills(layout), Array.Empty<InstalledSkillRecord>());
        if (installed.Count == 0)
        {
            Toast("Nothing to remove in this target");
            return;
        }

        ConfirmModal(_ws, "Remove all installed skills?", $"Deletes every catalog skill from {layout.PrimaryRoot.FullName}.", () =>
        {
            var summary = SafeGet(() => new SkillInstaller(skillCatalog).Remove(installed.Select(record => record.Skill).ToArray(), layout), default(SkillRemoveSummary));
            Toast(summary is null ? "Remove failed" : $"Removed {summary.RemovedCount} skill(s)");
            RebuildActivePage();
        });
    }

    private void InstallAllRecommendedFromUi()
    {
        var scan = SafeGet(() => new ProjectSkillRecommender(skillCatalog).Analyze(Session.ProjectDirectory), null);
        if (scan is null)
        {
            Toast("Project scan failed");
            return;
        }

        var layout = ResolveSkillLayout();
        var installedByName = SafeGet(() => new SkillInstaller(skillCatalog).GetInstalledSkills(layout), Array.Empty<InstalledSkillRecord>())
            .ToDictionary(record => record.Skill.Name, StringComparer.OrdinalIgnoreCase);
        var installable = scan.Recommendations
            .Where(r => !installedByName.TryGetValue(r.Skill.Name, out var rec) || !rec.IsCurrent)
            .Select(r => r.Skill)
            .GroupBy(s => s.Name, StringComparer.OrdinalIgnoreCase).Select(g => g.First())
            .ToArray();
        if (installable.Length == 0)
        {
            Toast("No new recommended skills to install");
            return;
        }

        var summary = SafeGet(() => new SkillInstaller(skillCatalog).Install(installable, layout, force: false), default(SkillInstallSummary));
        Toast(summary is null ? "Install failed" : $"Installed {summary.InstalledCount}, skipped {summary.SkippedExisting.Count}");
        RebuildActivePage();
    }

    private static int SafeCount(Func<int> getter)
    {
        try
        {
            return getter();
        }
        catch
        {
            return 0;
        }
    }

    private static T SafeGet<T>(Func<T> getter, T fallback)
    {
        try
        {
            return getter();
        }
        catch
        {
            return fallback;
        }
    }

    private static int SafeConsole(Func<int> getter, int fallback)
    {
        try
        {
            var value = getter();
            return value > 0 ? value : fallback;
        }
        catch
        {
            return fallback;
        }
    }
}
