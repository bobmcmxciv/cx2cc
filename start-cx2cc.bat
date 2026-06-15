@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py -3 "%~dp0start-cx2cc.py"
) else (
    python "%~dp0start-cx2cc.py"
)
