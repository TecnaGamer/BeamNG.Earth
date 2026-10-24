@echo off
rem Points every Rust tool (cargo, rustc, rustdoc) at the custom Windows 7 toolchain in the "rust" folder,
rem the same way the bats point Python at the "python" folder. Called by run_server.bat, launcher.bat, install.bat
rem and build_native.bat. If rust\bin\rustc.exe does not exist it does nothing (the system Rust is left alone).
rem   rust\                      extract the toolchain archive here, so that rust\bin\rustc.exe exists
rem   BNGE_RUST_DIR              use another folder instead of rust\
rem   BNGE_CARGO_HOME            where cargo keeps its crate cache (default: rust\cargo_home)
for %%I in ("%~dp0..\rust") do set "RUST_DIR=%%~fI"
if not "%BNGE_RUST_DIR%"=="" set "RUST_DIR=%BNGE_RUST_DIR%"
if not exist "%RUST_DIR%\bin\rustc.exe" goto :eof
set "PATH=%RUST_DIR%\bin;%PATH%"
set "RUSTC=%RUST_DIR%\bin\rustc.exe"
set "RUSTDOC=%RUST_DIR%\bin\rustdoc.exe"
set "RUSTUP_TOOLCHAIN="
set "RUSTUP_HOME="
set "CARGO_HOME=%RUST_DIR%\cargo_home"
if not "%BNGE_CARGO_HOME%"=="" set "CARGO_HOME=%BNGE_CARGO_HOME%"
if not "%PYTHON%"=="" set "PYO3_PYTHON=%PYTHON%"
goto :eof
