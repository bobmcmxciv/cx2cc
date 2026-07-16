param(
    [string]$Version = "dev"
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent (Split-Path -Parent $ScriptDir)
$Venv = Join-Path $Root ".venv"
$Python = Join-Path $Venv "Scripts\python.exe"
$ReleaseName = "cx2cc-$Version-windows-x64"
$ReleaseDir = Join-Path $Root "dist\$ReleaseName"
$ZipPath = Join-Path $Root "dist\$ReleaseName.zip"
$HashPath = "$ZipPath.sha256"
$SmokeRoot = Join-Path $Root "dist\smoke-$ReleaseName"

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
& $Python -m pip install -r requirements.txt pyinstaller==6.16.0
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

if (Test-Path $SmokeRoot) { Remove-Item $SmokeRoot -Recurse -Force }
New-Item -ItemType Directory -Force -Path $SmokeRoot | Out-Null
Expand-Archive -Path $ZipPath -DestinationPath $SmokeRoot -Force

$SmokeAppDir = Join-Path $SmokeRoot $ReleaseName
$SmokeBinary = Join-Path $SmokeAppDir "cx2cc.exe"
$ExpectedFiles = @(
    ".env.example",
    "LICENSE",
    "README.md",
    "README.zh-CN.md",
    "cx2cc.exe",
    "run.bat",
    "scripts/windows/install-startup.ps1",
    "scripts/windows/run-silent.bat",
    "scripts/windows/start-cx2cc.ps1",
    "scripts/windows/uninstall-startup.ps1",
    "start-cx2cc.bat"
)
$ExpectedFiles = $ExpectedFiles | Sort-Object
$ActualFiles = Get-ChildItem $SmokeAppDir -File -Recurse | ForEach-Object {
    $_.FullName.Substring($SmokeAppDir.Length + 1).Replace("\", "/")
} | Sort-Object
if (Compare-Object $ExpectedFiles $ActualFiles) {
    throw "Package contents do not match the release whitelist"
}
if (-not (Test-Path $SmokeBinary)) {
    throw "Missing executable at $SmokeBinary"
}

$Process = $null
try {
    $env:CX2CC_PORT = "18901"
    $env:CX2CC_HOST = "127.0.0.1"
    Remove-Item Env:CX2CC_UPSTREAM_BASE_URL -ErrorAction SilentlyContinue
    Remove-Item Env:CX2CC_UPSTREAM_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:CX2CC_UPSTREAM_API_KEYS -ErrorAction SilentlyContinue

    $Process = Start-Process -FilePath $SmokeBinary -ArgumentList "serve" -WorkingDirectory $SmokeAppDir -PassThru -WindowStyle Hidden
    $Healthy = $false
    for ($i = 0; $i -lt 60; $i++) {
        if ($Process.HasExited) {
            throw "Smoke test process exited early with code $($Process.ExitCode)"
        }

        try {
            $Response = Invoke-WebRequest -Uri "http://127.0.0.1:18901/health" -UseBasicParsing -TimeoutSec 2
            $Body = $Response.Content | ConvertFrom-Json
            $HasUpstreamField = $Body.PSObject.Properties.Name -contains "upstream"
            if ($Response.StatusCode -eq 200 -and $Body.status -eq "ok" -and $Body.upstream_configured -eq $false -and -not $HasUpstreamField) {
                $Healthy = $true
                break
            }
            throw "Unexpected health response: $($Response.Content)"
        } catch {
            if ($i -eq 59) {
                throw
            }
            Start-Sleep -Seconds 1
        }
    }

    if (-not $Healthy) {
        throw "Timed out waiting for healthy cx2cc"
    }
} finally {
    if ($null -ne $Process -and -not $Process.HasExited) {
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
        $Process.WaitForExit()
    }
}

$hash = Get-FileHash $ZipPath -Algorithm SHA256
"$($hash.Hash)  $(Split-Path -Leaf $ZipPath)" | Set-Content -Path $HashPath -Encoding ASCII

Write-Host "Built $ZipPath"
Write-Host "SHA256 $($hash.Hash)"
