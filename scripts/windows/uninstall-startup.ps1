$ErrorActionPreference = "Stop"

$StartupDir = [Environment]::GetFolderPath("Startup")
$StartupScript = Join-Path $StartupDir "cx2cc-startup.vbs"

if (Test-Path $StartupScript) {
    Remove-Item $StartupScript -Force
    Write-Host "Removed cx2cc startup script: $StartupScript"
} else {
    Write-Host "cx2cc startup script is not installed."
}
