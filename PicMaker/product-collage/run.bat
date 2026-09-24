@echo off
setlocal
cd /d "%~dp0"

set "PY_CMD="
where py >nul 2>nul
if not errorlevel 1 (
    set "PY_CMD=py"
    goto :python_found
)
where python >nul 2>nul
if not errorlevel 1 (
    set "PY_CMD=python"
    goto :python_found
)

echo.
echo [ERROR] Python was not found.
echo Install Python 3.11 or newer from:
echo https://www.python.org/downloads/
echo Enable "Add python.exe to PATH" during setup.
echo.
pause
exit /b 1

:python_found
echo.
echo [1/2] Installing required packages...
%PY_CMD% -m pip install --upgrade pip
if errorlevel 1 (
    echo [ERROR] pip upgrade failed.
    pause
    exit /b 1
)

%PY_CMD% -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Package installation failed.
    pause
    exit /b 1
)

echo.
echo [2/2] Starting application...
%PY_CMD% app.py

if errorlevel 1 (
    echo.
    echo [ERROR] Application exited with an error.
    pause
)

endlocal
