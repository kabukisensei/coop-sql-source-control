# SSMS add-in

A window in SQL Server Management Studio over the same `sqlsc` command line:
**Tools > SQL Source Control**. It does nothing the CLI cannot; every button
runs one `sqlsc --json` command and shows the answer, so the add-in, the CLI,
coop-agent and CI always agree.

- **Repo / Database**: open a repo's `sqlsc.yml` and pick one of its targets.
  The add-in remembers recent repos and the target last used with each
  (`%APPDATA%\coop-sqlsc\ssms.json`); a remembered repo whose `sqlsc.yml` is
  gone leaves the list. The line under the status says which `sqlsc` the
  window runs, its version and where it came from (coop's install in
  `%USERPROFILE%\.local\bin` first, then `PATH`, then one chosen with **Find
  sqlsc…**, which is forgotten once its file is gone), and warns when the
  add-in and `sqlsc` are not the same `major.minor`.
- **Changes**: what differs between the database and the repo, which side
  changed (*Changed in*: Database, Repo or Both, see the main README) and who
  has each object checked out (*you* for your own). Select rows, then
  **Commit** (with the message below), **Get latest…** (shows the script, then
  asks before running it), **Diff** (or double-click a row), **Check out**
  (with an optional note for the team), **Check in**, or **Track rows…** to
  keep a selected lookup table's rows in the repo as static data. With nothing
  selected, Commit and Get latest act on everything, after asking. Commit's
  question counts only the rows sqlsc will commit and names the rest (checked
  out by someone else, or changed in the repo, which Get latest applies); when
  nothing in the list can be committed it says so instead of asking. Commit
  pulls the branch's upstream first and reports what it held back because the
  repo moved, and Get latest what it kept because the database did (see
  *Working as a team* in the main README for every rule). When the repo has migration scripts the
  database has not run, the status line says so and Get latest's question
  names them.
- **Checkouts**: everything checked out in this repo. **Check in** commits and
  releases, **Undo checkout** releases without committing, and **Release…**
  takes someone else's checkout away (`sqlsc unlock --force`).
- **Output**: diffs, the get-latest script, and connection errors.

## Install

1. Install the CLI. It comes with coop (`coop update` keeps it current in
   `%USERPROFILE%\.local\bin`, which the add-in uses first); without coop,
   `pipx install git+https://github.com/kabukisensei/coop-sql-source-control`
   puts it on PATH. Otherwise use **Find sqlsc…** in the window.
2. Download `CoopSqlSourceControl.vsix` and `install-ssms.ps1` from the
   [releases page](https://github.com/kabukisensei/coop-sql-source-control/releases)
   (or, between releases, the `CoopSqlSourceControl-vsix` artifact of the latest
   CI run on `main`).
3. Close SSMS, then from a normal (not elevated) PowerShell:
   `.\install-ssms.ps1 -Vsix .\CoopSqlSourceControl.vsix`
   (`-Uninstall` removes it again).

The add-in targets SSMS 21, which runs on the 64-bit Visual Studio 2022 (17.x)
shell, and SSMS 22, which runs on the Visual Studio 2026 (18.x) shell. Both
register with the Visual Studio installer as their own product
(`Microsoft.VisualStudio.Product.Ssms`) and ship `VSIXInstaller.exe`, so the
script installs the add-in for the current user through it, the way a Visual
Studio extension installs: no admin rights, nothing written under Program
Files. Microsoft does not support third-party SSMS extensions officially. The
same `.vsix` also installs into Visual Studio 2022, which is the easiest place
to debug it (F5 opens the experimental instance).

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
