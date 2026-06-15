$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RunSilent = Join-Path $ScriptDir "run-silent.bat"
$StartupDir = [Environment]::GetFolderPath("Startup")
$StartupScript = Join-Path $StartupDir "cx2cc-startup.vbs"

if (-not (Test-Path $RunSilent)) {
    throw "Missing run-silent.bat: $RunSilent"
}

$escaped = $RunSilent.Replace('"', '""')
@"
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """$escaped""", 0, False
"@ | Set-Content -Path $StartupScript -Encoding ASCII

Write-Host "Installed cx2cc startup script: $StartupScript"
