@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0cx2cc.exe" (
    "%~dp0cx2cc.exe" start
) else (
    where py >nul 2>nul
    if %ERRORLEVEL%==0 (
        py -3 "%~dp0start-cx2cc.py" start
    ) else (
        python "%~dp0start-cx2cc.py" start
    )
)
