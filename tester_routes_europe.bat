@echo off
setlocal
cd /d "%~dp0"
set "BNE_EU_ROAD_DETAILS=on"
echo Routes France et glissieres Europe activees.
call "%~dp0tester_france.bat"
endlocal
