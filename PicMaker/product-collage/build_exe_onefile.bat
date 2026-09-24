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

echo [ERROR] Python was not found.
pause
exit /b 1

:python_found
%PY_CMD% -m pip install --upgrade pip
if errorlevel 1 goto :fail

%PY_CMD% -m pip install -r requirements.txt
if errorlevel 1 goto :fail

%PY_CMD% -m pip install pyinstaller
if errorlevel 1 goto :fail

rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul

%PY_CMD% -m PyInstaller --noconfirm --clean --windowed --onefile --name ProductCollage_OneFile --collect-all tkinterdnd2 --hidden-import=onnxruntime --hidden-import=rembg app.py
if errorlevel 1 goto :fail

echo.
echo Build completed:
echo %CD%\dist\ProductCollage_OneFile.exe
echo.
echo Note: onefile is easier to distribute but may start slower.
pause
exit /b 0

:fail
echo.
echo [ERROR] Build failed.
pause
exit /b 1
