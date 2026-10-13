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
            await RefreshAsync();
        }

        private async void OnRefresh(object sender, RoutedEventArgs e)
        {
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
        }

        private async void OnCommit(object sender, RoutedEventArgs e)
        {
            if (client == null || Target == null)
            {
                return;
            }

            var selected = SelectedChanges;
            if (selected.Count == 0 && !Confirm("Commit every change (" + Count(ChangesGrid) + ") from " + Target.Name + "?"))
            {
                return;
            }

            var result = await RunAsync("Committing…", t => client.CommitAsync(Target.Name, Names(selected), CommitMessageBox.Text, t));
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

            Say((sha == null ? "Nothing to commit." : "Committed as " + sha + ".")
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
            var selected = SelectedChanges;
            if (client == null || Target == null || selected.Count == 0)
            {
                Say("Select the objects to check out.");
                return;
            }

            var result = await RunAsync("Checking out…", t => client.CheckoutAsync(Target.Name, Names(selected), null, t), acceptFailure: true);
            if (result == null)
            {
                return;
            }

            var taken = LockInfo.ListFrom(result.Json, "conflicts");
            await RefreshAsync();
            Say(taken.Count == 0
                ? "Checked out " + selected.Count + " object(s)."
                : "Checked out by someone else: " + string.Join(", ", taken.Select(l => l.Object + " (" + l.LockedBy + ")")) + ".");
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

            var result = await RunAsync(commit ? "Checking in…" : "Undoing checkout…", t => client.CheckinAsync(Target.Name, names, CommitMessageBox.Text, commit, t), acceptFailure: true);
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
            Say((sha != null ? "Committed as " + sha + ". " : string.Empty)
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
            SetEnabled(false);
            Say(doing);
            try
            {
                var result = await action(CancellationToken.None);
                if (result.Ok || (acceptFailure && result.Json != null && result.ErrorKind == null))
                {
                    return result;
                }

                Say(result.ErrorMessage ?? "sqlsc failed.");
                if (result.ErrorKind == "connect" || result.ErrorKind == "config" || result.ErrorKind == "crash")
                {
                    ShowOutput(result.ErrorMessage ?? string.Empty, switchTab: false);
                }

                return null;
            }
            finally
            {
                busy = false;
                SetEnabled(true);
            }
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
            ChangesGrid.ItemsSource = null;
            LocksGrid.ItemsSource = null;
            TargetBox.ItemsSource = null;
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
