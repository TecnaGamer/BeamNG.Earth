@echo off
setlocal
cd /d "%~dp0"
set "BNE_FR_ROADS=on"
set "BNE_FR_HOUSES=on"
set "BNE_FR_HOUSES_INFER=on"
echo Mode France experimental : routes, panneaux et maisons regionales.
echo Gardez cette fenetre ouverte pendant la partie.
py -3.12 start.py -- --regional-poles europe
if errorlevel 1 pause
endlocal
