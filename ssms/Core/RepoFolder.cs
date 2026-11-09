using System;
using System.IO;

namespace CoopSqlSourceControl.Core
{
    /// <summary>Where the "Open a repo's sqlsc.yml" dialog starts.</summary>
    public static class RepoFolder
    {
        /// <summary>How many folders above the open solution to look for sqlsc.yml.</summary>
        public const int Levels = 4;

        /// <summary>
        /// The folder holding sqlsc.yml at or above the open solution's folder (SSMS opens the
        /// SQL project inside the scripts folder, one level below sqlsc.yml: dev VM check,
        /// 2026-11-09), else the folder of the repo opened last, else null (the dialog's own
        /// choice).
        /// </summary>
        public static string StartFolder(string solutionFolder, string lastConfig, Func<string, bool> fileExists)
        {
            var folder = string.IsNullOrEmpty(solutionFolder) ? null : solutionFolder.TrimEnd('\\', '/');
            for (var level = 0; folder != null && level <= Levels; level++)
            {
                if (fileExists(Path.Combine(folder, "sqlsc.yml")))
                {
                    return folder;
                }

                folder = Path.GetDirectoryName(folder);
            }

            return string.IsNullOrEmpty(lastConfig) ? null : Path.GetDirectoryName(lastConfig);
        }
    }
}
