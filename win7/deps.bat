@echo off
rem Usage: win7\deps.bat "C:\path\to\rustc.exe"   (shows which DLL is missing and which file asks for it)
cd /d "%~dp0\.."
set "PYTHONNOUSERSITE=1"
"%~dp0..\python\python.exe" "win7\deps.py" %*
echo.
pause
