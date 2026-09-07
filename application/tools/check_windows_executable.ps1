param(
    [Parameter(Mandatory = $true)][string]$ExecutablePath,
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [Parameter(Mandatory = $true)][string]$TestDataDirectory
)

$ErrorActionPreference = 'Stop'
$resolvedExecutable = (Resolve-Path -LiteralPath $ExecutablePath).Path
$resolvedPython = (Resolve-Path -LiteralPath $PythonPath).Path
$testTarget = [System.IO.Path]::GetFullPath($TestDataDirectory)
if (Test-Path -LiteralPath $testTarget) {
    throw 'The test data directory must be new to protect existing data.'
}
New-Item -ItemType Directory -Path $testTarget | Out-Null
$oldDataDirectory = $env:OLLAMA_DUAL_CHAT_DATA_DIR
$oldQtPlatform = $env:QT_QPA_PLATFORM
$oldPythonPath = $env:PYTHONPATH
$testProcess = $null
if (-not ('HealthLifeSmokeWindows' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class HealthLifeSmokeWindows {
    private delegate bool EnumWindowProc(IntPtr handle, IntPtr parameter);
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindowProc callback, IntPtr parameter);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr handle, out uint processId);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetWindowText(IntPtr handle, StringBuilder text, int capacity);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr handle, uint message, IntPtr wParam, IntPtr lParam);
    public static string Title(IntPtr handle) {
        var text = new StringBuilder(512);
        GetWindowText(handle, text, text.Capacity);
        return text.ToString();
    }
    public static IntPtr FindOwnedWindow(int processId) {
        IntPtr result = IntPtr.Zero;
        EnumWindows((handle, parameter) => {
            uint owner;
            GetWindowThreadProcessId(handle, out owner);
            if (owner == processId && Title(handle).Contains("健康生活服务平台")) {
                result = handle;
                return false;
            }
            return true;
        }, IntPtr.Zero);
        return result;
    }
}
'@
}
try {
    $env:OLLAMA_DUAL_CHAT_DATA_DIR = $testTarget
    $env:PYTHONPATH = Join-Path (Split-Path $PSScriptRoot -Parent) 'src'
    # Pre-create an empty test DB so frozen legacy-data discovery never imports
    # real data from this computer into the smoke-test directory.
    & $resolvedPython -c 'import os; from pathlib import Path; from ollama_chat_app.data.database import Database; Database(Path(os.environ["OLLAMA_DUAL_CHAT_DATA_DIR"]) / "app.db").initialize()'
    if ($LASTEXITCODE -ne 0) { throw 'Could not initialize the synthetic test database.' }
    $env:QT_QPA_PLATFORM = $null
    $env:PYTHONPATH = $null
    $testProcess = Start-Process -FilePath $resolvedExecutable -WindowStyle Hidden -PassThru
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 250
        $testProcess.Refresh()
        if ($testProcess.HasExited) { throw 'The frozen app exited during startup.' }
        # Process.MainWindowHandle excludes hidden windows. Inspect only windows
        # owned by this freshly launched test process, without showing/focusing them.
        $ownedWindow = [HealthLifeSmokeWindows]::FindOwnedWindow($testProcess.Id)
    } until (($ownedWindow -ne [IntPtr]::Zero) -or ([DateTime]::UtcNow -ge $deadline))
    if ($ownedWindow -eq [IntPtr]::Zero) { throw 'No native application window appeared.' }
    $windowTitle = [HealthLifeSmokeWindows]::Title($ownedWindow)
    if ($windowTitle -notlike '*健康生活服务平台*') {
        throw "Unexpected startup window title: $windowTitle"
    }
    $closeAccepted = [HealthLifeSmokeWindows]::PostMessage(
        $ownedWindow, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero
    )
    if (-not $closeAccepted -or -not $testProcess.WaitForExit(10000)) {
        throw 'The test application did not close gracefully.'
    }
    $logErrors = @()
    $logDirectory = Join-Path $testTarget 'logs'
    if (Test-Path -LiteralPath $logDirectory) {
        $logErrors = @(Get-ChildItem -LiteralPath $logDirectory -File |
            Select-String -Pattern 'ERROR|CRITICAL|Traceback')
    }
    if ($logErrors.Count -gt 0) { throw 'The synthetic startup log contains errors.' }
    [ordered]@{
        status = 'passed'
        executable = $resolvedExecutable
        file_version = (Get-Item -LiteralPath $resolvedExecutable).VersionInfo.FileVersion
        product_version = (Get-Item -LiteralPath $resolvedExecutable).VersionInfo.ProductVersion
        sha256 = (Get-FileHash -LiteralPath $resolvedExecutable -Algorithm SHA256).Hash
        signature_status = (Get-AuthenticodeSignature -LiteralPath $resolvedExecutable).Status.ToString()
        native_window_title = $windowTitle
        exit_code = $testProcess.ExitCode
        log_errors = $logErrors.Count
        synthetic_data_directory = $testTarget
        real_user_data_opened = $false
        ai_requests = 0
    } | ConvertTo-Json
} finally {
    if ($null -ne $testProcess -and -not $testProcess.HasExited) {
        # Only this newly launched smoke-test process can be stopped.
        Stop-Process -Id $testProcess.Id -ErrorAction SilentlyContinue
    }
    $env:OLLAMA_DUAL_CHAT_DATA_DIR = $oldDataDirectory
    $env:QT_QPA_PLATFORM = $oldQtPlatform
    $env:PYTHONPATH = $oldPythonPath
}
