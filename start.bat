@echo off
if exist ".venv\Scripts\python.exe" (
    .venv\Scripts\python.exe cli.py
) else if exist "..\src\.venv\Scripts\python.exe" (
    ..\src\.venv\Scripts\python.exe cli.py
) else (
    python cli.py
)
pause
