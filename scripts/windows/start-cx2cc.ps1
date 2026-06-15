$ErrorActionPreference = "Continue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent (Split-Path -Parent $ScriptDir)
$LogDir = Join-Path $Root "logs"
$OutLog = Join-Path $LogDir "cx2cc.out.log"
$ErrLog = Join-Path $LogDir "cx2cc.err.log"
$Exe = Join-Path $Root "cx2cc.exe"

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
Set-Location $Root

if (-not $env:CX2CC_HOST) { $env:CX2CC_HOST = "127.0.0.1" }
if (-not $env:CX2CC_PORT) { $env:CX2CC_PORT = "8901" }
$env:PYTHONUNBUFFERED = "1"

function Resolve-PythonCommand {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { return @($py.Source, "-3") }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { return @($python.Source) }

    throw "Python 3 was not found. Install Python 3 or add it to PATH."
}

if (Test-Path $Exe) {
    & $Exe serve 1>> $OutLog 2>> $ErrLog
    exit $LASTEXITCODE
}

$pythonCommand = Resolve-PythonCommand
$pythonExe = $pythonCommand[0]
$pythonArgs = @()
if ($pythonCommand.Count -gt 1) { $pythonArgs = $pythonCommand[1..($pythonCommand.Count - 1)] }
$pythonArgs += "start-cx2cc.py"
$pythonArgs += "serve"

& $pythonExe @pythonArgs 1>> $OutLog 2>> $ErrLog
exit $LASTEXITCODE
