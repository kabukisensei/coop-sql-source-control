<#
.SYNOPSIS
  Installs the Coop SQL Source Control add-in into SSMS 21 or 22.

.DESCRIPTION
  SSMS 21 and 22 register with the Visual Studio installer as their own
  product (Microsoft.VisualStudio.Product.Ssms) and ship VSIXInstaller.exe,
  so the add-in installs for the current user the way a Visual Studio
  extension does: no admin rights and nothing written under Program Files.
  Run it with SSMS closed; SSMS loads the add-in the next time it starts.

.EXAMPLE
  .\install-ssms.ps1 -Vsix .\CoopSqlSourceControl.vsix
  .\install-ssms.ps1 -Vsix .\CoopSqlSourceControl.vsix -Ssms "C:\Program Files\Microsoft SQL Server Management Studio 21\Release\Common7\IDE\SSMS.exe"
  .\install-ssms.ps1 -Uninstall
#>
param(
    [string]$Vsix,
    [string]$Ssms,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$extensionId = 'CoopSqlSourceControl.772e6ac3-d552-4127-ba5f-3c25fd661535'

$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (-not (Test-Path $vswhere)) {
    throw "Could not find $vswhere, which SSMS 21 and 22 install. Is SSMS 21 or 22 installed?"
}
$instances = & $vswhere -all -prerelease -products Microsoft.VisualStudio.Product.Ssms -format json | ConvertFrom-Json
if ($Ssms) {
    $root = (Resolve-Path $Ssms).Path
    $instances = @($instances | Where-Object { $root.StartsWith($_.installationPath, [StringComparison]::OrdinalIgnoreCase) })
}
$instance = $instances | Sort-Object { [version]$_.installationVersion } -Descending | Select-Object -First 1
if (-not $instance) {
    throw 'Could not find SSMS 21 or 22. Pass -Ssms with the path to SSMS.exe.'
}

if (Get-Process -Name Ssms -ErrorAction SilentlyContinue) {
    throw 'Close SSMS first.'
}

$installer = Join-Path $instance.installationPath 'Common7\IDE\VSIXInstaller.exe'
$log = "coop-sqlsc-vsix-$(Get-Date -Format yyyyMMddHHmmss).log"
if ($Uninstall) {
    $arguments = @('/quiet', "/instanceIds:$($instance.instanceId)", "/logFile:$log", "/uninstall:$extensionId")
} else {
    if (-not $Vsix -or -not (Test-Path $Vsix)) {
        throw 'Pass -Vsix with the CoopSqlSourceControl.vsix built by CI (the CoopSqlSourceControl-vsix artifact).'
    }
    $arguments = @('/quiet', "/instanceIds:$($instance.instanceId)", "/logFile:$log", "`"$((Resolve-Path $Vsix).Path)`"")
}

$run = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru
if ($run.ExitCode -ne 0) {
    throw "VSIXInstaller exited with $($run.ExitCode); see $(Join-Path $env:TEMP $log)."
}

$verb = if ($Uninstall) { 'Removed the add-in from' } else { 'Installed the add-in into' }
Write-Host "$verb $($instance.displayName) $($instance.installationVersion) for $env:USERNAME."
if (-not $Uninstall) {
    Write-Host 'Start SSMS and open Tools > SQL Source Control.'
}
