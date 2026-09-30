@echo off
cd /d "%~dp0"
rem Tout sur une seule ligne : cmd relit un .bat au fil de l'eau, donc rien ne doit suivre l'appel.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0mise_a_jour.ps1" %* & echo. & pause & exit
