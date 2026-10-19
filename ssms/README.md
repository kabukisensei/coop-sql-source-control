# SSMS add-in

A window in SQL Server Management Studio over the same `sqlsc` command line:
**Tools > SQL Source Control**. It does nothing the CLI cannot; every button
runs one `sqlsc --json` command and shows the answer, so the add-in, the CLI,
coop-agent and CI always agree.

- **Repo / Database**: open a repo's `sqlsc.yml` and pick one of its targets.
  The add-in remembers recent repos and the target last used with each
  (`%APPDATA%\coop-sqlsc\ssms.json`).
- **Changes**: what differs between the database and the repo, with who has
  each object checked out. Select rows, then **Commit** (with the message
  below), **Get latest…** (shows the script, then asks before running it),
  **Diff** (or double-click a row), **Check out** or **Check in**. With nothing
  selected, Commit and Get latest act on everything, after asking.
- **Checkouts**: everything checked out in this repo. **Check in** commits and
  releases, **Undo checkout** releases without committing, and **Release…**
  takes someone else's checkout away (`sqlsc unlock --force`).
- **Output**: diffs, the get-latest script, and connection errors.

## Install

1. Install the CLI: `pipx install git+https://github.com/kabukisensei/coop-sql-source-control`.
   The add-in finds `sqlsc` on PATH or in `%USERPROFILE%\.local\bin`; otherwise
   use **Find sqlsc…** in the window.
2. Download `CoopSqlSourceControl.vsix` from the `CoopSqlSourceControl-vsix`
   artifact of the latest CI run on `main`.
3. Close SSMS, then from an elevated PowerShell:
   `.\ssms\install-ssms.ps1 -Vsix .\CoopSqlSourceControl.vsix`
   (`-Uninstall` removes it again).

The add-in targets SSMS 21, which runs on the 64-bit Visual Studio 2022 (17.x)
shell, and SSMS 22, which runs on the Visual Studio 2026 (18.x) shell; the
manifest accepts both. Microsoft does not support third-party SSMS extensions officially, so
the install copies the files into SSMS's `Extensions` folder the way SSMS
add-ins traditionally are installed. The same `.vsix` also installs into Visual
Studio 2022, which is the easiest place to debug it (F5 opens the experimental
instance).

## Build

CI builds it on Windows (`ssms-vsix` job) and runs the core tests on Linux
(`ssms-core` job). Locally, on Windows with Visual Studio 2022 and the
"Visual Studio extension development" workload:

```powershell
msbuild ssms\Extension\CoopSqlSourceControl.Extension.csproj /restore /p:Configuration=Release
dotnet test ssms\Core.Tests
```

`ssms/Core` holds everything that does not need SSMS (running `sqlsc`, reading
its JSON, quoting arguments, settings) and is compiled into the extension, so
the add-in ships as one assembly.
