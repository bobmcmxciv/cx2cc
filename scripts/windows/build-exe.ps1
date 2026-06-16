$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent (Split-Path -Parent $ScriptDir)
$Venv = Join-Path $Root ".venv"
$Python = Join-Path $Venv "Scripts\python.exe"
$ReleaseName = "cx2cc-windows-x64"
$ReleaseDir = Join-Path $Root "dist\$ReleaseName"
$ZipPath = Join-Path $Root "dist\$ReleaseName.zip"
$HashPath = "$ZipPath.sha256"

Set-Location $Root

if (-not (Test-Path $Python)) {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        & $py.Source -3 -m venv $Venv
    } else {
        python -m venv $Venv
    }
}

& $Python -m pip install --upgrade pip
& $Python -m pip install -r requirements.txt pyinstaller
& $Python -m pytest
& $Python -m compileall server.py translator.py start-cx2cc.py

& $Python -m PyInstaller --clean --onefile --name cx2cc --windowed start-cx2cc.py

if (Test-Path $ReleaseDir) { Remove-Item $ReleaseDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $ReleaseDir "scripts\windows") | Out-Null

Copy-Item (Join-Path $Root "dist\cx2cc.exe") $ReleaseDir
Copy-Item (Join-Path $Root ".env.example") $ReleaseDir
Copy-Item (Join-Path $Root "README.md") $ReleaseDir
Copy-Item (Join-Path $Root "README.zh-CN.md") $ReleaseDir
Copy-Item (Join-Path $Root "LICENSE") $ReleaseDir
Copy-Item (Join-Path $Root "run.bat") $ReleaseDir
Copy-Item (Join-Path $Root "start-cx2cc.bat") $ReleaseDir
Copy-Item (Join-Path $Root "scripts\windows\start-cx2cc.ps1") (Join-Path $ReleaseDir "scripts\windows")
Copy-Item (Join-Path $Root "scripts\windows\run-silent.bat") (Join-Path $ReleaseDir "scripts\windows")
Copy-Item (Join-Path $Root "scripts\windows\install-startup.ps1") (Join-Path $ReleaseDir "scripts\windows")
Copy-Item (Join-Path $Root "scripts\windows\uninstall-startup.ps1") (Join-Path $ReleaseDir "scripts\windows")

if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
Compress-Archive -Path $ReleaseDir -DestinationPath $ZipPath -Force

$hash = Get-FileHash $ZipPath -Algorithm SHA256
"$($hash.Hash)  $(Split-Path -Leaf $ZipPath)" | Set-Content -Path $HashPath -Encoding ASCII

Write-Host "Built $ZipPath"
Write-Host "SHA256 $($hash.Hash)"
