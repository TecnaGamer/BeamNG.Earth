@echo off
rem BeamNG.Earth: double-click to play. Installs the first time, then starts the world server and BeamNG.drive
rem (launcher.py, as the .exe does). Keep this window open while you play; closing the game stops the server.
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto run
echo First start: installing BeamNG.Earth (this takes a few minutes, once)...
where py >nul 2>nul
if errorlevel 1 goto plainpython
py -3 install.py
goto checkinstall
:plainpython
python install.py
:checkinstall
if exist ".venv\Scripts\python.exe" goto run
echo.
echo The installer did not finish. Install Python 3.10+ from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH"), then double-click this file again.
pause
exit /b 1
:run
".venv\Scripts\python.exe" launcher.py %*
echo.
echo BeamNG.Earth has stopped.
pause
