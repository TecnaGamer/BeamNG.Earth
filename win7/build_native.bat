@echo off
rem Rebuilds server\earthfast.pyd (the Rust module) with the custom toolchain in the "rust" folder.
rem Normally not needed: the release already contains a .pyd that win7\patch_pyd.py makes loadable.
cd /d "%~dp0\.."
set "PYDIR=%CD%\python"
if not "%BNGE_PYTHON_DIR%"=="" set "PYDIR=%BNGE_PYTHON_DIR%"
set "PYTHON=%PYDIR%\python.exe"
set "PYTHONNOUSERSITE=1"
set "PYTHONUTF8=1"
if not exist "%PYTHON%" (
  echo The embedded Python was not found: %PYTHON%
  pause
  exit /b 1
)
call "%~dp0rust_env.bat"
if not exist "%RUSTC%" (
  echo The Rust toolchain was not found. Extract it so that rust\bin\rustc.exe exists.
  pause
  exit /b 1
)
echo Using %RUSTC%
"%RUSTC%" -vV
"%PYTHON%" tools\build_native.py %*
"%PYTHON%" win7\patch_pyd.py
echo.
pause
