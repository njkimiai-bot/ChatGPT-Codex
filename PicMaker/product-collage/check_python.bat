@echo off
setlocal
cd /d "%~dp0"

echo === Python check ===
where py
where python
echo.
py --version 2>nul
python --version 2>nul
echo.

echo === Current files ===
dir /b
echo.
pause
endlocal
