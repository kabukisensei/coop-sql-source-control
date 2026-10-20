using System;
using System.ComponentModel.Design;
using System.Runtime.InteropServices;
using System.Threading;
using Microsoft.VisualStudio.Shell;
using Task = System.Threading.Tasks.Task;

namespace CoopSqlSourceControl
{
    /// <summary>
    /// Adds Tools > SQL Source Control, which opens the window. Everything the
    /// window does goes through the sqlsc command line, so the add-in and the CLI
    /// always agree.
    /// </summary>
    [PackageRegistration(UseManagedResourcesOnly = true, AllowsBackgroundLoading = true)]
    [InstalledProductRegistration("Coop SQL Source Control", "Source control for SQL Server, Azure SQL and Fabric, through sqlsc.", "1.0.0")]
    [ProvideMenuResource("Menus.ctmenu", 1)]
    [ProvideToolWindow(typeof(SourceControlWindow), Style = VsDockStyle.Tabbed, Window = "3ae79031-e1bc-11d0-8f78-00a0c9110057")]
    [Guid(PackageIds.PackageGuidString)]
    public sealed class SqlSourceControlPackage : AsyncPackage
    {
        protected override async Task InitializeAsync(CancellationToken cancellationToken, IProgress<ServiceProgressData> progress)
        {
            await JoinableTaskFactory.SwitchToMainThreadAsync(cancellationToken);
            var commands = await GetServiceAsync(typeof(IMenuCommandService)) as OleMenuCommandService;
            if (commands == null)
            {
                return;
            }

            var open = new MenuCommand(
                (sender, args) => { _ = JoinableTaskFactory.RunAsync(ShowWindowAsync); },
                new CommandID(PackageIds.CommandSet, PackageIds.OpenWindowCommand));
            commands.AddCommand(open);
        }

        private async Task ShowWindowAsync()
        {
            await JoinableTaskFactory.SwitchToMainThreadAsync(DisposalToken);
            var window = await ShowToolWindowAsync(typeof(SourceControlWindow), 0, true, DisposalToken);
            if (window == null)
            {
                throw new NotSupportedException("Cannot create the SQL Source Control window.");
            }
        }
    }
}
