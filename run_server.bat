@echo off
rem BeamNG.Earth (Windows 7 / Vista standalone): start the world server with the recommended options.
rem Uses the Python embedded in the "python" folder next to this file: nothing to install, no PATH needed.
rem Double-click this file, then start BeamNG.drive. Keep this window open while you play.
cd /d "%~dp0"

set "PYDIR=%~dp0python"
if not "%BNGE_PYTHON_DIR%"=="" set "PYDIR=%BNGE_PYTHON_DIR%"
set "PYTHON=%PYDIR%\python.exe"
set "PYTHONW=%PYDIR%\pythonw.exe"
set "PYTHONNOUSERSITE=1"
set "PYTHONUTF8=1"
call "%~dp0win7\rust_env.bat"

if not exist "%PYTHON%" goto nopython

rem Windows 7: make the native module (server\earthfast.pyd) loadable, see win7\patch_pyd.py
if exist "win7\patch_pyd.py" "%PYTHON%" "win7\patch_pyd.py"
if exist ".beamng_earth.json" goto run
echo First start: installing the mod into BeamNG.drive and checking the Python packages...
"%PYTHON%" install.py
if not exist ".beamng_earth.json" goto installfailed
:run
"%PYTHON%" start.py %*
echo.
echo The server has stopped.
pause
exit /b 0

:nopython
echo.
echo The embedded Python was not found:
echo   %PYTHON%
echo Keep the "python" folder next to run_server.bat (or set BNGE_PYTHON_DIR), then run it again.
pause
exit /b 1

:installfailed
echo.
echo The installer did not finish. See the messages above, then double-click run_server.bat again.
pause
exit /b 1
