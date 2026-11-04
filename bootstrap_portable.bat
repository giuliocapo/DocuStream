@echo off
setlocal EnableDelayedExpansion
REM =====================================================================
REM  DocuStream - bootstrap_portable.bat
REM  Zero-admin portable installer for Windows.
REM
REM  What this script does:
REM   1. Checks for a system Python 3.11 (uses it if found).
REM   2. Otherwise downloads the official Python 3.11 embeddable package
REM      into the local runtime\ folder (no installer, no admin rights,
REM      no registry writes).
REM   3. Enables pip inside the embedded runtime (python311._pth fix +
REM      get-pip.py).
REM   4. Installs DocuStream's dependencies INTO the local runtime only.
REM
REM  Nothing is written outside this folder. Uninstall = delete folder.
REM =====================================================================

set "ROOT=%~dp0"
set "RUNTIME=%ROOT%runtime"
set "PY_VERSION=3.11.9"
set "PY_ZIP=python-%PY_VERSION%-embed-amd64.zip"
set "PY_URL=https://www.python.org/ftp/python/%PY_VERSION%/%PY_ZIP%"
set "GETPIP_URL=https://bootstrap.pypa.io/get-pip.py"

echo.
echo ======================================================
echo   DocuStream Portable Bootstrap (zero-admin)
echo ======================================================
echo.

REM ---------------------------------------------------------------------
REM 0. Already bootstrapped? Then just verify dependencies.
REM ---------------------------------------------------------------------
if exist "%RUNTIME%\python.exe" (
    echo [*] Local embedded runtime found at %RUNTIME%
    set "PYBIN=%RUNTIME%\python.exe"
    goto :install_deps
)

REM ---------------------------------------------------------------------
REM 1. Try system Python 3.11 first (regular pip install --user style is
REM    deliberately avoided to keep the footprint inside this folder).
REM ---------------------------------------------------------------------
if exist "C:\Python311\python.exe" (
    echo [*] Found system Python 3.11 at C:\Python311
    set "PYBIN=C:\Python311\python.exe"
    goto :install_deps
)

echo [*] No usable Python 3.11 found. Downloading embedded runtime...
if not exist "%RUNTIME%" mkdir "%RUNTIME%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; " ^
    "Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%RUNTIME%\%PY_ZIP%'" || goto :download_failed

echo [*] Extracting embedded runtime...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "Expand-Archive -Force -Path '%RUNTIME%\%PY_ZIP%' -DestinationPath '%RUNTIME%'" || goto :download_failed
del "%RUNTIME%\%PY_ZIP%" >nul 2>&1

set "PYBIN=%RUNTIME%\python.exe"

REM ---------------------------------------------------------------------
REM 2. Enable pip in the embedded runtime.
REM    The ._pth file blocks site-packages and the project folder by
REM    default; rewrite it so that pip, site-packages and ".." (the folder
REM    containing app.py) are all importable.
REM ---------------------------------------------------------------------
for %%F in ("%RUNTIME%\python*._pth") do (
    echo [*] Unblocking %%~nxF for pip + site-packages + project folder
    (
        echo python311.zip
        echo .
        echo Lib\site-packages
        echo ..
        echo.
        echo import site
    ) > "%%F"
)

REM ---------------------------------------------------------------------
REM 3. The embeddable package ships WITHOUT Tkinter, which customtkinter
REM    needs. Download the official standalone CPython 3.11 build
REM    (python-build-standalone) and graft its Tcl/Tk files into the
REM    runtime. Everything still lives inside this folder - zero admin.
REM ---------------------------------------------------------------------
if not exist "%RUNTIME%\_tkinter.pyd" (
    echo [*] Embedded runtime lacks Tkinter - fetching Tcl/Tk payload...
    set "TK_URL=https://github.com/astral-sh/python-build-standalone/releases/download/20241016/cpython-3.11.10+20241016-x86_64-pc-windows-msvc-shared-install_only.tar.gz"
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; " ^
        "Invoke-WebRequest -Uri '!TK_URL!' -OutFile '%RUNTIME%\tkpayload.tar.gz'" || goto :download_failed
    tar -xzf "%RUNTIME%\tkpayload.tar.gz" -C "%RUNTIME%" || goto :download_failed
    xcopy /E /I /Y "%RUNTIME%\python\tcl" "%RUNTIME%\tcl" >nul
    xcopy /E /I /Y "%RUNTIME%\python\Lib\tkinter" "%RUNTIME%\tkinter" >nul
    copy /Y "%RUNTIME%\python\DLLs\_tkinter.pyd" "%RUNTIME%\_tkinter.pyd" >nul
    copy /Y "%RUNTIME%\python\DLLs\tcl86t.dll" "%RUNTIME%\tcl86t.dll" >nul
    copy /Y "%RUNTIME%\python\DLLs\tk86t.dll" "%RUNTIME%\tk86t.dll" >nul
    rmdir /S /Q "%RUNTIME%\python"
    del "%RUNTIME%\tkpayload.tar.gz" >nul 2>&1
    echo [*] Tcl/Tk grafted into the embedded runtime.
)

echo [*] Bootstrapping pip into the embedded runtime...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; " ^
    "Invoke-WebRequest -Uri '%GETPIP_URL%' -OutFile '%RUNTIME%\get-pip.py'" || goto :download_failed
"%PYBIN%" "%RUNTIME%\get-pip.py" --no-warn-script-location || goto :pip_failed
del "%RUNTIME%\get-pip.py" >nul 2>&1

:install_deps
echo.
echo [*] Installing DocuStream dependencies (this can take a few minutes)...
"%PYBIN%" -m pip install --no-warn-script-location --upgrade pip || goto :pip_failed
"%PYBIN%" -m pip install --no-warn-script-location ^
    customtkinter pyaudiowpatch numpy faster-whisper requests || goto :pip_failed

echo.
echo ======================================================
echo   Bootstrap complete!
echo   Double-click run.bat to launch DocuStream.
echo ======================================================
echo.
pause
exit /b 0

:download_failed
echo.
echo [ERROR] Download or extraction failed. Check your internet connection
echo         (or a corporate proxy) and re-run this script.
pause
exit /b 1

:pip_failed
echo.
echo [ERROR] Dependency installation failed. Re-run this script; if it
echo         persists, install the packages manually:
echo         %PYBIN% -m pip install customtkinter pyaudiowpatch numpy faster-whisper requests
pause
exit /b 1
