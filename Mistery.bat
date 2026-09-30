@echo off
rem Launch Mistery without a console window hanging around.
cd /d "%~dp0"
rem Python 3.12 when the py launcher has it with PySide6: a newer Python
rem installed later becomes plain "pythonw", often without PySide6, and
rem Mistery would not start at all. Otherwise whatever pythonw is.
py -3.12 -c "import PySide6" >nul 2>&1 && (start "" pyw -3.12 main.py & exit /b)
start "" pythonw.exe main.py
