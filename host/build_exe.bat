@echo off
rem Build the Windows host exe (BindDeckLite.exe). Run this on Windows.
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [build] Python not found in PATH. Install Python 3.12 x64 first.
  pause
  exit /b 1
)

if not exist .venv (
  echo [build] creating .venv ...
  python -m venv .venv
)
call .venv\Scripts\activate.bat

python -m pip install --upgrade pip
python -m pip install -r requirements-build.txt
if errorlevel 1 goto :fail

pyinstaller --noconfirm --clean app.spec
if errorlevel 1 goto :fail

echo.
echo [build] done: %~dp0dist\BindDeckLite.exe
echo [build] double-click it: the console stays hidden, the tray icon is at the bottom right.
pause
exit /b 0

:fail
echo.
echo [build] FAILED - see the output above.
pause
exit /b 1
