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
            if (FindSqlsc() == null)
            {
                return;
            }

            ShowRecent();
            var last = settings.Recent.FirstOrDefault(r => File.Exists(r.ConfigPath));
            if (last != null)
            {
                await OpenRepoAsync(last.ConfigPath);
            }
        }

        // --- repo and target -------------------------------------------------------------

        private string FindSqlsc()
        {
            var exe = SqlscRunner.Find(settings.SqlscPath);
            LocateButton.Visibility = exe == null ? Visibility.Visible : Visibility.Collapsed;
            if (exe == null)
            {
                Say("sqlsc was not found. Install it with `pipx install git+https://github.com/kabukisensei/coop-sql-source-control`, "
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

        private void OnLocateSqlsc(object sender, RoutedEventArgs e)
        {
            var dialog = new Microsoft.Win32.OpenFileDialog { Title = "Find sqlsc.exe", Filter = "sqlsc|sqlsc.exe;coop-sqlsc.exe|Programs|*.exe" };
            if (dialog.ShowDialog() != true)
            {
                return;
            }

            settings.SqlscPath = dialog.FileName;
            SaveSettings();
            if (FindSqlsc() != null)
            {
                Say("Using " + dialog.FileName + ". Open a repo's sqlsc.yml to start.");
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
            Say((changes.Count == 0
                    ? "The repo matches " + target.Name + "."
                    : changes.Count + " object(s) differ between " + target.Name + " and the repo.")
                + (warnings.Length > 0 ? " " + warnings : string.Empty));
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
            var affected = selected.Count > 0 ? selected : (ChangesGrid.ItemsSource as IEnumerable<ChangeInfo> ?? Enumerable.Empty<ChangeInfo>()).ToList();
            var deletions = affected.Count(c => c.Action == "deleted");
            // "Deleted" means the repo has the object and the database does not: dropped here,
            // or a teammate's new object not applied with Get latest yet. Committing removes the file.
            var warning = deletions == 0 ? string.Empty
                : Environment.NewLine + Environment.NewLine + deletions + " of them would delete the object's file from the repo, because "
                    + Target.Name + " does not have it. If it is new from the repo and not applied yet, use Get latest instead.";
            if ((selected.Count == 0 || deletions > 0)
                && !Confirm((selected.Count == 0 ? "Commit every change (" + Count(ChangesGrid) + ") from " : "Commit " + selected.Count + " change(s) from ")
                    + Target.Name + "?" + warning))
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
            await RefreshAsync();
            if (sha != null)
            {
                CommitMessageBox.Clear();
            }

            var pushed = result.Json.Value<string>("pushed");
            var pushError = result.Json.Value<string>("push_error");
            Say((pushError ?? (sha == null ? "Nothing to commit." : "Committed as " + sha + (pushed != null ? " and pushed " + pushed : string.Empty) + "."))
                + (skipped.Count > 0 ? " Left for whoever has them checked out: " + string.Join(", ", skipped) + "." : string.Empty));
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

            Say("Review the script in Output, then answer the question.");
            if (!Confirm("Run this script against " + target.Name + " (" + target.Database + ")? It makes " + steps + " change(s)."))
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
            await RefreshAsync();
            Say(failed.Count == 0
                ? "Updated " + target.Name + "."
                : "Get latest failed, so nothing was changed: " + string.Join("; ", failed));
        }

        private async void OnCheckout(object sender, RoutedEventArgs e)
        {
            if (client == null || Target == null)
            {
                return;
            }

            // Objects are checked out before they are edited, when they do not differ yet and
            // so are not in the grid: with nothing selected, ask for their names.
            var names = Names(SelectedChanges);
            if (names.Count == 0)
            {
                var typed = Ask("Check out", "Objects to check out, separated by commas (for example dbo.Customer, sales.vTotals):");
                names = (typed ?? string.Empty).Split(new[] { ',', ';' }, StringSplitOptions.RemoveEmptyEntries)
                    .Select(n => n.Trim()).Where(n => n.Length > 0).Distinct().ToList();
                if (names.Count == 0)
                {
                    return;
                }
            }

            var result = await RunAsync("Checking out…", t => client.CheckoutAsync(Target.Name, names, null, t), acceptFailure: true);
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
            var box = new TextBox { Name = "AskBox", MinWidth = 380, Margin = new Thickness(0, 6, 0, 10) };
            var ok = new Button { Content = "OK", IsDefault = true, MinWidth = 70, Margin = new Thickness(0, 0, 6, 0) };
            var cancel = new Button { Content = "Cancel", IsCancel = true, MinWidth = 70 };
            var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
            buttons.Children.Add(ok);
            buttons.Children.Add(cancel);
            var panel = new StackPanel { Margin = new Thickness(12) };
            panel.Children.Add(new TextBlock { Text = prompt, TextWrapping = TextWrapping.Wrap, MaxWidth = 380 });
            panel.Children.Add(box);
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
            System.Windows.Automation.AutomationProperties.SetAutomationId(box, "AskBox");
            ok.Click += (s, a) => window.DialogResult = true;
            window.Loaded += (s, a) => box.Focus();
            return window.ShowDialog() == true ? box.Text : null;
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
            if (sha != null)
            {
                CommitMessageBox.Clear();
            }

            await RefreshAsync();
            var pushedTo = result.Json.Value<string>("pushed");
            var pushFailed = result.Json.Value<string>("push_error");
            Say((pushFailed != null ? pushFailed + " " : sha != null ? "Committed as " + sha + (pushedTo != null ? " and pushed " + pushedTo : string.Empty) + ". " : string.Empty)
                + "Released " + released + " object(s)."
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
                RefreshButton, CommitButton, GetLatestButton, DiffButton, CheckoutButton, CheckinButton,
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

        private static int Count(DataGrid grid)
        {
            return grid.Items.Count;
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
