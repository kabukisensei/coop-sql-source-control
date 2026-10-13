using System;

namespace CoopSqlSourceControl
{
    /// <summary>The GUIDs and IDs the .vsct command table and the package share.</summary>
    internal static class PackageIds
    {
        public const string PackageGuidString = "772e6ac3-d552-4127-ba5f-3c25fd661535";
        public const string CommandSetGuidString = "97408c30-cee9-4f28-9089-0df5d62f50f7";
        public const string WindowGuidString = "18150326-ca43-49a4-9e5d-25e9e3daad32";

        public static readonly Guid CommandSet = new Guid(CommandSetGuidString);

        public const int OpenWindowCommand = 0x0100;
    }
}
