@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title ServiceCreator - build

set "ROOT=%~dp0"
set "PYTHON="
set "SC_BUILD_NO_PAUSE="
if /i "%~1"=="--no-pause" set "SC_BUILD_NO_PAUSE=1"

if exist "%ROOT%.venv\Scripts\python.exe" (
    set "PYTHON=%ROOT%.venv\Scripts\python.exe"
    goto :python_ready
)

for %%P in (
    "%ProgramFiles%\Python311\python.exe"
    "%LocalAppData%\Programs\Python\Python311\python.exe"
) do (
    if not defined PYTHON if exist "%%~P" set "PYTHON=%%~P"
)

if not defined PYTHON (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        if not defined PYTHON (
            "%%~P" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)" >nul 2>&1
            if not errorlevel 1 set "PYTHON=%%~P"
        )
    )
)

if not defined PYTHON (
    echo ERROR: Python 3.11 was not found.
    echo Install Python 3.11 x64 and run this script again.
    if not defined SC_BUILD_NO_PAUSE pause
    exit /b 1
)

echo [1/6] Creating virtual environment .venv ...
"%PYTHON%" -m venv "%ROOT%.venv"
if errorlevel 1 goto :failed
set "PYTHON=%ROOT%.venv\Scripts\python.exe"

:python_ready
echo [1/6] Python: %PYTHON%
"%PYTHON%" -c "import sys; print(sys.version)"
if errorlevel 1 goto :failed

echo [2/6] Installing build and runtime dependencies ...
"%PYTHON%" -m pip install --disable-pip-version-check --upgrade pip
if errorlevel 1 goto :failed
"%PYTHON%" -m pip install --disable-pip-version-check -r "%ROOT%requirements.txt"
if errorlevel 1 goto :failed

echo [3/6] Generating application icon ...
"%PYTHON%" "%ROOT%tools\make_icon.py"
if errorlevel 1 goto :failed
if not exist "%ROOT%assets\app.ico" (
    echo ERROR: assets\app.ico was not generated.
    goto :failed
)

echo [4/6] Checking the source tree ...
"%PYTHON%" -m compileall -q "%ROOT%main.py" "%ROOT%service_creator"
if errorlevel 1 goto :failed
"%PYTHON%" "%ROOT%main.py" --selftest --report "%TEMP%\ServiceCreator_build_selftest.txt"
if errorlevel 1 (
    echo ERROR: Source self-test failed. See "%TEMP%\ServiceCreator_build_selftest.txt".
    goto :failed
)

echo [5/6] Cleaning old build output ...
if exist "%ROOT%build" rmdir /s /q "%ROOT%build"
if exist "%ROOT%dist" rmdir /s /q "%ROOT%dist"
if exist "%ROOT%dist" (
    echo ERROR: Cannot remove dist. Close running copies of ServiceCreator.exe.
    goto :failed
)

echo [6/6] Building the single-file executable ...
"%PYTHON%" -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --windowed ^
    --uac-admin ^
    --name ServiceCreator ^
    --icon "%ROOT%assets\app.ico" ^
    --version-file "%ROOT%tools\version_info.txt" ^
    --distpath "%ROOT%dist" ^
    --workpath "%ROOT%build\work" ^
    --specpath "%ROOT%build" ^
    --hidden-import servicemanager ^
    --hidden-import win32service ^
    --hidden-import win32serviceutil ^
    --hidden-import win32event ^
    --hidden-import win32api ^
    --hidden-import win32con ^
    --hidden-import win32security ^
    --hidden-import win32timezone ^
    --hidden-import win32process ^
    --hidden-import pywintypes ^
    --hidden-import pythoncom ^
    --hidden-import pystray._win32 ^
    --exclude-module pystray._appindicator ^
    --exclude-module pystray._darwin ^
    --exclude-module pystray._gtk ^
    --exclude-module pystray._xorg ^
    "%ROOT%main.py"
if errorlevel 1 goto :failed

if not exist "%ROOT%dist\ServiceCreator.exe" (
    echo ERROR: dist\ServiceCreator.exe was not created.
    goto :failed
)

echo.
echo BUILD OK
for %%F in ("%ROOT%dist\ServiceCreator.exe") do echo   %%~fF  (%%~zF bytes)
echo.
echo The delivery folder contains only the client executable:
dir /b "%ROOT%dist"
echo.
echo Run dist\ServiceCreator.exe as administrator to manage Windows services.
if not defined SC_BUILD_NO_PAUSE pause
exit /b 0

:failed
echo.
echo BUILD FAILED
if not defined SC_BUILD_NO_PAUSE pause
exit /b 1
