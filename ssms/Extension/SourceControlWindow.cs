using System.Runtime.InteropServices;
using Microsoft.VisualStudio.Shell;

namespace CoopSqlSourceControl
{
    [Guid(PackageIds.WindowGuidString)]
    public sealed class SourceControlWindow : ToolWindowPane
    {
        public SourceControlWindow()
            : base(null)
        {
            Caption = "SQL Source Control";
            Content = new SourceControlControl();
        }
    }
}
