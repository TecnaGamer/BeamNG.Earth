@echo off
rem Builds BeamNG.Earth-<VERSION>.exe from this folder with the projects own build tool (tools\build_app.py).
rem Needs the .venv the Play files made (Python 3.13). The .exe lands in the "dist" folder.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (echo Run "Play BALANCED.bat" once first - it sets up Python for this folder. & pause & exit /b 1)
echo Installing PyInstaller (the tool that packs everything into one .exe)...
".venv\Scripts\python.exe" -m pip install --upgrade pyinstaller
if errorlevel 1 (echo PyInstaller did not install - send the text above with your bug report. & pause & exit /b 1)
echo Building the .exe (a few minutes)...
".venv\Scripts\python.exe" tools\build_app.py
if errorlevel 1 (echo The build failed - send the text above with your bug report. & pause & exit /b 1)
echo.
echo Done: your .exe is in the dist folder.
start "" dist
pause
