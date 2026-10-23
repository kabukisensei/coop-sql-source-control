using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using CoopSqlSourceControl.Core;
using Newtonsoft.Json.Linq;

namespace CoopSqlSourceControl
{
    /// <summary>
    /// The SQL Source Control window: pick a repo (its sqlsc.yml) and a database,
    /// see what differs and who has what checked out, and act on it. Every button
    /// is one sqlsc command run with --json; the window only shows the answer.
    /// </summary>
    public partial class SourceControlControl : UserControl
    {
        private readonly string settingsPath = Settings.DefaultPath;
        private Settings settings;
        private SqlscClient client;
        private string configPath;
        private bool busy;
        private bool loadingTargets;
        private CancellationTokenSource running;

        public SourceControlControl()
        {
            InitializeComponent();
            settings = Settings.Load(settingsPath);
            Loaded += OnLoaded;
        }

        private TargetInfo Target => TargetBox.SelectedItem as TargetInfo;

        private async void OnLoaded(object sender, RoutedEventArgs e)
        {
            Loaded -= OnLoaded;
            // Repos whose folder is gone (a deleted clone, another session's temporary
            // folder) leave the list, so it only offers what can be opened.
            if (settings.Prune(File.Exists).Count > 0)
            {
                SaveSettings();
            }

            var exe = FindSqlsc();
            if (exe == null)
            {
                return;
            }

            await DescribeSqlscAsync(exe);
            ShowRecent();
            var last = settings.Recent.FirstOrDefault();
            if (last != null)
            {
                await OpenRepoAsync(last.ConfigPath);
            }
        }

        /// <summary>The add-in's own version, for the line that names the sqlsc in use.</summary>
        private static string AddinVersion
        {
            get
            {
                var version = typeof(SourceControlControl).Assembly.GetName().Version;
                return version == null ? "?" : version.Major + "." + version.Minor + "." + version.Build;
            }
        }

        /// <summary>Names the sqlsc in use and its version under the status line.</summary>
        private async Task DescribeSqlscAsync(string exe)
        {
            var location = SqlscLocation.Find(settings.SqlscPath);
            if (location == null)
            {
                SqlscText.Text = string.Empty;
                return;
            }

            string version = null;
            try
            {
                version = await new SqlscRunner(exe).VersionAsync(CancellationToken.None);
            }
            catch (Exception exc) when (!(exc is OutOfMemoryException))
            {
                // The version line is informational; the commands report their own errors.
            }

            SqlscText.Text = location.Describe(version, AddinVersion);
        }

        // --- repo and target -------------------------------------------------------------

        private string FindSqlsc()
        {
            // A chosen path whose file is gone (an old venv) is forgotten, so it cannot
            // keep the window from finding coop's install later.
            if (!string.IsNullOrWhiteSpace(settings.SqlscPath) && !File.Exists(settings.SqlscPath))
            {
                settings.SqlscPath = null;
                SaveSettings();
            }

            var exe = SqlscRunner.Find(settings.SqlscPath);
            LocateButton.Visibility = exe == null ? Visibility.Visible : Visibility.Collapsed;
            if (exe == null)
            {
                SqlscText.Text = string.Empty;
                Say("sqlsc was not found. It comes with coop (run `coop update`), or install it with "
                    + "`pipx install git+https://github.com/kabukisensei/coop-sql-source-control`, "
                    + "or use Find sqlsc… to point to sqlsc.exe.");
            }

            return exe;
        }

        private void ShowRecent()
        {
            RepoBox.SelectionChanged -= OnRepoChanged;
            RepoBox.ItemsSource = settings.Recent.Select(r => r.ConfigPath).ToList();
            RepoBox.SelectedItem = configPath;
            RepoBox.SelectionChanged += OnRepoChanged;
        }

        private async void OnLocateSqlsc(object sender, RoutedEventArgs e)
        {
            var dialog = new Microsoft.Win32.OpenFileDialog { Title = "Find sqlsc.exe", Filter = "sqlsc|sqlsc.exe;coop-sqlsc.exe|Programs|*.exe" };
            if (dialog.ShowDialog() != true)
            {
                return;
            }

            settings.SqlscPath = dialog.FileName;
            SaveSettings();
            var exe = FindSqlsc();
            if (exe != null)
            {
                await DescribeSqlscAsync(exe);
                Say("Using " + exe + ". Open a repo's sqlsc.yml to start.");
            }
        }

        private async void OnOpenRepo(object sender, RoutedEventArgs e)
        {
            var dialog = new Microsoft.Win32.OpenFileDialog { Title = "Open a repo's sqlsc.yml", Filter = "sqlsc.yml|sqlsc.yml|YAML|*.yml;*.yaml" };
            if (dialog.ShowDialog() == true)
            {
                await OpenRepoAsync(dialog.FileName);
            }
        }

        private async void OnRepoChanged(object sender, SelectionChangedEventArgs e)
        {
            var path = RepoBox.SelectedItem as string;
            if (path != null && path != configPath)
            {
                await OpenRepoAsync(path);
            }
        }

        private async Task OpenRepoAsync(string path)
        {
            var exe = FindSqlsc();
            if (exe == null)
            {
                return;
            }

            configPath = path;
            client = new SqlscClient(new SqlscRunner(exe), path);
            settings.Remember(path, null);
            SaveSettings();
            ShowRecent();
            ClearGrids();

            var result = await RunAsync("Reading the targets in " + path + "…", t => client.TargetsAsync(t));
            if (result == null)
            {
                return;
            }

            var targets = TargetInfo.ListFrom(result.Json);
            loadingTargets = true;
            TargetBox.ItemsSource = targets;
            var wanted = settings.LastTarget(path);
            TargetBox.SelectedItem = targets.FirstOrDefault(t => t.Name == wanted && t.Problem == null)
                ?? targets.FirstOrDefault(t => t.IsDefault && t.Problem == null)
                ?? targets.FirstOrDefault(t => t.Problem == null);
            loadingTargets = false;
            if (Target == null)
            {
                Say(targets.Count == 0 ? "This sqlsc.yml has no targets yet." : "None of the targets in this sqlsc.yml can be used; see the list.");
                return;
            }

            await RefreshAsync();
        }

        private async void OnTargetChanged(object sender, SelectionChangedEventArgs e)
        {
            if (loadingTargets || Target == null)
            {
                return;
            }

            settings.Remember(configPath, Target.Name);
            SaveSettings();
            ClearRows(); // the last target's rows must not sit under this one's name
            await RefreshAsync();
        }

        private void OnCancel(object sender, RoutedEventArgs e)
        {
            running?.Cancel();
        }

        private async void OnRefresh(object sender, RoutedEventArgs e)
        {
            // With no usable target (sqlsc.yml was wrong, or sqlsc could not read it),
            // read the targets again: the file or the tool may have been fixed since.
            if (Target == null && configPath != null)
            {
                await OpenRepoAsync(configPath);
                return;
            }

            await RefreshAsync();
        }

        private async Task RefreshAsync()
        {
            if (client == null || Target == null)
            {
                return;
            }

            var target = Target;
            var result = await RunAsync("Comparing " + target.Name + " with the repo…", t => client.StatusAsync(target.Name, t));
            if (result == null)
            {
                ClearRows(); // rows from an earlier answer are no longer known to be true
                return;
            }

            var changes = ChangeInfo.ListFrom(result.Json);
            ChangesGrid.ItemsSource = changes;
            LocksGrid.ItemsSource = LockInfo.ListFrom(result.Json, "locks");
            var warnings = Warnings(result.Json);
            var pending = MigrationInfo.PendingFrom(result.Json);
            Say((changes.Count == 0
                    ? "The repo matches " + target.Name + "."
                    : changes.Count + " object(s) differ between " + target.Name + " and the repo" + Sides(changes) + ".")
                + (pending.Count > 0 ? " " + pending.Count + " migration script(s) have not run on " + target.Name + " yet; Get latest runs them." : string.Empty)
                + (warnings.Length > 0 ? " " + warnings : string.Empty));
        }

        /// <summary>": 2 to commit, 1 to get latest, 1 changed on both sides", from the Changed in column.</summary>
        private static string Sides(IList<ChangeInfo> changes)
        {
            var parts = new List<string>();
            var toCommit = changes.Count(c => c.Origin == "database");
            var toGet = changes.Count(c => c.IsFromRepo);
            var both = changes.Count(c => c.IsConflict);
            if (toCommit > 0)
            {
                parts.Add(toCommit + " to commit");
            }

            if (toGet > 0)
            {
                parts.Add(toGet + " to get latest");
            }

            if (both > 0)
            {
                parts.Add(both + " changed on both sides");
            }

            return parts.Count == 0 ? string.Empty : ": " + string.Join(", ", parts);
        }

        // --- changes tab -----------------------------------------------------------------

        private IList<ChangeInfo> SelectedChanges => ChangesGrid.SelectedItems.OfType<ChangeInfo>().ToList();

        private async void OnDiff(object sender, RoutedEventArgs e)
        {
            var selected = SelectedChanges;
            if (client == null || Target == null || selected.Count == 0)
            {
                Say("Select the objects to compare.");
                return;
            }

            var result = await RunAsync("Comparing…", t => client.DiffAsync(Target.Name, Names(selected), t));
            if (result == null)
            {
                return;
            }

            var text = new StringBuilder();
            foreach (var change in TargetInfo.Items(result.Json, "changes"))
            {
                text.AppendLine(change.Value<string>("diff"));
            }

            ShowOutput(text.Length == 0 ? "No differences." : text.ToString());
            Say(text.Length == 0 ? "No differences." : "The differences for " + selected.Count + " object(s) are in Output.");
        }

        private async void OnCommit(object sender, RoutedEventArgs e)
        {
            if (client == null || Target == null)
            {
                return;
            }

            var selected = SelectedChanges;
            var everything = selected.Count == 0;
            var affected = everything ? (ChangesGrid.ItemsSource as IEnumerable<ChangeInfo> ?? Enumerable.Empty<ChangeInfo>()).ToList() : selected;
            // The question counts what sqlsc will commit: rows someone else has checked out
            // and rows the repo moved are named as left out, not counted (dev VM finding 3).
            // With no rows at all the command still runs: it may owe git a migration script
            // or sqlsc.yml, and says "Nothing to commit" otherwise.
            var plan = new CommitPlan(affected);
            if (affected.Count > 0 && plan.Committable.Count == 0)
            {
                Say(plan.NothingToCommit(Target.Name));
                return;
            }

            // "Deleted" means the repo has the object and the database does not. When sqlsc knows
            // the object was synced here before, that is a drop and the file goes; when it has
            // never seen it (a teammate's new object), sqlsc leaves it for Get latest. Only the
            // ones it cannot tell apart need a warning.
            var deletions = plan.Committable.Count(c => c.Action == "deleted" && c.Origin != "repo" && c.Origin != "database");
            var warning = deletions == 0 ? string.Empty
                : Environment.NewLine + Environment.NewLine + deletions + " of them would delete the object's file from the repo, because "
                    + Target.Name + " does not have it. If it is new from the repo and not applied yet, use Get latest instead.";
            var question = plan.Question(Target.Name, everything);
            if (question != null && (everything || deletions > 0 || plan.LeftOut.Length > 0) && !Confirm(question + warning))
            {
                return;
            }

            var push = PushBox.IsChecked == true;
            var result = await RunAsync(push ? "Committing and pushing…" : "Committing…", t => client.CommitAsync(Target.Name, Names(selected), CommitMessageBox.Text, t, push));
            if (result == null)
            {
                return;
            }

            var sha = result.Json.Value<string>("commit");
            var skipped = TargetInfo.Items(result.Json, "skipped").Select(s => s.Value<string>("object") + " (" + s.Value<string>("locked_by") + ")").ToList();
            var heldBack = TargetInfo.Items(result.Json, "held_back").Select(h => h.Value<string>("object")).ToList();
            await RefreshAsync();
            if (sha != null)
            {
                CommitMessageBox.Clear();
            }

            var pushed = result.Json.Value<string>("pushed");
            var pushError = result.Json.Value<string>("push_error");
            Say((pushError ?? (sha == null ? "Nothing to commit." : "Committed as " + sha + (pushed != null ? " and pushed " + pushed : string.Empty) + "."))
                + PulledNote(result.Json)
                + (skipped.Count > 0 ? " Left for whoever has them checked out: " + string.Join(", ", skipped) + "." : string.Empty)
                + (heldBack.Count > 0 ? " Left because the repo changed them since your last sync (see Changed in; Get latest applies them): " + string.Join(", ", heldBack) + "." : string.Empty));
        }

        private async void OnTrackRows(object sender, RoutedEventArgs e)
        {
            if (client == null || Target == null)
            {
                return;
            }

            // Tables already shown as changes can be picked; lookup tables usually are not, so ask.
            var names = Names(SelectedChanges.Where(c => c.Type == "table"));
            if (names.Count == 0)
            {
                var typed = Ask("Track rows", "Tables whose rows to keep in the repo as static data, separated by commas (for example dbo.Status, ref.Country):");
                names = SplitNames(typed);
                if (names.Count == 0)
                {
                    return;
                }
            }

            var result = await RunAsync("Reading the rows…", t => client.DataAddAsync(Target.Name, names, t));
            if (result == null)
            {
                return;
            }

            var tracked = TargetInfo.Items(result.Json, "tracked").Select(x => x.Value<string>("object")).ToList();
            await RefreshAsync();
            Say("Now tracking the rows of " + string.Join(", ", tracked) + ". Commit adds the data file" + (tracked.Count == 1 ? string.Empty : "s") + " to the repo.");
        }

        private static IList<string> SplitNames(string typed)
        {
            return (typed ?? string.Empty).Split(new[] { ',', ';' }, StringSplitOptions.RemoveEmptyEntries)
                .Select(n => n.Trim()).Where(n => n.Length > 0).Distinct().ToList();
        }

        private async void OnGetLatest(object sender, RoutedEventArgs e)
        {
            if (client == null || Target == null)
            {
                return;
            }

            var target = Target;
            var names = Names(SelectedChanges);
            var plan = await RunAsync("Working out what get latest would change…", t => client.GetLatestAsync(target.Name, names, true, t));
            if (plan == null)
            {
                return;
            }

            var steps = TargetInfo.Items(plan.Json, "steps").Count(s => s.Value<string>("action") != "skip" && s.Value<string>("action") != "manual");
            ShowOutput(plan.Json.Value<string>("script") ?? string.Empty);
            if (steps == 0)
            {
                Say("Nothing to do; " + target.Name + " already matches the repo.");
                return;
            }

            if (target.IsProtected)
            {
                // sqlsc refuses to change a protected target; asking first would only lead to that.
                Say(target.Name + " is protected, so get latest only shows the script (in Output). Release to it with `sqlsc deploy`.");
                return;
            }

            var scripts = MigrationInfo.RanFrom(plan.Json);
            Say("Review the script in Output, then answer the question.");
            if (!Confirm("Run this script against " + target.Name + " (" + target.Database + ")? It makes " + steps + " change(s)"
                + (scripts.Count > 0 ? ", including " + scripts.Count + " migration script(s): " + string.Join(", ", scripts) : string.Empty) + "."))
            {
                Say("Get latest was not run; the script is in Output.");
                return;
            }

            var result = await RunAsync("Updating " + target.Name + "…", t => client.GetLatestAsync(target.Name, names, false, t), acceptFailure: true);
            if (result == null)
            {
                return;
            }

            var failed = TargetInfo.Items(result.Json, "failed").Select(f => f.Value<string>("object") + ": " + f.Value<string>("error")).ToList();
            var kept = TargetInfo.Items(result.Json, "kept").Select(k => k.Value<string>("object")).ToList();
            await RefreshAsync();
            Say((failed.Count == 0
                    ? "Updated " + target.Name + "."
                    : "Get latest failed, so nothing was changed: " + string.Join("; ", failed))
                + (kept.Count > 0 ? " Kept as they are, because " + target.Name + " changed them since your last sync (commit them first): " + string.Join(", ", kept) + "." : string.Empty));
        }

        private async void OnCheckout(object sender, RoutedEventArgs e)
        {
            if (client == null || Target == null)
            {
                return;
            }

            // Objects are checked out before they are edited, when they do not differ yet and
            // so are not in the grid: with nothing selected, ask for their names. The note is
            // what teammates see next to the lock (why, or until when), like Redgate's.
            var names = Names(SelectedChanges);
            var answers = names.Count == 0
                ? AskFields("Check out",
                    "Objects to check out, separated by commas (for example dbo.Customer, sales.vTotals):",
                    "Note for the team (optional):")
                : AskFields("Check out " + (names.Count == 1 ? names[0] : names.Count + " objects"), "Note for the team (optional):");
            if (answers == null)
            {
                return;
            }

            var note = answers[answers.Length - 1];
            if (names.Count == 0)
            {
                names = SplitNames(answers[0]);
                if (names.Count == 0)
                {
                    return;
                }
            }

            var result = await RunAsync("Checking out…", t => client.CheckoutAsync(Target.Name, names, note, t), acceptFailure: true);
            if (result == null)
            {
                return;
            }

            var taken = LockInfo.ListFrom(result.Json, "conflicts");
            var granted = TargetInfo.Items(result.Json, "checked_out").Count();
            await RefreshAsync();
            Say(taken.Count == 0
                ? "Checked out " + granted + " object(s); they are on the Checkouts tab."
                : "Checked out by someone else: " + string.Join(", ", taken.Select(l => l.Object + " (" + l.LockedBy + ")")) + ".");
        }

        /// <summary>A one-line question in a small modal window; null when cancelled.</summary>
        private string Ask(string title, string prompt)
        {
            var answers = AskFields(title, prompt);
            return answers == null ? null : answers[0];
        }

        /// <summary>One text box per prompt in a small modal window; null when cancelled.
        /// The boxes carry automation ids AskBox, AskBox2, … so UI tests can find them.</summary>
        private string[] AskFields(string title, params string[] prompts)
        {
            var boxes = new List<TextBox>();
            var panel = new StackPanel { Margin = new Thickness(12) };
            for (var i = 0; i < prompts.Length; i++)
            {
                var id = i == 0 ? "AskBox" : "AskBox" + (i + 1);
                var box = new TextBox { Name = id, MinWidth = 380, Margin = new Thickness(0, 6, 0, 10) };
                System.Windows.Automation.AutomationProperties.SetAutomationId(box, id);
                System.Windows.Automation.AutomationProperties.SetName(box, prompts[i]);
                panel.Children.Add(new TextBlock { Text = prompts[i], TextWrapping = TextWrapping.Wrap, MaxWidth = 380 });
                panel.Children.Add(box);
                boxes.Add(box);
            }

            var ok = new Button { Content = "OK", IsDefault = true, MinWidth = 70, Margin = new Thickness(0, 0, 6, 0) };
            var cancel = new Button { Content = "Cancel", IsCancel = true, MinWidth = 70 };
            var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
            buttons.Children.Add(ok);
            buttons.Children.Add(cancel);
            panel.Children.Add(buttons);
            var window = new Window
            {
                Title = title,
                Content = panel,
                SizeToContent = SizeToContent.WidthAndHeight,
                ResizeMode = ResizeMode.NoResize,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                ShowInTaskbar = false,
                Owner = Window.GetWindow(this),
            };
            ok.Click += (s, a) => window.DialogResult = true;
            window.Loaded += (s, a) => boxes[0].Focus();
            return window.ShowDialog() == true ? boxes.Select(b => b.Text).ToArray() : null;
        }

        private async void OnCheckinChanges(object sender, RoutedEventArgs e)
        {
            await CheckinAsync(Names(SelectedChanges), commit: true);
        }

        // --- checkouts tab ---------------------------------------------------------------

        private IList<string> SelectedLocks => LocksGrid.SelectedItems.OfType<LockInfo>().Select(l => l.Name).ToList();

        private async void OnCheckinLocks(object sender, RoutedEventArgs e)
        {
            await CheckinAsync(SelectedLocks, commit: true);
        }

        private async void OnUndoCheckout(object sender, RoutedEventArgs e)
        {
            await CheckinAsync(SelectedLocks, commit: false);
        }

        private async void OnForceRelease(object sender, RoutedEventArgs e)
        {
            var names = SelectedLocks;
            if (client == null || Target == null || names.Count == 0)
            {
                Say("Select the checkouts to release.");
                return;
            }

            if (!Confirm("Release " + names.Count + " checkout(s), including other people's? Their edits are not committed."))
            {
                return;
            }

            var result = await RunAsync("Releasing…", t => client.UnlockAsync(Target.Name, names, true, t));
            if (result != null)
            {
                await RefreshAsync();
                Say("Released " + TargetInfo.Items(result.Json, "released").Count() + " checkout(s).");
            }
        }

        private async Task CheckinAsync(IList<string> names, bool commit)
        {
            if (client == null || Target == null)
            {
                return;
            }

            if (names.Count == 0 && !Confirm(commit ? "Check in everything you have checked out?" : "Undo all of your checkouts?"))
            {
                return;
            }

            var push = commit && PushBox.IsChecked == true;
            var result = await RunAsync(commit ? "Checking in…" : "Undoing checkout…", t => client.CheckinAsync(Target.Name, names, CommitMessageBox.Text, commit, t, push), acceptFailure: true);
            if (result == null)
            {
                return;
            }

            var sha = result.Json.Value<string>("commit");
            var released = TargetInfo.Items(result.Json, "released").Count();
            var notMine = (result.Json["not_checked_out"] as JArray)?.Select(n => n.ToString()).ToList() ?? new List<string>();
            var kept = TargetInfo.Items(result.Json, "held_back").Select(h => h.Value<string>("object")).ToList();
            if (sha != null)
            {
                CommitMessageBox.Clear();
            }

            await RefreshAsync();
            var pushedTo = result.Json.Value<string>("pushed");
            var pushFailed = result.Json.Value<string>("push_error");
            Say((pushFailed != null ? pushFailed + " " : sha != null ? "Committed as " + sha + (pushedTo != null ? " and pushed " + pushedTo : string.Empty) + ". " : string.Empty)
                + "Released " + released + " object(s)." + PulledNote(result.Json)
                + (kept.Count > 0 ? " Still checked out, not committed (changed on both sides or in the repo since your last sync; see Changed in): " + string.Join(", ", kept) + "." : string.Empty)
                + (notMine.Count > 0 ? " Not checked out by you: " + string.Join(", ", notMine) + "." : string.Empty));
        }

        // --- plumbing ----------------------------------------------------------------------

        /// <summary>
        /// Runs one sqlsc command with the buttons disabled. Returns null (after
        /// saying why) when sqlsc reported a problem, unless acceptFailure is set
        /// and it still answered with a JSON document (a clash, a failed step).
        /// </summary>
        private async Task<CliResult> RunAsync(string doing, Func<CancellationToken, Task<CliResult>> action, bool acceptFailure = false)
        {
            if (busy)
            {
                return null;
            }

            busy = true;
            running = new CancellationTokenSource();
            SetEnabled(false);
            Say(doing);
            try
            {
                var result = await action(running.Token);
                if (result.Ok || (acceptFailure && result.Json != null && result.ErrorKind == null))
                {
                    return result;
                }

                if (result.ErrorKind == "cancelled")
                {
                    Say("Cancelled. If it was changing the database, Refresh shows where the database is now.");
                    return null;
                }

                // The status line gets the first sentence; the whole message (a driver's
                // error, a sign-in failure) goes to Output, where it can be read and copied.
                var message = result.ErrorMessage ?? "sqlsc failed.";
                var summary = FirstSentence(message);
                Say(summary == message ? message : summary + " Details are in Output.");
                ShowOutput(message, switchTab: false);
                return null;
            }
            catch (Exception exc) when (!(exc is OutOfMemoryException))
            {
                // Every button handler is async void: an exception escaping it would
                // reach SSMS's dispatcher and can take the whole IDE down.
                Say("The add-in hit an unexpected error; details are in Output.");
                ShowOutput(exc.ToString(), switchTab: false);
                return null;
            }
            finally
            {
                running.Dispose();
                running = null;
                busy = false;
                SetEnabled(true);
            }
        }

        private static string FirstSentence(string text)
        {
            var line = text.Split('\n')[0].Trim();
            var end = line.IndexOf(". ", StringComparison.Ordinal);
            var sentence = end > 0 ? line.Substring(0, end + 1) : line;
            if (sentence.Length > 300)
            {
                sentence = sentence.Substring(0, 297);
            }

            // Cut short (a YAML error's first line, a long driver message): say so.
            return sentence == text.Trim() || sentence.EndsWith(".", StringComparison.Ordinal) ? sentence : sentence.TrimEnd(':', ',', ' ') + "…";
        }

        private void SetEnabled(bool enabled)
        {
            foreach (var button in new[]
            {
                RefreshButton, CommitButton, GetLatestButton, DiffButton, CheckoutButton, CheckinButton, TrackRowsButton,
                LockCheckinButton, UndoCheckoutButton, ForceReleaseButton,
            })
            {
                button.IsEnabled = enabled;
            }

            RepoBox.IsEnabled = enabled;
            TargetBox.IsEnabled = enabled;
            CancelButton.Visibility = enabled ? Visibility.Collapsed : Visibility.Visible;
        }

        private static IList<string> Names(IEnumerable<ChangeInfo> changes)
        {
            return changes.Select(c => c.Name).Distinct().ToList();
        }

        /// <summary>" Pulled 2 commit(s) from origin/main first." when commit or checkin fast-forwarded the branch.</summary>
        private static string PulledNote(JObject json)
        {
            var git = json["git"] as JObject;
            var pulled = git?.Value<int?>("pulled") ?? 0;
            return pulled > 0 ? " Pulled " + pulled + " commit(s) from " + git.Value<string>("upstream") + " first." : string.Empty;
        }

        private static string Warnings(JObject json)
        {
            var warnings = (json["warnings"] as JArray)?.Select(w => w.ToString()).ToList() ?? new List<string>();
            return string.Join(" ", warnings);
        }

        private void ClearGrids()
        {
            ClearRows();
            TargetBox.ItemsSource = null;
        }

        private void ClearRows()
        {
            ChangesGrid.ItemsSource = null;
            LocksGrid.ItemsSource = null;
        }

        private void ShowOutput(string text, bool switchTab = true)
        {
            OutputBox.Text = text.Replace("\r\n", "\n").Replace("\n", Environment.NewLine);
            if (switchTab)
            {
                OutputTab.IsSelected = true;
            }
        }

        private void Say(string text)
        {
            StatusText.Text = text;
        }

        private static bool Confirm(string question)
        {
            return MessageBox.Show(question, "SQL Source Control", MessageBoxButton.YesNo, MessageBoxImage.Question) == MessageBoxResult.Yes;
        }

        private void SaveSettings()
        {
            try
            {
                settings.Save(settingsPath);
            }
            catch (IOException)
            {
                // Remembering recent repos is a convenience; never fail an action over it.
            }
            catch (UnauthorizedAccessException)
            {
            }
        }
    }
}
