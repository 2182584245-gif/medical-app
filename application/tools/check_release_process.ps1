param(
    [Parameter(Mandatory = $true)][string]$ExecutablePath,
    [Parameter(Mandatory = $true)][string]$ExpectedSha256,
    [Parameter(Mandatory = $true)][string]$TestRoot,
    [switch]$ConfirmRun
)

# Process/log smoke only: does not control native UI or imply a visual/E2E pass.
$ErrorActionPreference = 'Stop'
$medicalExe = (Resolve-Path -LiteralPath $ExecutablePath).Path
$medicalTest = [IO.Path]::GetFullPath($TestRoot)
$medicalParent = 'D:\medical-app-release-20260914-v160\startup'
if (-not $medicalTest.StartsWith($medicalParent + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'A new child of this release startup directory is required.'
}
if (Test-Path -LiteralPath $medicalTest) { throw 'Refusing to overwrite an existing test directory.' }
if ($ExpectedSha256 -notmatch '^[a-fA-F0-9]{64}$' -or
    (Get-FileHash -LiteralPath $medicalExe -Algorithm SHA256).Hash -ne $ExpectedSha256) {
    throw 'Frozen EXE identity mismatch; nothing started.'
}
if (-not $ConfirmRun) { throw 'Explicit test execution required.' }
$medicalData = Join-Path $medicalTest 'data'
$medicalLocal = Join-Path $medicalTest 'localappdata'
$medicalRoaming = Join-Path $medicalTest 'roaming'
$medicalTemp = Join-Path $medicalTest 'temp'
foreach ($path in @($medicalData, $medicalLocal, $medicalRoaming, $medicalTemp)) {
    New-Item -ItemType Directory -Path $path | Out-Null
}
$medicalNames = @('OLLAMA_DUAL_CHAT_DATA_DIR','LOCALAPPDATA','APPDATA','TEMP','TMP',
    'PYTHONPATH','PYTHONHOME','QT_QPA_PLATFORM','DEEPSEEK_API_KEY','OLLAMA_API_KEY','OPENAI_API_KEY',
    'HEALTHLIFE_CLOUD_BASE_URL')
$medicalPrior = @{}
foreach ($name in $medicalNames) {
    $medicalPrior[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$medicalChild = $null
try {
    $env:OLLAMA_DUAL_CHAT_DATA_DIR = $medicalData
    $env:LOCALAPPDATA = $medicalLocal
    $env:APPDATA = $medicalRoaming
    $env:TEMP = $medicalTemp
    $env:TMP = $medicalTemp
    foreach ($name in $medicalNames | Where-Object { $_ -notin @(
        'OLLAMA_DUAL_CHAT_DATA_DIR','LOCALAPPDATA','APPDATA','TEMP','TMP') }) {
        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
    # An existing isolated empty SQLite file prevents legacy data discovery.
    $medicalBuildPython = 'D:\medical-app-release-20260909\.venv-build\Scripts\python.exe'
    & $medicalBuildPython -I -c 'import sqlite3,sys; sqlite3.connect(sys.argv[1]).close()' (Join-Path $medicalData 'app.db')
    if ($LASTEXITCODE -ne 0) { throw 'Isolated SQLite preparation failed.' }
    $medicalChild = Start-Process -FilePath $medicalExe -WorkingDirectory $medicalTest -WindowStyle Hidden -PassThru
    Start-Sleep -Seconds 8
    $medicalChild.Refresh()
    if ($medicalChild.HasExited) { throw 'Frozen EXE exited during startup.' }
    if (-not (Test-Path -LiteralPath (Join-Path $medicalData 'app.db'))) {
        throw 'Isolated application database missing.'
    }
    $medicalLogs = @(Get-ChildItem -LiteralPath $medicalData -Filter '*.log' -Recurse -File)
    if (@($medicalLogs | Select-String -Pattern 'ERROR|CRITICAL|Traceback|启动失败').Count) {
        throw 'Application error marker found; details remain in isolated log.'
    }
    $medicalReport = [ordered]@{
        status = 'startup_process_alive'
        executable_sha256 = $ExpectedSha256
        process_id = $medicalChild.Id
        observation_seconds = 8
        error_markers_found = $false
        native_ui_automated = $false
        graceful_close_verified = $false
        packet_capture_performed = $false
        login_actions = 0
        ai_actions = 0
        isolated_data_root = $medicalData
        cleanup = 'stop only this test process, preserve all test files'
    }
    $medicalReport | ConvertTo-Json
} finally {
    if ($medicalChild -and -not $medicalChild.HasExited) {
        Stop-Process -Id $medicalChild.Id -ErrorAction SilentlyContinue
        $medicalChild.WaitForExit(10000) | Out-Null
    }
    foreach ($name in $medicalNames) {
        [Environment]::SetEnvironmentVariable($name, $medicalPrior[$name], 'Process')
    }
}
