@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo L'application n'est pas encore installee.
    echo Lancez d'abord INSTALLER.bat.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" run.py
if errorlevel 1 (
    echo.
    echo L'application n'a pas pu demarrer.
    pause
    exit /b 1
)
