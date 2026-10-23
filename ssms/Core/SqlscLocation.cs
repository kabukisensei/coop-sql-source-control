using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;

namespace CoopSqlSourceControl.Core
{
    /// <summary>Which sqlsc the add-in runs, and where it came from.</summary>
    public sealed class SqlscLocation
    {
        public const string CoopInstall = "coop's install";
        public const string OnPath = "PATH";
        public const string Chosen = "chosen with Find sqlsc…";

        public SqlscLocation(string path, string source)
        {
            Path = path;
            Source = source;
        }

        public string Path { get; }

        /// <summary>CoopInstall, OnPath or Chosen.</summary>
        public string Source { get; }

        /// <summary>
        /// sqlsc as coop installs it for this Windows user (%USERPROFILE%\.local\bin, the
        /// one `coop update` keeps current), else on PATH, else the one chosen with Find
        /// sqlsc… A chosen path only counts while the file exists and nothing better is
        /// installed, so an old venv forgotten in the settings cannot keep running once
        /// coop's install is there (dev VM finding 1, 2026-10-23). Null when none is found.
        /// </summary>
        public static SqlscLocation Find(string configured)
        {
            return Find(configured, File.Exists, Environment.GetEnvironmentVariable("PATH"),
                Environment.GetFolderPath(Environment.SpecialFolder.UserProfile));
        }

        public static SqlscLocation Find(string configured, Func<string, bool> exists, string pathVariable, string home)
        {
            var names = new[] { "sqlsc.exe", "coop-sqlsc.exe", "sqlsc", "coop-sqlsc" };
            if (!string.IsNullOrEmpty(home))
            {
                var coop = System.IO.Path.Combine(home, ".local", "bin");
                var installed = names.Select(n => System.IO.Path.Combine(coop, n)).FirstOrDefault(exists);
                if (installed != null)
                {
                    return new SqlscLocation(installed, CoopInstall);
                }
            }

            foreach (var folder in (pathVariable ?? string.Empty).Split(System.IO.Path.PathSeparator).Where(f => f.Trim().Length > 0))
            {
                foreach (var name in names)
                {
                    string candidate;
                    try
                    {
                        candidate = System.IO.Path.Combine(folder.Trim().Trim('"'), name);
                    }
                    catch (ArgumentException)
                    {
                        continue;
                    }

                    if (exists(candidate))
                    {
                        return new SqlscLocation(candidate, OnPath);
                    }
                }
            }

            if (!string.IsNullOrWhiteSpace(configured) && exists(configured))
            {
                return new SqlscLocation(configured, Chosen);
            }

            return null;
        }

        /// <summary>"1.0.4" from `sqlsc --version` ("sqlsc, version 1.0.4"), or null.</summary>
        public static string ParseVersion(string output)
        {
            var match = Regex.Match(output ?? string.Empty, @"\bversion\s+(\d+(?:\.\d+)+)", RegexOptions.IgnoreCase);
            return match.Success ? match.Groups[1].Value : null;
        }

        /// <summary>
        /// True when sqlsc and the add-in are not the same major.minor: the JSON they
        /// agree on may have moved. A missing version (an old sqlsc) counts as a mismatch.
        /// </summary>
        public static bool Mismatch(string sqlscVersion, string addinVersion)
        {
            return MajorMinor(sqlscVersion) == null || MajorMinor(sqlscVersion) != MajorMinor(addinVersion);
        }

        private static string MajorMinor(string version)
        {
            var parts = (version ?? string.Empty).Split('.');
            return parts.Length >= 2 ? parts[0] + "." + parts[1] : null;
        }

        /// <summary>The one line the window shows about the sqlsc it uses.</summary>
        public string Describe(string version, string addinVersion)
        {
            var text = "sqlsc " + (version ?? "(version unknown)") + " from " + Source + ": " + Path;
            return Mismatch(version, addinVersion)
                ? text + ". The add-in is " + addinVersion + "; run `coop update` (or reinstall both) so they match."
                : text;
        }
    }
}
