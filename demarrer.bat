@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo L'application n'est pas encore installee.
    echo Lancez d'abord INSTALLER.bat.
    pause
    exit /b 1
)

rem Dossier clone avec git : mise a jour automatique avant le demarrage, avec retour a la version
rem precedente si la nouvelle ne s'installe pas ou ne demarre pas (voir demarrer_maj.ps1).
rem Sans git (copie issue d'un zip), cette partie est ignoree : utilisez METTRE_A_JOUR.bat.
if exist ".git" (
    where git >nul 2>nul
    if not errorlevel 1 (
        powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0demarrer_maj.ps1"
    )
)

".venv\Scripts\python.exe" run.py
if errorlevel 1 (
    echo.
    echo L'application n'a pas pu demarrer.
    pause
    exit /b 1
)
