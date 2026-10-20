using System;
using System.IO;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using CoopSqlSourceControl.Core;
using Xunit;

namespace CoopSqlSourceControl.Core.Tests
{
    public class CommandLineTests
    {
        [Theory]
        [InlineData("status", "status")]
        [InlineData("", "\"\"")]
        [InlineData("dbo.My Table", "\"dbo.My Table\"")]
        [InlineData("say \"hi\"", "\"say \\\"hi\\\"\"")]
        [InlineData(@"C:\repo dir\", "\"C:\\repo dir\\\\\"")]
        [InlineData(@"C:\repo\sqlsc.yml", @"C:\repo\sqlsc.yml")]
        public void QuotesOnlyWhatNeedsIt(string argument, string expected)
        {
            Assert.Equal(expected, CommandLine.Quote(argument));
        }

        [Fact]
        public void JoinsWithSpaces()
        {
            Assert.Equal("commit -m \"Add order count\" [sales].[Order]",
                CommandLine.Join(new[] { "commit", "-m", "Add order count", "[sales].[Order]" }));
        }

        [Fact]
        public void ArgumentsPutJsonAndConfigFirst()
        {
            var args = SqlscRunner.Arguments(@"C:\r\sqlsc.yml", new[] { "status", "--target", "dev" });
            Assert.Equal(new[] { "--json", "--config", @"C:\r\sqlsc.yml", "status", "--target", "dev" }, args);
        }
    }

    public class ResultTests
    {
        private const string Status = @"{
  ""ok"": true,
  ""command"": ""status"",
  ""user"": ""ada@example.com"",
  ""changes"": [
    {""action"": ""changed"", ""type"": ""view"", ""schema"": ""sales"", ""name"": ""vTotals"", ""object"": ""[sales].[vTotals]"", ""path"": ""Views/sales.vTotals.sql"", ""origin"": ""database"", ""locked_by"": ""ada@example.com"", ""mine"": true},
    {""action"": ""new"", ""type"": ""schema"", ""schema"": """", ""name"": ""audit"", ""object"": ""[audit]"", ""path"": ""Security/Schemas/audit.sql"", ""origin"": ""unknown"", ""locked_by"": null, ""mine"": false},
    {""action"": ""deleted"", ""type"": ""data"", ""schema"": ""dbo"", ""name"": ""Status"", ""object"": ""[dbo].[Status]"", ""path"": ""Data/dbo.Status.sql"", ""origin"": ""repo"", ""locked_by"": ""bob@example.com""}
  ],
  ""locks"": [
    {""object"": ""[sales].[vTotals]"", ""type"": ""view"", ""schema"": ""sales"", ""name"": ""vTotals"", ""locked_by"": ""ada@example.com"", ""locked_at"": ""2026-10-13T12:10:00+00:00"", ""note"": ""widening"", ""repo"": ""github.com/x/y""}
  ]
}";

        [Fact]
        public void ReadsStatus()
        {
            var result = CliResult.FromOutput(0, Status, string.Empty);
            Assert.True(result.Ok);
            var changes = ChangeInfo.ListFrom(result.Json);
            Assert.Equal(new[] { "Changed", "New", "Deleted" }, changes.Select(c => c.ActionText));
            Assert.Equal(new[] { "View", "Schema", "Static data" }, changes.Select(c => c.TypeText));
            Assert.Equal(new[] { "[sales].[vTotals]", "[audit]", "[dbo].[Status]" }, changes.Select(c => c.Name));
            Assert.Equal("ada@example.com", changes[0].LockedBy);
            Assert.Null(changes[1].LockedBy);

            var lockInfo = Assert.Single(LockInfo.ListFrom(result.Json, "locks"));
            Assert.Equal("widening", lockInfo.Note);
            Assert.Equal(new DateTimeOffset(2026, 10, 13, 12, 10, 0, TimeSpan.Zero), lockInfo.LockedAt.Value.ToUniversalTime());
        }

        [Fact]
        public void ShowsYouForYourOwnCheckoutsAndWhichSideMoved()
        {
            var json = CliResult.FromOutput(0, Status, string.Empty).Json;
            var changes = ChangeInfo.ListFrom(json);
            Assert.Equal(new[] { "you", "", "bob@example.com" }, changes.Select(c => c.LockedByText));
            Assert.Equal(new[] { "Database", "", "Repo" }, changes.Select(c => c.OriginText));
            Assert.True(changes[2].IsFromRepo);
            Assert.Contains("changed in repo, checked out by bob@example.com", changes[2].ToString());

            // The lock list has no `mine` flag: the user name decides, case-insensitively.
            var lockInfo = Assert.Single(LockInfo.ListFrom(json, "locks"));
            Assert.True(lockInfo.IsMine);
            Assert.Equal("you", lockInfo.LockedByText);
            Assert.Equal("[sales].[vTotals] checked out by you", lockInfo.ToString());
        }

        [Fact]
        public void ReadsErrors()
        {
            var result = CliResult.FromOutput(2, "{\"ok\": false, \"error\": {\"kind\": \"connect\", \"message\": \"Sign in failed.\"}}", "");
            Assert.False(result.Ok);
            Assert.Equal("connect", result.ErrorKind);
            Assert.Equal("Sign in failed.", result.ErrorMessage);
        }

        [Fact]
        public void ACrashKeepsTheLastLinesOfStderr()
        {
            var result = CliResult.FromOutput(1, "", "Traceback (most recent call last):\n  File x\nModuleNotFoundError: No module named 'pyodbc'\n");
            Assert.False(result.Ok);
            Assert.Equal("crash", result.ErrorKind);
            Assert.Contains("No module named 'pyodbc'", result.ErrorMessage);
        }

        [Fact]
        public void ReadsTargets()
        {
            var json = CliResult.FromOutput(0, @"{""ok"": true, ""targets"": [
                {""name"": ""dev"", ""kind"": ""azure_sql"", ""server"": ""s"", ""database"": ""Sales"", ""default"": true, ""protected"": false, ""problem"": null},
                {""name"": ""gold"", ""default"": false, ""problem"": ""Target 'gold' has no server.""}]}", "").Json;
            var targets = TargetInfo.ListFrom(json);
            Assert.Equal("dev  (Sales on s)", targets[0].Display);
            Assert.True(targets[0].IsDefault);
            Assert.Equal("gold  (Target 'gold' has no server.)", targets[1].Display);
            // Screen readers read ToString, not DisplayMemberPath.
            Assert.Equal(targets[0].Display, targets[0].ToString());
        }
    }

    public class SettingsTests
    {
        [Fact]
        public void RemembersReposNewestFirstAndKeepsTheirTarget()
        {
            var settings = new Settings();
            settings.Remember(Path.Combine(Path.GetTempPath(), "a", "sqlsc.yml"), "dev");
            settings.Remember(Path.Combine(Path.GetTempPath(), "b", "sqlsc.yml"), "test");
            settings.Remember(Path.Combine(Path.GetTempPath(), "a", "sqlsc.yml"), null);
            Assert.Equal(2, settings.Recent.Count);
            Assert.EndsWith(Path.Combine("a", "sqlsc.yml"), settings.Recent[0].ConfigPath);
            Assert.Equal("dev", settings.LastTarget(Path.Combine(Path.GetTempPath(), "a", "sqlsc.yml")));
        }

        [Fact]
        public void SavesAndLoadsAndSurvivesADamagedFile()
        {
            var path = Path.Combine(Path.GetTempPath(), "sqlsc-tests-" + Guid.NewGuid().ToString("N"), "ssms.json");
            var settings = new Settings { SqlscPath = @"C:\tools\sqlsc.exe" };
            settings.Remember("/repo/sqlsc.yml", "dev");
            settings.Save(path);
            var loaded = Settings.Load(path);
            Assert.Equal(@"C:\tools\sqlsc.exe", loaded.SqlscPath);
            Assert.Equal("dev", loaded.LastTarget("/repo/sqlsc.yml"));

            File.WriteAllText(path, "{ not json");
            Assert.Empty(Settings.Load(path).Recent);
        }

        [Fact]
        public void KeepsTenRecentRepos()
        {
            var settings = new Settings();
            for (var i = 0; i < 15; i++)
            {
                settings.Remember("/repo" + i + "/sqlsc.yml", "dev");
            }

            Assert.Equal(Settings.MaxRecent, settings.Recent.Count);
            Assert.Equal("/repo14/sqlsc.yml", settings.Recent[0].ConfigPath);
        }
    }

    public class RunnerTests
    {
        // Runs a stand-in for sqlsc (a python script printing its arguments as JSON)
        // to check the process plumbing and quoting end to end. Linux only, where
        // the CI job runs these tests.
        [Fact]
        public async Task RunsAProcessAndReadsItsJson()
        {
            if (!OperatingSystem.IsLinux() || !File.Exists("/usr/bin/python3"))
            {
                return;
            }

            var script = Path.Combine(Path.GetTempPath(), "fake-sqlsc-" + Guid.NewGuid().ToString("N"));
            File.WriteAllText(script, "#!/usr/bin/python3\nimport json, sys\nprint(json.dumps({'ok': True, 'args': sys.argv[1:]}))\n");
            File.SetUnixFileMode(script, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
            var runner = new SqlscRunner(script);
            var result = await runner.RunAsync(null, new[] { "commit", "-m", "say \"hi\" to [dbo].[A B]" }, CancellationToken.None);
            Assert.True(result.Ok, result.ErrorMessage);
            Assert.Equal(
                new[] { "--json", "commit", "-m", "say \"hi\" to [dbo].[A B]" },
                result.Json["args"].ToObject<string[]>());
        }

        [Fact]
        public async Task AMissingExecutableIsAnError()
        {
            var runner = new SqlscRunner(Path.Combine(Path.GetTempPath(), "no-such-sqlsc-" + Guid.NewGuid().ToString("N")));
            var result = await runner.RunAsync(null, new[] { "status" }, CancellationToken.None);
            Assert.False(result.Ok);
            Assert.Equal("not_found", result.ErrorKind);
        }
    }
}
