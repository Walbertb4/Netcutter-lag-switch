@echo off
REM Run this on Windows, inside the project folder, by double-clicking
REM or from cmd. Python 3.9+ must be installed.

pip install -r requirements.txt

python -m PyInstaller --onefile --noconsole --uac-admin --name NetCutter main.py

echo.
echo Done! Executable: dist\NetCutter.exe
pause
