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

        /// <summary>The name to hand back to sqlsc: schema.name, or the schema's own name.</summary>
        public string Name { get; set; }

        public string ActionText => Action == "new" ? "New" : Action == "changed" ? "Changed" : Action == "deleted" ? "Deleted" : Action;

        public string TypeText => TypeLabels.For(Type);

        /// <summary>What a screen reader announces for the row (WPF uses ToString).</summary>
        public override string ToString() => ActionText + " " + TypeText + " " + Object;

        public static IList<ChangeInfo> ListFrom(JObject json)
        {
            return TargetInfo.Items(json, "changes").Select(c => new ChangeInfo
            {
                Action = c.Value<string>("action"),
                Type = c.Value<string>("type"),
                Object = c.Value<string>("object"),
                Path = c.Value<string>("path"),
                LockedBy = c.Value<string>("locked_by"),
                Name = QualifiedName(c.Value<string>("schema"), c.Value<string>("name")),
            }).ToList();
        }

        internal static string QualifiedName(string schema, string name)
        {
            return string.IsNullOrEmpty(schema) ? "[" + name + "]" : "[" + schema + "].[" + name + "]";
        }
    }

    /// <summary>Someone's checkout of one object.</summary>
    public sealed class LockInfo
    {
        public string Object { get; set; }
        public string Type { get; set; }
        public string Name { get; set; }
        public string LockedBy { get; set; }
        public DateTimeOffset? LockedAt { get; set; }
        public string Note { get; set; }

        public string TypeText => TypeLabels.For(Type);

        public string LockedAtText => LockedAt.HasValue ? LockedAt.Value.ToLocalTime().ToString("yyyy-MM-dd HH:mm") : string.Empty;

        /// <summary>What a screen reader announces for the row (WPF uses ToString).</summary>
        public override string ToString() => Object + " checked out by " + LockedBy;

        public static IList<LockInfo> ListFrom(JObject json, string property)
        {
            return TargetInfo.Items(json, property).Select(l => new LockInfo
            {
                Object = l.Value<string>("object"),
                Type = l.Value<string>("type"),
                Name = ChangeInfo.QualifiedName(l.Value<string>("schema"), l.Value<string>("name")),
                LockedBy = l.Value<string>("locked_by"),
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
