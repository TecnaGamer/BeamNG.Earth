@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0tester_plaques_internationales.bat" (
  echo Lanceur de plaques internationales absent. Utilisez la branche french-license-plates.
  pause
  exit /b 1
)
set "BNE_EU_ROAD_DETAILS=on"
echo Routes France, glissieres Europe et plaques internationales activees.
call "%~dp0tester_plaques_internationales.bat"
endlocal
