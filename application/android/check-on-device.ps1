param(
    [Parameter(Mandatory=$true)][string]$Serial,
    [string]$SdkDirectory = 'C:\Users\wuyuf\AppData\Local\Android\Sdk',
    [string]$BuildRoot = 'D:\CodexBuild\HealthLifeAndroid-v1.0.2'
)
$ErrorActionPreference = 'Stop'
$adbExecutable = Join-Path $SdkDirectory 'platform-tools\adb.exe'
$debugPackage = Join-Path $BuildRoot 'build\app\outputs\apk\debug\app-debug.apk'
$testPackage = Join-Path $BuildRoot 'build\app\outputs\apk\androidTest\debug\app-debug-androidTest.apk'
if (!(Test-Path -LiteralPath $debugPackage) -or !(Test-Path -LiteralPath $testPackage)) {
    throw 'Build assembleDebug and assembleDebugAndroidTest first.'
}
$state = & $adbExecutable -s $Serial get-state
if ($LASTEXITCODE -ne 0 -or $state.Trim() -ne 'device') { throw 'Selected Android device is not authorized/ready.' }
# The .debug package is separate from the personal release app and its real data.
& $adbExecutable -s $Serial install -r $debugPackage
if ($LASTEXITCODE -ne 0) { throw 'Debug APK installation failed.' }
& $adbExecutable -s $Serial install -r $testPackage
if ($LASTEXITCODE -ne 0) { throw 'Instrumentation APK installation failed.' }
$deviceResult = & $adbExecutable -s $Serial shell am instrument -w -r 'cn.healthlife.local.debug.test/androidx.test.runner.AndroidJUnitRunner' 2>&1
$runnerExit = $LASTEXITCODE
$resultText = $deviceResult -join [Environment]::NewLine
Write-Output $resultText
if ($runnerExit -ne 0 -or $resultText -notmatch '(?m)^OK \(7 tests\)') {
    throw 'Device tests did not report all 7 passing tests. Inspect the output.'
}
# Android instrumentation can return shell code 0 even when a test fails:
# require an actual OK (7 tests) summary in the captured output before claiming success.
