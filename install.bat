@echo off
rem BeamNG.Earth installer (replaces "python install.py"): mod into BeamNG.drive + Python packages check.
rem Options pass through: install.bat --user-dir "D:\BeamNG\current" --yes   |   --no-venv   |   --dry-run
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
"%PYTHON%" install.py %*
echo.
pause
exit /b 0

:nopython
echo.
echo The embedded Python was not found:
echo   %PYTHON%
echo Keep the "python" folder next to this file (or set BNGE_PYTHON_DIR), then run it again.
pause
exit /b 1
