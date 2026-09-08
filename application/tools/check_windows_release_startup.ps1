param(
    [Parameter(Mandatory = $true)][string]$ExecutablePath,
    [Parameter(Mandatory = $true)][string]$ExpectedSha256,
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [Parameter(Mandatory = $true)][string]$TestRoot,
    [string]$ExpectedVersion = '1.4.0',
    [int]$StartupTimeoutSeconds = 45,
    [switch]$ConfirmRun
)

$ErrorActionPreference = 'Stop'
$resolvedExecutable = (Resolve-Path -LiteralPath $ExecutablePath).Path
$resolvedPython = (Resolve-Path -LiteralPath $PythonPath).Path
$testTarget = [IO.Path]::GetFullPath($TestRoot)
$sourceRoot = Split-Path $PSScriptRoot -Parent
$startupParent = [IO.Path]::GetFullPath('D:\medical-app-release-20260908\startup')
if ($ExpectedSha256 -notmatch '^[a-fA-F0-9]{64}$') { throw 'An exact expected EXE SHA256 is required.' }
if ((Get-FileHash -LiteralPath $resolvedExecutable -Algorithm SHA256).Hash -ne $ExpectedSha256) {
    throw 'EXE hash differs from the frozen, approved build; nothing started.'
}
if (-not $testTarget.StartsWith($startupParent.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'A new child directory under this release startup directory is required.'
}
if (Test-Path -LiteralPath $testTarget) { throw 'Startup target already exists; nothing overwritten.' }
if ($StartupTimeoutSeconds -lt 5 -or $StartupTimeoutSeconds -gt 60) { throw 'Startup timeout must be 5-60 seconds.' }
$expectedPattern = '^' + [regex]::Escape($ExpectedVersion) + '(\.0)?$'
$versionInfo = (Get-Item -LiteralPath $resolvedExecutable).VersionInfo
if ($versionInfo.FileVersion -notmatch $expectedPattern -or $versionInfo.ProductVersion -notmatch $expectedPattern) {
    throw 'File or product version does not match the frozen release.'
}
if (-not $ConfirmRun) {
    [ordered]@{ status='preflight_only'; executable=$resolvedExecutable; sha256=$ExpectedSha256;
        file_version=$versionInfo.FileVersion; product_version=$versionInfo.ProductVersion;
        would_create=$testTarget; process_started=$false } | ConvertTo-Json
    exit 0
}

# No security/firewall changes, login actions, or API requests are performed.
# All mutable app locations are fresh; the empty DB prevents legacy discovery.
New-Item -ItemType Directory -Path $testTarget | Out-Null
$dataDirectory = Join-Path $testTarget 'data'
$localDirectory = Join-Path $testTarget 'localappdata'
$roamingDirectory = Join-Path $testTarget 'roaming'
$tempDirectory = Join-Path $testTarget 'temp'
foreach ($directory in @($dataDirectory, $localDirectory, $roamingDirectory, $tempDirectory)) {
    New-Item -ItemType Directory -Path $directory | Out-Null
}
$isolatedNames = @('OLLAMA_DUAL_CHAT_DATA_DIR','LOCALAPPDATA','APPDATA','TEMP','TMP',
    'QT_QPA_PLATFORM','PYTHONPATH','PYTHONHOME','DEEPSEEK_API_KEY','OLLAMA_API_KEY','OPENAI_API_KEY',
    'HEALTHLIFE_CLOUD_BASE_URL')
$priorEnvironment = @{}
foreach ($name in $isolatedNames) {
    $priorEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$testProcess = $null
$report = [ordered]@{status='failed'; executable=$resolvedExecutable; expected_sha256=$ExpectedSha256;
    version=$ExpectedVersion; synthetic_root=$testTarget; login_actions=0; ai_actions=0;
    packet_capture_performed=$false; environment_isolated=$true; process_started=$false}
if (-not ('MedicalReleaseStartupWindows' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public static class MedicalReleaseStartupWindows {
    private delegate bool EnumWindowProc(IntPtr handle, IntPtr parameter);
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindowProc callback, IntPtr parameter);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr handle, out uint processId);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetWindowText(IntPtr handle, StringBuilder text, int capacity);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr handle, uint message, IntPtr wParam, IntPtr lParam);
    public static string Title(IntPtr handle) {
        var text = new StringBuilder(1024); GetWindowText(handle, text, text.Capacity); return text.ToString();
    }
    public static IntPtr[] FindOwnedWindows(int processId) {
        var result = new List<IntPtr>();
        EnumWindows((handle, parameter) => {
            uint owner; GetWindowThreadProcessId(handle, out owner);
            if (owner == processId && Title(handle).Contains("健康生活服务平台")) result.Add(handle);
            return true;
        }, IntPtr.Zero);
        return result.ToArray();
    }
}
'@
}
try {
    $env:OLLAMA_DUAL_CHAT_DATA_DIR = $dataDirectory
    $env:LOCALAPPDATA = $localDirectory
    $env:APPDATA = $roamingDirectory
    $env:TEMP = $tempDirectory
    $env:TMP = $tempDirectory
    $env:QT_QPA_PLATFORM = $null
    $env:PYTHONPATH = $null
    $env:PYTHONHOME = $null
    $env:DEEPSEEK_API_KEY = $null
    $env:OLLAMA_API_KEY = $null
    $env:OPENAI_API_KEY = $null
    $env:HEALTHLIFE_CLOUD_BASE_URL = $null
    & $resolvedPython -I -c 'import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from ollama_chat_app.data.database import Database; Database(Path(sys.argv[2])).initialize()' (Join-Path $sourceRoot 'src') (Join-Path $dataDirectory 'app.db')
    if ($LASTEXITCODE -ne 0) { throw 'Isolated empty database initialization failed.' }
    $testProcess = Start-Process -FilePath $resolvedExecutable -WorkingDirectory $testTarget -WindowStyle Hidden -PassThru
    $report.process_started = $true
    $report.process_id = $testProcess.Id
    $deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    $ownedWindows = @()
    do {
        Start-Sleep -Milliseconds 200
        $testProcess.Refresh()
        if ($testProcess.HasExited) { throw 'The frozen application exited during startup.' }
        $ownedWindows = @([MedicalReleaseStartupWindows]::FindOwnedWindows($testProcess.Id))
    } until ($ownedWindows.Count -gt 0 -or [DateTime]::UtcNow -ge $deadline)
    if ($ownedWindows.Count -ne 1) { throw 'Exactly one main window owned by this process is required.' }
    $window = $ownedWindows[0]
    $title = [MedicalReleaseStartupWindows]::Title($window)
    if ($title -ne '健康生活服务平台 — 云端模式') { throw 'Startup did not show the expected default cloud-mode main window.' }
    $report.native_window_title = $title
    # Observe for a short stabilization period; do not click, focus or enter any data.
    Start-Sleep -Milliseconds 1000
    $testProcess.Refresh()
    if ($testProcess.HasExited) { throw 'Application exited before the close check.' }
    $closeAccepted = [MedicalReleaseStartupWindows]::PostMessage($window, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero)
    if (-not $closeAccepted -or -not $testProcess.WaitForExit(10000)) {
        throw 'Application did not close normally after WM_CLOSE.'
    }
    if ($testProcess.ExitCode -ne 0) { throw 'Application returned a nonzero exit code.' }
    $logDirectory = Join-Path $dataDirectory 'logs'
    $logErrors = @()
    if (Test-Path -LiteralPath $logDirectory) {
        $logErrors = @(Get-ChildItem -LiteralPath $logDirectory -File | Select-String -Pattern 'ERROR|CRITICAL|Traceback')
    }
    if ($logErrors.Count -gt 0) { throw 'Isolated startup logs contain errors.' }
    $dbReport = & $resolvedPython -I -c 'import json,sqlite3,sys; c=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro",uri=True); print(json.dumps({"schema":c.execute("PRAGMA user_version").fetchone()[0],"integrity":c.execute("PRAGMA integrity_check").fetchone()[0],"users":c.execute("SELECT count(*) FROM users").fetchone()[0],"fk":len(c.execute("PRAGMA foreign_key_check").fetchall())})); c.close()' (Join-Path $dataDirectory 'app.db')
    if ($LASTEXITCODE -ne 0) { throw 'Isolated post-startup database check failed.' }
    $database = $dbReport | ConvertFrom-Json
    if ($database.schema -ne 6 -or $database.integrity -ne 'ok' -or $database.users -ne 0 -or $database.fk -ne 0) {
        throw 'Startup test database was not an intact empty schema 6 database.'
    }
    if ((Get-FileHash -LiteralPath $resolvedExecutable -Algorithm SHA256).Hash -ne $ExpectedSha256) {
        throw 'EXE changed during startup verification.'
    }
    $report.status = 'passed'
    $report.exit_code = $testProcess.ExitCode
    $report.file_version = $versionInfo.FileVersion
    $report.product_version = $versionInfo.ProductVersion
    $report.log_errors = 0
    $report.database = $database
    $report.signature_status = (Get-AuthenticodeSignature -LiteralPath $resolvedExecutable).Status.ToString()
} catch {
    $report.failure = 'Startup verification failed; inspect only this isolated run directory.'
    throw
} finally {
    if ($null -ne $testProcess -and -not $testProcess.HasExited) {
        # Failure cleanup targets only the just-created process, never another app instance.
        Stop-Process -Id $testProcess.Id -ErrorAction SilentlyContinue
        $report.forced_cleanup = $true
    }
    foreach ($name in $isolatedNames) {
        [Environment]::SetEnvironmentVariable($name, $priorEnvironment[$name], 'Process')
    }
    $json = $report | ConvertTo-Json -Depth 6
    $json | Set-Content -LiteralPath (Join-Path $testTarget 'startup-report.json') -Encoding UTF8
    Write-Output $json
}
