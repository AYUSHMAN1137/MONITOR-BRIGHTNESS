@echo off
cd /d "%~dp0"
start "" pythonw app_gui.py
if errorlevel 1 (
    start "" python app_gui.py
)
exit
