@echo off
rem Daedalus setup: checks / installs Python 3.11, the packages, shortcuts, and the Icarus agent (Ollama + model).
rem   install.bat                 everything (asks before each download)
rem   install.bat -NoAgent        skip Icarus           install.bat -AgentOnly   only Icarus
rem   install.bat -CheckOnly      report, change nothing    install.bat -Yes      no questions
rem If this file was downloaded on its own (no install.ps1 beside it), it fetches Daedalus from GitHub into
rem %USERPROFILE%\Daedalus first and installs from there. No labels / goto: they misbehave when the file is saved
rem with Unix line endings (e.g. GitHub's "Download raw file").
setlocal
cd /d "%~dp0"
set "HERE=%~dp0"
set "DEST=%USERPROFILE%\Daedalus"
set "NEED=1"
if exist "%HERE%install.ps1" if exist "%HERE%daedalus\__init__.py" set "NEED="
if exist "%DEST%\install.ps1" if exist "%DEST%\daedalus\__init__.py" if defined NEED (set "NEED=" & set "HERE=%DEST%\")
if defined NEED (
  echo install.bat is on its own here - downloading Daedalus from GitHub into %DEST% ...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $zip=Join-Path $env:TEMP 'daedalus-main.zip'; $tmp=Join-Path $env:TEMP 'daedalus-main-x'; Invoke-WebRequest 'https://github.com/ayenn1214/daedalus/archive/refs/heads/main.zip' -OutFile $zip -UseBasicParsing; if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }; Expand-Archive $zip $tmp -Force; $src=(Get-ChildItem $tmp -Directory | Select-Object -First 1).FullName; New-Item -ItemType Directory -Force $env:DEST | Out-Null; Copy-Item (Join-Path $src '*') $env:DEST -Recurse -Force; Remove-Item $zip, $tmp -Recurse -Force"
  if errorlevel 1 (
    echo.
    echo Download failed. Get the full zip instead: github.com/ayenn1214/daedalus - Code - Download ZIP,
    echo right-click it - Extract All, then run install.bat inside the extracted folder.
    pause
    exit /b 1
  )
  set "HERE=%DEST%\"
)
cd /d "%HERE%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%install.ps1" %*
set RC=%errorlevel%
echo.
pause
exit /b %RC%
