@echo off
rem Build the Windows host exe (BindDeckLite.exe). Run this on Windows.
rem Single packaging path: pyinstaller app.spec (windowed, icon, tray + dialog
rem hidden imports). CI (.github/workflows/build-host.yml) and release.yml
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
echo [build] double-click it: a native pywebview window opens (no console, no browser); \
      close it to exit, or minimize to the tray.
pause
exit /b 0

:fail
echo.
echo [build] FAILED - see the output above.
pause
exit /b 1
