[CmdletBinding()]
param(
    [ValidateSet('Debug', 'Release')]
    [string]$Variant = 'Debug',
    [string]$Python = $env:HEALTH_BUILD_PYTHON,
    [switch]$Check
)

$ErrorActionPreference = 'Stop'

if ($Variant -eq 'Release') {
    if ([string]::IsNullOrWhiteSpace($env:HEALTH_SIGNING_STORE) -or
        [string]::IsNullOrWhiteSpace($env:HEALTH_SIGNING_PASSWORD)) {
        throw 'Provide HEALTH_SIGNING_STORE and HEALTH_SIGNING_PASSWORD for release signing.'
    }
    if (!(Test-Path -LiteralPath $env:HEALTH_SIGNING_STORE -PathType Leaf)) {
        throw 'HEALTH_SIGNING_STORE must point to an existing keystore file.'
    }
}

$wrapper = './gradlew'
if ([System.IO.Path]::DirectorySeparatorChar -eq '\') {
    $wrapper = './gradlew.bat'
}

$previousPython = $env:HEALTH_BUILD_PYTHON
if ($Python) {
    $env:HEALTH_BUILD_PYTHON = $Python
}
$tasks = @(":app:assemble$Variant")
if ($Check) {
    $tasks += ":app:lint$Variant"
    $tasks += ':app:compileDebugAndroidTestJavaWithJavac'
}

Push-Location -LiteralPath (Join-Path $PSScriptRoot 'android')
try {
    & $wrapper --no-daemon --console=plain @tasks
    if ($LASTEXITCODE -ne 0) {
        throw "Gradle failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
    $env:HEALTH_BUILD_PYTHON = $previousPython
}

