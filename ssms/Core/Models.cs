using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;

namespace CoopSqlSourceControl.Core
{
    /// <summary>A database target from sqlsc.yml (and coop's project.yml).</summary>
    public sealed class TargetInfo
    {
        public string Name { get; set; }
        public string Kind { get; set; }
        public string Server { get; set; }
        public string Database { get; set; }
        public bool IsDefault { get; set; }
        public bool IsProtected { get; set; }
        public string Problem { get; set; }

        public string Display => Problem == null
            ? Name + "  (" + Database + " on " + Server + ")" + (IsProtected ? "  protected" : string.Empty)
            : Name + "  (" + Problem + ")";

        /// <summary>What a screen reader announces for the item (WPF uses ToString, not DisplayMemberPath).</summary>
        public override string ToString() => Display;

        public static IList<TargetInfo> ListFrom(JObject json)
        {
            return Items(json, "targets").Select(t => new TargetInfo
            {
                Name = t.Value<string>("name"),
                Kind = t.Value<string>("kind"),
                Server = t.Value<string>("server"),
                Database = t.Value<string>("database"),
                IsDefault = t.Value<bool?>("default") == true,
                IsProtected = t.Value<bool?>("protected") == true,
                Problem = t.Value<string>("problem"),
            }).ToList();
        }

        internal static IEnumerable<JObject> Items(JObject json, string property)
        {
            var array = json?[property] as JArray;
            return array == null ? Enumerable.Empty<JObject>() : array.OfType<JObject>();
        }
    }

    /// <summary>One object that differs between the database and the repo.</summary>
    public sealed class ChangeInfo
    {
        public string Action { get; set; }
        public string Type { get; set; }
        public string Object { get; set; }
        public string Path { get; set; }
        public string LockedBy { get; set; }
        public bool IsMine { get; set; }

        /// <summary>Which side moved since the last sync: database, repo, both or unknown.</summary>
        public string Origin { get; set; }

        /// <summary>The name to hand back to sqlsc: schema.name, or the schema's own name.</summary>
        public string Name { get; set; }

        public string ActionText => Action == "new" ? "New" : Action == "changed" ? "Changed" : Action == "deleted" ? "Deleted" : Action;

        public string TypeText => TypeLabels.For(Type);

        public string OriginText => OriginLabels.For(Origin);

        /// <summary>The lock owner as the grid shows it: "you" for your own checkout.</summary>
        public string LockedByText => LockedBy == null ? string.Empty : IsMine ? "you" : LockedBy;

        /// <summary>True when the repo moved and the database did not: Get latest, not Commit.</summary>
        public bool IsFromRepo => Origin == "repo";

        public bool IsConflict => Origin == "both";

        /// <summary>What a screen reader announces for the row (WPF uses ToString).</summary>
        public override string ToString() => ActionText + " " + TypeText + " " + Object
            + (OriginText.Length > 0 ? ", changed in " + OriginText.ToLowerInvariant() : string.Empty)
            + (LockedByText.Length > 0 ? ", checked out by " + LockedByText : string.Empty);

        public static IList<ChangeInfo> ListFrom(JObject json)
        {
            var user = json?.Value<string>("user");
            return TargetInfo.Items(json, "changes").Select(c => new ChangeInfo
            {
                Action = c.Value<string>("action"),
                Type = c.Value<string>("type"),
                Object = c.Value<string>("object"),
                Path = c.Value<string>("path"),
                LockedBy = c.Value<string>("locked_by"),
                IsMine = Mine(c, user),
                Origin = c.Value<string>("origin"),
                Name = QualifiedName(c.Value<string>("schema"), c.Value<string>("name")),
            }).ToList();
        }

        /// <summary>sqlsc says `mine` where it knows; older versions only name the user, so compare.</summary>
        internal static bool Mine(JObject item, string user)
        {
            var flag = item.Value<bool?>("mine");
            if (flag.HasValue)
            {
                return flag.Value;
            }

            var lockedBy = item.Value<string>("locked_by");
            return lockedBy != null && user != null && string.Equals(lockedBy, user, StringComparison.OrdinalIgnoreCase);
        }

        internal static string QualifiedName(string schema, string name)
        {
            return string.IsNullOrEmpty(schema) ? "[" + name + "]" : "[" + schema + "].[" + name + "]";
        }
    }

    /// <summary>
    /// What a Commit of these rows would do, worked out from the grid the way sqlsc will:
    /// objects checked out by someone else are left to them, objects the repo moved (Repo
    /// or Both) are left for Get latest or a diff; the rest are committed. The window asks
    /// about, and counts, only the committable ones (dev VM finding 3, 2026-10-23).
    /// </summary>
    public sealed class CommitPlan
    {
        public CommitPlan(IList<ChangeInfo> rows)
        {
            Committable = rows.Where(r => !Locked(r) && !r.IsFromRepo && !r.IsConflict).ToList();
            CheckedOutByOthers = rows.Where(Locked).ToList();
            HeldBack = rows.Where(r => !Locked(r) && (r.IsFromRepo || r.IsConflict)).ToList();
        }

        public IList<ChangeInfo> Committable { get; }

        public IList<ChangeInfo> CheckedOutByOthers { get; }

        public IList<ChangeInfo> HeldBack { get; }

        private static bool Locked(ChangeInfo row)
        {
            return row.LockedBy != null && !row.IsMine;
        }

        /// <summary>"1 is left for Get latest (the repo changed it); 1 is checked out by bob." or empty.</summary>
        public string LeftOut
        {
            get
            {
                var parts = new List<string>();
                if (HeldBack.Count > 0)
                {
                    var both = HeldBack.Count(h => h.IsConflict);
                    parts.Add(HeldBack.Count + (HeldBack.Count == 1 ? " is" : " are") + " left out because the repo changed "
                        + (HeldBack.Count == 1 ? "it" : "them") + (both > 0 ? " (" + both + " on both sides)" : string.Empty)
                        + "; Get latest applies the repo's version, Diff shows it");
                }

                if (CheckedOutByOthers.Count > 0)
                {
                    var owners = string.Join(", ", CheckedOutByOthers.Select(c => c.LockedBy).Distinct());
                    parts.Add(CheckedOutByOthers.Count + (CheckedOutByOthers.Count == 1 ? " is" : " are") + " checked out by " + owners);
                }

                return parts.Count == 0 ? string.Empty : string.Join("; ", parts) + ".";
            }
        }

        /// <summary>The question Commit asks, or null when there is nothing to ask about.</summary>
        public string Question(string target, bool everything)
        {
            if (Committable.Count == 0)
            {
                return null;
            }

            var what = everything ? "every committable change (" + Committable.Count + ")" : Committable.Count + " change(s)";
            return "Commit " + what + " from " + target + "?" + (LeftOut.Length > 0 ? "\n\n" + LeftOut : string.Empty);
        }

        /// <summary>What the window says instead of asking when nothing can be committed.</summary>
        public string NothingToCommit(string target)
        {
            return "Nothing to commit from " + target + ": " + (LeftOut.Length > 0 ? LeftOut : "nothing differs.");
        }
    }

    /// <summary>Someone's checkout of one object.</summary>
    public sealed class LockInfo
    {
        public string Object { get; set; }
        public string Type { get; set; }
        public string Name { get; set; }
        public string LockedBy { get; set; }
        public bool IsMine { get; set; }
        public DateTimeOffset? LockedAt { get; set; }
        public string Note { get; set; }

        public string TypeText => TypeLabels.For(Type);

        /// <summary>The lock owner as the grid shows it: "you" for your own checkout.</summary>
        public string LockedByText => IsMine ? "you" : LockedBy;

        public string LockedAtText => LockedAt.HasValue ? LockedAt.Value.ToLocalTime().ToString("yyyy-MM-dd HH:mm") : string.Empty;

        /// <summary>What a screen reader announces for the row (WPF uses ToString).</summary>
        public override string ToString() => Object + " checked out by " + LockedByText;

        public static IList<LockInfo> ListFrom(JObject json, string property)
        {
            var user = json?.Value<string>("user");
            return TargetInfo.Items(json, property).Select(l => new LockInfo
            {
                Object = l.Value<string>("object"),
                Type = l.Value<string>("type"),
                Name = ChangeInfo.QualifiedName(l.Value<string>("schema"), l.Value<string>("name")),
                LockedBy = l.Value<string>("locked_by"),
                IsMine = ChangeInfo.Mine(l, user),
                LockedAt = ParseTime(l["locked_at"]),
                Note = l.Value<string>("note"),
            }).ToList();
        }

        private static DateTimeOffset? ParseTime(JToken token)
        {
            if (token == null || token.Type == JTokenType.Null)
            {
                return null;
            }

            if (token.Type == JTokenType.Date)
            {
                var value = token.Value<DateTime>();
                return new DateTimeOffset(DateTime.SpecifyKind(value, value.Kind == DateTimeKind.Unspecified ? DateTimeKind.Utc : value.Kind));
            }

            DateTimeOffset parsed;
            return DateTimeOffset.TryParse(token.ToString(), out parsed) ? parsed : (DateTimeOffset?)null;
        }
    }

    /// <summary>Migration scripts (Migrations/*.sql) as sqlsc reports them.</summary>
    public static class MigrationInfo
    {
        /// <summary>The ids of scripts `status` says this database has not run yet.</summary>
        public static IList<string> PendingFrom(JObject status)
        {
            var block = status?["migrations"] as JObject;
            return Ids(block, "pending");
        }

        /// <summary>The ids of scripts a get-latest or deploy answer ran, or would run.</summary>
        public static IList<string> RanFrom(JObject update)
        {
            return Ids(update, "migrations");
        }

        private static IList<string> Ids(JObject json, string property)
        {
            return TargetInfo.Items(json, property).Select(m => m.Value<string>("id")).Where(id => id != null).ToList();
        }
    }

    public static class OriginLabels
    {
        /// <summary>The "Changed in" column: empty when sqlsc has no sync record for the object.</summary>
        public static string For(string origin)
        {
            switch (origin)
            {
                case "database": return "Database";
                case "repo": return "Repo";
                case "both": return "Both";
                default: return string.Empty;
            }
        }
    }

    public static class TypeLabels
    {
        public static string For(string type)
        {
            switch (type)
            {
                case "schema": return "Schema";
                case "table": return "Table";
                case "view": return "View";
                case "function": return "Function";
                case "procedure": return "Stored procedure";
                case "trigger": return "Trigger";
                case "data": return "Static data";
                default: return type;
            }
        }
    }
}
