@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" tools\prepare_pr505_regional_data.py
) else (
  py -3.12 tools\prepare_pr505_regional_data.py
)
if errorlevel 1 (
  echo La preparation a echoue. Conservez le message ci-dessus.
  pause
  exit /b 1
)
echo Donnees regionales pretes. Lancez maintenant tester_france.bat.
pause
endlocal
