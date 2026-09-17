@echo off
rem Launch Mistery without a console window hanging around.
cd /d "%~dp0"
start "" pythonw.exe main.py
