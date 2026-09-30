@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo L'application n'est pas encore installee.
    echo Lancez d'abord INSTALLER.bat.
    pause
    exit /b 1
)

rem Dossier clone avec git : mise a jour automatique avant le demarrage.
rem Sans git (copie issue d'un zip), cette partie est ignoree : utilisez METTRE_A_JOUR.bat.
if exist ".git" (
    where git >nul 2>nul
    if not errorlevel 1 (
        echo Recuperation de la derniere version depuis GitHub...
        git pull --ff-only
        if errorlevel 1 (
            echo Mise a jour du code impossible ^(modifications locales, reseau...^). Demarrage avec la version actuelle.
        ) else (
            echo Verification des composants Python...
            ".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check --upgrade -r requirements.txt -c constraints.txt
            if errorlevel 1 echo Les composants Python n'ont pas pu etre verifies. Demarrage quand meme.
        )
    )
)

".venv\Scripts\python.exe" run.py
if errorlevel 1 (
    echo.
    echo L'application n'a pas pu demarrer.
    pause
    exit /b 1
)
