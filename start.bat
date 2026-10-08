@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo creating venv with D:\Install\py310\python.exe
  "D:\Install\py310\python.exe" -m venv .venv
  if errorlevel 1 (
    echo venv creation failed. Check that D:\Install\py310\python.exe exists.
    pause
    exit /b 1
  )
)

if not exist ".venv\Lib\site-packages\PIL\__init__.py" (
  echo installing pillow
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)

if not exist "extension\icons\icon128.png" .venv\Scripts\python.exe server\make_icons.py

.venv\Scripts\python.exe server\server.py
echo.
echo server exited.
pause
