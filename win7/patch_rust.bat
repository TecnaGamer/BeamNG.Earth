@echo off
rem Windows 7: make a Rust toolchain start (rustc.exe / cargo.exe / rustc_driver-*.dll ask for DLLs that only
rem exist on Windows 8/10: api-ms-win-core-winrt-error-l1-1-0.dll, bcryptprimitives.dll ProcessPrng, WaitOnAddress).
rem It rewrites those imports (same patcher as for earthfast.pyd) and puts efw7shim.dll / efw7err.dll beside each
rem patched file. Originals are kept as *.orig. Run it once after installing or updating Rust.
rem   win7\patch_rust.bat                      patches %USERPROFILE%\.rustup\toolchains and %USERPROFILE%\.cargo\bin
rem   win7\patch_rust.bat "D:\rust\toolchain"  patches the given folder(s) instead
cd /d "%~dp0\.."
set "PYDIR=%CD%\python"
if not "%BNGE_PYTHON_DIR%"=="" set "PYDIR=%BNGE_PYTHON_DIR%"
set "PYTHON=%PYDIR%\python.exe"
set "PYTHONNOUSERSITE=1"
if not exist "%PYTHON%" (
  echo The embedded Python was not found: %PYTHON%
  pause
  exit /b 1
)
set "RUSTUP=%RUSTUP_HOME%"
if "%RUSTUP%"=="" set "RUSTUP=%USERPROFILE%\.rustup"
set "CARGO=%CARGO_HOME%"
if "%CARGO%"=="" set "CARGO=%USERPROFILE%\.cargo"
if "%~1"=="" (
  "%PYTHON%" "win7\patch_pyd.py" "%RUSTUP%\toolchains" "%CARGO%\bin"
) else (
  "%PYTHON%" "win7\patch_pyd.py" %*
)
echo.
pause
