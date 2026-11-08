# Start/resume the independent data build; never controls the BeamNG server.
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskWork = Join-Path $taskRoot 'build\service-signs-us'
$taskScript = Join-Path $PSScriptRoot 'build_us_service_signs.py'
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw 'Repository .venv Python is required; install tools/service_signs.requirements.txt in it first.'
}
New-Item -ItemType Directory -Path $taskWork -Force | Out-Null
$taskStatus = Join-Path $taskWork 'status.json'
if (Test-Path -LiteralPath $taskStatus) {
    $taskState = Get-Content -LiteralPath $taskStatus -Raw | ConvertFrom-Json
    if ($taskState.phase -notin @('complete', 'failed')) {
        $taskExisting = Get-CimInstance Win32_Process -Filter ("ProcessId = " + [int]$taskState.pid)
        if ($taskExisting -and $taskExisting.CommandLine -like '*build_us_service_signs.py*') {
            Write-Output ('National build is already running: ' + $taskState.pid)
            exit 0
        }
    }
}
$taskProcess = Start-Process -FilePath $taskPython -ArgumentList @('-u', ('"' + $taskScript + '"')) `
    -WorkingDirectory $taskRoot -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $taskWork 'build.log') `
    -RedirectStandardError (Join-Path $taskWork 'build-error.log') -PassThru
$taskProcess.Id | Set-Content -LiteralPath (Join-Path $taskWork 'launcher.pid')
Write-Output ('Started/resumed national service-sign build: ' + $taskProcess.Id)
