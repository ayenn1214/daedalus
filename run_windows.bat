@echo off
rem Daedalus launcher: needs Python 3.11 (the sketch solver ships Windows builds for 3.10/3.11 only).
cd /d %~dp0
where py >nul 2>nul || goto nopy
py -3.11 -c "" >nul 2>nul || goto nopy
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -c "import sys; sys.exit(sys.version_info[:2] != (3, 11))" || rmdir /s /q .venv
)
if not exist .venv (
  echo First run: setting up the Python 3.11 environment, this takes a few minutes...
  py -3.11 -m venv .venv || goto fail
)
call .venv\Scripts\activate
python -m pip install -q --upgrade pip
python -m pip install -q --only-binary=:all: -r requirements.txt || goto fail
python -m pip install -q --only-binary=:all: -r requirements-optional.txt 2>nul
python -m daedalus %*
if errorlevel 1 pause
exit /b

:nopy
echo Python 3.11 not found.
echo Run install.bat - it installs Python 3.11 (alongside any other Python), the packages and Icarus.
choice /m "Run install.bat now"
if errorlevel 2 exit /b 1
call "%~dp0install.bat"
exit /b
pause
exit /b 1

:fail
echo Setup failed - see the messages above.
pause
exit /b 1
