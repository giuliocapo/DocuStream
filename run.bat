@echo off
REM =====================================================================
REM  DocuStream - run.bat
REM  One-click silent launcher. Prefers the local embedded runtime,
REM  falls back to any system Python on PATH. No administrator rights.
REM =====================================================================
setlocal
set "ROOT=%~dp0"

REM Prefer the portable embedded runtime created by bootstrap_portable.bat
if exist "%ROOT%runtime\pythonw.exe" (
    start "" "%ROOT%runtime\pythonw.exe" "%ROOT%app.py"
    exit /b 0
)

REM Fall back to system Python (pythonw = no console window)
where pythonw >nul 2>&1
if %ERRORLEVEL%==0 (
    start "" pythonw "%ROOT%app.py"
    exit /b 0
)
where python >nul 2>&1
if %ERRORLEVEL%==0 (
    start "" python "%ROOT%app.py"
    exit /b 0
)

echo [ERROR] No Python runtime found.
echo Run bootstrap_portable.bat first to create the portable runtime.
pause
exit /b 1
