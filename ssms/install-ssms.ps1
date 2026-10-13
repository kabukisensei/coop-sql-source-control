<#
.SYNOPSIS
  Installs the Coop SQL Source Control add-in into SSMS 21 or 22.

.DESCRIPTION
  SSMS has no extension manager, so the add-in is installed the way SSMS
  add-ins always have been: its files are unpacked into a folder under
  SSMS's Common7\IDE\Extensions, and SSMS picks it up the next time it
  starts. Run from an elevated PowerShell (Program Files needs it), with
  SSMS closed.

.EXAMPLE
  .\install-ssms.ps1 -Vsix .\CoopSqlSourceControl.vsix
  .\install-ssms.ps1 -Vsix .\CoopSqlSourceControl.vsix -Ssms "C:\Program Files\Microsoft SQL Server Management Studio 21\Release\Common7\IDE\Ssms.exe"
  .\install-ssms.ps1 -Uninstall
#>
param(
    [string]$Vsix,
    [string]$Ssms,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

if (-not $Ssms) {
    $found = Get-ChildItem -Path "$env:ProgramFiles\Microsoft SQL Server Management Studio *" -Filter Ssms.exe -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -like '*\Common7\IDE\Ssms.exe' } |
        Sort-Object FullName -Descending
    if (-not $found) {
        throw "Could not find SSMS 21 or 22 under $env:ProgramFiles. Pass -Ssms with the path to Ssms.exe."
    }
    $Ssms = $found[0].FullName
}

if (Get-Process -Name Ssms -ErrorAction SilentlyContinue) {
    throw 'Close SSMS first.'
}

$target = Join-Path (Split-Path $Ssms) 'Extensions\Cooptimize\CoopSqlSourceControl'
if (Test-Path $target) {
    Remove-Item -Recurse -Force $target
}

if ($Uninstall) {
    Write-Host "Removed the add-in from $target"
} else {
    if (-not $Vsix -or -not (Test-Path $Vsix)) {
        throw 'Pass -Vsix with the CoopSqlSourceControl.vsix built by CI (the CoopSqlSourceControl-vsix artifact).'
    }
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    $zip = Join-Path $env:TEMP 'CoopSqlSourceControl.zip'
    Copy-Item $Vsix $zip -Force
    Expand-Archive -Path $zip -DestinationPath $target -Force
    Remove-Item $zip
    Write-Host "Installed the add-in into $target"
}

# SSMS caches which extensions it has; clearing the cache makes it look again.
Get-ChildItem -Path "$env:LOCALAPPDATA\Microsoft\SSMS" -Directory -ErrorAction SilentlyContinue |
    ForEach-Object { Join-Path $_.FullName 'ComponentModelCache' } |
    Where-Object { Test-Path $_ } |
    ForEach-Object { Remove-Item -Recurse -Force $_ }

Write-Host 'Start SSMS and open Tools > SQL Source Control.'
