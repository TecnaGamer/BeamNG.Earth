@echo off
setlocal
cd /d "%~dp0"
set "BNE_EU_ROAD_DETAILS=on"
set "BNE_TOWNSIGNS=off"
set "BNE_FR_ROADS=on"
set "BNE_FR_HOUSES=on"
set "BNE_FR_HOUSES_INFER=on"
if exist "%~dp0build\regional-pr505-eb73b0f7\manifest.json" set "BNE_REGIONAL_DIR=%~dp0build\regional-pr505-eb73b0f7"
echo Mode France experimental : routes, panneaux et maisons regionales.
echo Gardez cette fenetre ouverte pendant la partie.
py -3.12 start.py -- --regional-poles europe
if errorlevel 1 pause
endlocal
