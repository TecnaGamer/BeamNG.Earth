@echo off
rem BeamNG.Earth 1.5.56 (KyzoFox build): starts the world server and the game. Keep this window open while you play.
cd /d "%~dp0"
rem a .venv made with a too-new Python (3.15+: no ready-made duckdb for it) is removed and made again
if exist ".venv\Scripts\python.exe" ".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info < (3, 15) else 1)" || (echo Removing the server setup made with a too-new Python... & rmdir /s /q .venv)
if exist ".venv\Scripts\python.exe" goto run
echo First start: installing the server packages (a few minutes)...
where py >nul 2>nul
if errorlevel 1 (python install.py & goto checked)
py -3.13 -c "pass" >nul 2>nul && (py -3.13 install.py & goto checked)
py -3.12 -c "pass" >nul 2>nul && (py -3.12 install.py & goto checked)
py -3.11 -c "pass" >nul 2>nul && (py -3.11 install.py & goto checked)
echo You need Python 3.13 (3.15 is too new for one of the packages): https://www.python.org/downloads/windows/ (pick a 3.13.x "Windows installer (64-bit)")
pause & exit /b 1
:checked
if not exist ".venv\Scripts\python.exe" (echo The install did not finish - send the text above with your bug report. & pause & exit /b 1)
:run
".venv\Scripts\python.exe" -c "import numpy, PIL, duckdb, fsspec, certifi" 2>nul
if errorlevel 1 (echo Installing the server packages into .venv ... & ".venv\Scripts\python.exe" -m pip install -r server\requirements.txt)
".venv\Scripts\python.exe" -c "import numpy, PIL, duckdb" 2>nul
if errorlevel 1 (echo The packages did not install - send the text above with your bug report. & pause & exit /b 1)
".venv\Scripts\python.exe" launcher.py %*
echo.
echo Stopped.
pause
