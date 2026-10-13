using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace CoopSqlSourceControl.Core
{
    /// <summary>What one `sqlsc --json …` run returned.</summary>
    public sealed class CliResult
    {
        public CliResult(int exitCode, JObject json, string errorKind, string errorMessage)
        {
            ExitCode = exitCode;
            Json = json;
            ErrorKind = errorKind;
            ErrorMessage = errorMessage;
        }

        public int ExitCode { get; }

        /// <summary>The JSON document, or null when sqlsc printed none (it crashed or was not found).</summary>
        public JObject Json { get; }

        public string ErrorKind { get; }

        public string ErrorMessage { get; }

        /// <summary>True when sqlsc reported `"ok": true`.</summary>
        public bool Ok => Json != null && Json.Value<bool?>("ok") == true;

        public static CliResult FromOutput(int exitCode, string stdout, string stderr)
        {
            JObject json = null;
            var text = (stdout ?? string.Empty).Trim();
            if (text.Length > 0)
            {
                try
                {
                    json = JObject.Parse(text);
                }
                catch (JsonReaderException)
                {
                    json = null;
                }
            }

            if (json == null)
            {
                var detail = FirstLines(string.IsNullOrWhiteSpace(stderr) ? text : stderr);
                return new CliResult(exitCode, null, "crash", "sqlsc stopped without an answer" + (detail.Length > 0 ? ": " + detail : "."));
            }

            var error = json["error"] as JObject;
            return error == null
                ? new CliResult(exitCode, json, null, null)
                : new CliResult(exitCode, json, error.Value<string>("kind"), error.Value<string>("message"));
        }

        private static string FirstLines(string text)
        {
            var lines = (text ?? string.Empty).Split('\n').Select(l => l.TrimEnd('\r')).Where(l => l.Trim().Length > 0);
            return string.Join(" ", lines.Reverse().Take(3).Reverse()).Trim();
        }
    }

    /// <summary>Runs the sqlsc command line and reads its JSON. Every call is one process.</summary>
    public sealed class SqlscRunner
    {
        private readonly string executable;

        public SqlscRunner(string executable)
        {
            this.executable = executable;
        }

        public TimeSpan Timeout { get; set; } = TimeSpan.FromMinutes(10);

        /// <summary>
        /// sqlsc as configured, else on PATH, else where pipx puts it by default.
        /// Returns null when it cannot be found.
        /// </summary>
        public static string Find(string configured)
        {
            if (!string.IsNullOrWhiteSpace(configured))
            {
                return File.Exists(configured) ? configured : null;
            }

            var names = new[] { "sqlsc.exe", "coop-sqlsc.exe", "sqlsc" };
            var path = Environment.GetEnvironmentVariable("PATH") ?? string.Empty;
            foreach (var folder in path.Split(Path.PathSeparator).Where(f => f.Trim().Length > 0))
            {
                foreach (var name in names)
                {
                    string candidate;
                    try
                    {
                        candidate = Path.Combine(folder.Trim().Trim('"'), name);
                    }
                    catch (ArgumentException)
                    {
                        continue;
                    }

                    if (File.Exists(candidate))
                    {
                        return candidate;
                    }
                }
            }

            var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            var pipx = Path.Combine(home, ".local", "bin", "sqlsc.exe");
            return File.Exists(pipx) ? pipx : null;
        }

        public static IList<string> Arguments(string configPath, IEnumerable<string> command)
        {
            var args = new List<string> { "--json" };
            if (!string.IsNullOrEmpty(configPath))
            {
                args.Add("--config");
                args.Add(configPath);
            }

            args.AddRange(command);
            return args;
        }

        public async Task<CliResult> RunAsync(string configPath, IEnumerable<string> command, CancellationToken cancellationToken)
        {
            var args = Arguments(configPath, command);
            var start = new ProcessStartInfo(executable, CommandLine.Join(args))
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
                WorkingDirectory = string.IsNullOrEmpty(configPath)
                    ? Environment.CurrentDirectory
                    : Path.GetDirectoryName(Path.GetFullPath(configPath)),
            };
            start.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            start.EnvironmentVariables["NO_COLOR"] = "1";

            using (var process = new Process { StartInfo = start })
            {
                try
                {
                    process.Start();
                }
                catch (System.ComponentModel.Win32Exception exc)
                {
                    return new CliResult(-1, null, "not_found", "Could not start sqlsc (" + executable + "): " + exc.Message);
                }

                // Read both pipes at once, so a full stderr buffer can never stall the run.
                var stdout = process.StandardOutput.ReadToEndAsync();
                var stderr = process.StandardError.ReadToEndAsync();
                var exited = Task.Run(() => process.WaitForExit((int)Timeout.TotalMilliseconds));
                var finished = await Task.WhenAny(exited, Task.Delay(System.Threading.Timeout.Infinite, cancellationToken)).ConfigureAwait(false);
                if (finished != exited || !exited.Result)
                {
                    TryKill(process);
                    return finished != exited
                        ? new CliResult(-1, null, "cancelled", "Cancelled.")
                        : new CliResult(-1, null, "timeout", "sqlsc did not finish within " + Timeout.TotalMinutes + " minutes.");
                }

                return CliResult.FromOutput(process.ExitCode, await stdout.ConfigureAwait(false), await stderr.ConfigureAwait(false));
            }
        }

        private static void TryKill(Process process)
        {
            try
            {
                if (!process.HasExited)
                {
                    process.Kill();
                }
            }
            catch (InvalidOperationException)
            {
            }
            catch (System.ComponentModel.Win32Exception)
            {
            }
        }
    }
}
