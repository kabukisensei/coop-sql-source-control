using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;

namespace CoopSqlSourceControl.Core
{
    /// <summary>
    /// The status line after a get-latest that ran, from sqlsc's answer. A failed run is
    /// not always a run that changed nothing: a target without transactions (a Fabric
    /// Warehouse) keeps the steps that ran before the failure, and "before" migration
    /// scripts commit on their own ahead of the structural changes. sqlsc says which in
    /// `done` (what ran and stays) and `transaction`; without them the outcome is unknown.
    /// </summary>
    public static class GetLatestReport
    {
        public static string Describe(string target, JObject result)
        {
            var failed = TargetInfo.Items(result, "failed")
                .Select(f => f.Value<string>("object") + ": " + Sentence(f.Value<string>("error")))
                .ToList();
            var kept = TargetInfo.Items(result, "kept").Select(k => k.Value<string>("object")).ToList();
            var text = failed.Count == 0 ? "Updated " + target + "." : Failure(target, result, failed);
            if (kept.Count > 0)
            {
                text += " Kept as they are, because " + target + " changed them since your last sync (commit them first): "
                    + string.Join(", ", kept) + ".";
            }

            return text;
        }

        /// <summary>When get-latest gave no answer to read (it was cancelled, or stopped
        /// without one) after it may have started changing the database.</summary>
        public static string Unknown(string target, string why)
        {
            return (string.IsNullOrWhiteSpace(why) ? string.Empty : why.Trim() + " ")
                + "Whether get latest changed " + target + " before it stopped is not known. "
                + "Refresh shows where " + target + " is now; check it before you run get latest again.";
        }

        private static string Failure(string target, JObject result, IList<string> failed)
        {
            var text = "Get latest failed on " + target + ": " + string.Join(" ", failed);
            var transaction = result?["transaction"];
            var doneArray = result?["done"] as JArray;
            if (transaction == null || transaction.Type != JTokenType.Boolean || doneArray == null)
            {
                return text + " sqlsc did not say what stayed applied, so what changed on " + target
                    + " is not known. Refresh and review the differences before you retry.";
            }

            var done = doneArray.Select(d => d.ToString()).ToList();
            var notRun = NotRun(result, done, FailedObjects(result));
            var notRunText = notRun.Count > 0 ? " Not applied: " + string.Join(", ", notRun) + "." : string.Empty;
            var heldBack = TargetInfo.Items(result, "held_back").Select(s => s.Value<string>("object")).ToList();
            var heldText = heldBack.Count > 0 ? " Held back for a manual change: " + string.Join(", ", heldBack) + "." : string.Empty;

            if (transaction.Value<bool>())
            {
                if (done.Count == 0)
                {
                    return text + " Every change was rolled back, so " + target + " is as it was." + heldText;
                }

                return text + " The rest was rolled back, but these were committed before it and stay applied: "
                    + string.Join(", ", done) + "." + notRunText + heldText
                    + " Refresh and review the differences before you retry.";
            }

            // No transaction: what ran stays, and the failed step may have run part way.
            return text + " " + target + " has no transactions, so nothing was rolled back. "
                + (done.Count == 0 ? "No step had finished" : "These stay applied: " + string.Join(", ", done))
                + ", and the failed step may have run part way." + notRunText + heldText
                + " Refresh and review the differences before you retry.";
        }

        private static HashSet<string> FailedObjects(JObject result)
        {
            return new HashSet<string>(TargetInfo.Items(result, "failed").Select(f => f.Value<string>("object")).Where(o => o != null));
        }

        /// <summary>The planned steps that neither stayed applied nor failed, in plan order.</summary>
        private static IList<string> NotRun(JObject result, IList<string> done, HashSet<string> failedObjects)
        {
            var applied = new HashSet<string>(done);
            return TargetInfo.Items(result, "steps")
                .Where(s => s.Value<string>("action") != "skip" && s.Value<string>("action") != "manual")
                .Select(s => s.Value<string>("object"))
                .Where(o => o != null && !applied.Contains(o) && !failedObjects.Contains(o))
                .Distinct()
                .ToList();
        }

        private static string Sentence(string text)
        {
            var trimmed = (text ?? string.Empty).Trim().TrimEnd('.');
            return trimmed.Length == 0 ? "failed." : trimmed + ".";
        }
    }
}
