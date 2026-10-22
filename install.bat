@echo off
rem Daedalus setup: checks / installs Python 3.11, the packages, shortcuts, and the Icarus agent (Ollama + model).
rem   install.bat                 everything (asks before each download)
rem   install.bat -NoAgent        skip Icarus           install.bat -AgentOnly   only Icarus
rem   install.bat -CheckOnly      report, change nothing    install.bat -Yes      no questions
cd /d %~dp0
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set RC=%errorlevel%
echo.
pause
exit /b %RC%
