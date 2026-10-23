using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Newtonsoft.Json;

namespace CoopSqlSourceControl.Core
{
    /// <summary>
    /// What the add-in remembers between SSMS sessions: the repos opened recently
    /// (their sqlsc.yml), the target last used with each, and where sqlsc is when
    /// it is not on PATH. Kept in %APPDATA%\coop-sqlsc\ssms.json.
    /// </summary>
    public sealed class Settings
    {
        public const int MaxRecent = 10;

        public string SqlscPath { get; set; }

        public List<RecentRepo> Recent { get; set; } = new List<RecentRepo>();

        public static string DefaultPath =>
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "coop-sqlsc", "ssms.json");

        public static Settings Load(string path)
        {
            try
            {
                if (File.Exists(path))
                {
                    var settings = JsonConvert.DeserializeObject<Settings>(File.ReadAllText(path));
                    if (settings != null)
                    {
                        settings.Recent = (settings.Recent ?? new List<RecentRepo>()).Where(r => !string.IsNullOrEmpty(r?.ConfigPath)).ToList();
                        return settings;
                    }
                }
            }
            catch (JsonException)
            {
                // A damaged settings file only loses the recent list.
            }
            catch (IOException)
            {
            }

            return new Settings();
        }

        public void Save(string path)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            File.WriteAllText(path, JsonConvert.SerializeObject(this, Formatting.Indented));
        }

        /// <summary>Moves (or adds) a repo to the top of the recent list.</summary>
        public void Remember(string configPath, string target)
        {
            var existing = Recent.FirstOrDefault(r => SamePath(r.ConfigPath, configPath));
            Recent.RemoveAll(r => SamePath(r.ConfigPath, configPath));
            Recent.Insert(0, new RecentRepo { ConfigPath = configPath, Target = target ?? existing?.Target });
            if (Recent.Count > MaxRecent)
            {
                Recent.RemoveRange(MaxRecent, Recent.Count - MaxRecent);
            }
        }

        /// <summary>
        /// Drops remembered repos whose sqlsc.yml is gone (a deleted clone, a temporary
        /// folder): the list offers only repos that can be opened. Returns what it dropped.
        /// </summary>
        public IList<string> Prune(Func<string, bool> exists)
        {
            var gone = Recent.Where(r => !exists(r.ConfigPath)).Select(r => r.ConfigPath).ToList();
            Recent.RemoveAll(r => gone.Contains(r.ConfigPath));
            return gone;
        }

        public string LastTarget(string configPath)
        {
            return Recent.FirstOrDefault(r => SamePath(r.ConfigPath, configPath))?.Target;
        }

        private static bool SamePath(string a, string b)
        {
            return string.Equals(Normalize(a), Normalize(b), StringComparison.OrdinalIgnoreCase);
        }

        private static string Normalize(string path)
        {
            try
            {
                return Path.GetFullPath(path).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            }
            catch (Exception exc) when (exc is ArgumentException || exc is NotSupportedException || exc is PathTooLongException)
            {
                return path;
            }
        }
    }

    public sealed class RecentRepo
    {
        public string ConfigPath { get; set; }

        public string Target { get; set; }
    }
}
