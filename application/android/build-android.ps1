param(
    [ValidateSet('Debug','Release')][string]$Variant = 'Debug',
    [string]$JavaDirectory = 'C:\Program Files\Java\jdk-21.0.11',
    [string]$SdkDirectory = 'C:\Users\wuyuf\AppData\Local\Android\Sdk',
    [string]$BuildRoot = 'D:\CodexBuild\HealthLifeAndroid-v1.0.2',
    [switch]$SkipLint
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonRuntime = Join-Path $projectRoot '.venv-release-v1.0.1\Scripts\python.exe'
if (!(Test-Path -LiteralPath $pythonRuntime)) { throw 'Please set a Python 3.13 build interpreter.' }
if (!(Test-Path -LiteralPath (Join-Path $JavaDirectory 'bin\java.exe'))) { throw 'Java runtime missing.' }
if (!(Test-Path -LiteralPath (Join-Path $SdkDirectory 'platform-tools'))) { throw 'Android SDK missing.' }
New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null
$env:JAVA_HOME = $JavaDirectory
$env:ANDROID_HOME = $SdkDirectory
$env:GRADLE_USER_HOME = Join-Path $BuildRoot 'gradle-cache'
$env:HEALTH_ANDROID_BUILD_DIR = Join-Path $BuildRoot 'build'
$env:HEALTH_BUILD_PYTHON = $pythonRuntime
try {
    if ($Variant -eq 'Release') {
        $signingDirectory = Join-Path $projectRoot 'packaging\private-android-signing'
        New-Item -ItemType Directory -Force -Path $signingDirectory | Out-Null
        $keyPath = Join-Path $signingDirectory 'healthlife-release.jks'
        $credentialPath = Join-Path $signingDirectory 'signing-password.dpapi.xml'
        if ((Test-Path -LiteralPath $keyPath) -xor (Test-Path -LiteralPath $credentialPath)) {
            throw 'Signing key/password pair incomplete. Do not generate a replacement key.'
        }
        if (!(Test-Path -LiteralPath $keyPath)) {
            $randomBytes = New-Object byte[] 48
            $random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
            $random.GetBytes($randomBytes)
            $random.Dispose()
            $env:HEALTH_SIGNING_PASSWORD = [Convert]::ToBase64String($randomBytes)
            $securePassword = ConvertTo-SecureString $env:HEALTH_SIGNING_PASSWORD -AsPlainText -Force
            [PSCredential]::new('healthlife', $securePassword) | Export-Clixml -LiteralPath $credentialPath
            & (Join-Path $JavaDirectory 'bin\keytool.exe') -genkeypair -keystore $keyPath -storetype JKS -alias healthlife -keyalg RSA -keysize 3072 -validity 10000 -dname 'CN=HealthLife Personal App, OU=Local Personal Use, O=HealthLife, C=CN' -storepass:env HEALTH_SIGNING_PASSWORD -keypass:env HEALTH_SIGNING_PASSWORD
            if ($LASTEXITCODE -ne 0) { throw 'Signing-key generation failed; preserve existing key files for recovery.' }
        } else {
            $signingCredential = Import-Clixml -LiteralPath $credentialPath
            $env:HEALTH_SIGNING_PASSWORD = $signingCredential.GetNetworkCredential().Password
        }
        $env:HEALTH_SIGNING_STORE = $keyPath
    }
    Push-Location $PSScriptRoot
    try {
        $gradleExecutable = Join-Path $BuildRoot 'gradle-8.13\bin\gradle.bat'
        if (!(Test-Path -LiteralPath $gradleExecutable)) { $gradleExecutable = Join-Path $PSScriptRoot 'gradlew.bat' }
        $taskNames = @('--no-daemon', ":app:assemble$Variant")
        if (!$SkipLint) { $taskNames += ":app:lint$Variant" }
        & $gradleExecutable @taskNames
        if ($LASTEXITCODE -ne 0) { throw 'Android build or checks failed.' }
    } finally { Pop-Location }
} finally {
    Remove-Item Env:\HEALTH_SIGNING_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:\HEALTH_SIGNING_STORE -ErrorAction SilentlyContinue
}
