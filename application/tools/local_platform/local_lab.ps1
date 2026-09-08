#requires -Version 5.1
<# Local Windows Docker Desktop lab only. No cloud profiles, no volume deletion. #>
[CmdletBinding()]
param(
    [ValidateSet('Start', 'Test', 'VerifyPersistence', 'Status', 'Stop', 'Check')]
    [string]$Action = 'Status'
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$labDirectory = [IO.Path]::GetFullPath($PSScriptRoot)
$applicationDirectory = [IO.Path]::GetFullPath((Join-Path $labDirectory '../..'))
$contextName = 'desktop-linux'
$builderName = 'desktop-linux'
$savedBuildEnvironment = @{}

function Invoke-LabDocker([string[]]$DockerArguments) {
    & docker --context $contextName @DockerArguments
    if ($LASTEXITCODE -ne 0) {
        throw 'Local container command failed. Keep the output; do not reset volumes.'
    }
}

try {
    # An explicit Docker context does not necessarily select the Buildx builder.
    # Refuse inherited alternate routing/configuration before any Docker call.
    foreach ($name in @('BUILDKIT_HOST', 'BUILDX_CONFIG', 'BUILDX_BAKE_FILE',
            'BUILDX_BAKE_FILE_SEPARATOR', 'DOCKER_CONFIG')) {
        if (-not [string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($name, 'Process'))) {
            throw 'Inherited Docker/Buildx routing overrides are not permitted in this local lab.'
        }
    }
    $inheritedBuilder = [Environment]::GetEnvironmentVariable('BUILDX_BUILDER', 'Process')
    if (-not [string]::IsNullOrEmpty($inheritedBuilder) -and $inheritedBuilder -cne $builderName) {
        throw 'An inherited alternative Buildx builder was refused; use the local desktop-linux builder.'
    }
    foreach ($entry in @{
            BUILDX_BUILDER = $builderName; COMPOSE_BAKE = 'false'; DOCKER_BUILDKIT = '1'
        }.GetEnumerator()) {
        $savedBuildEnvironment[$entry.Key] = [Environment]::GetEnvironmentVariable($entry.Key, 'Process')
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process')
    }
    $labEndpoint = & docker context inspect $contextName --format '{{.Endpoints.docker.Host}}'
    if ($LASTEXITCODE -ne 0 -or $labEndpoint.Trim() -ne 'npipe:////./pipe/dockerDesktopLinuxEngine') {
        throw 'Only the local Windows Docker Desktop Linux engine is permitted.'
    }
    $labOS = & docker --context $contextName info --format '{{.OSType}}/{{.Architecture}}'
    if ($LASTEXITCODE -ne 0 -or $labOS.Trim() -ne 'linux/x86_64') {
        throw 'Start Docker Desktop in Linux x86_64 mode before running this local lab.'
    }
    # Inspect, never bootstrap/create/switch a builder. The integrated docker
    # driver must have exactly one node pointing at the verified local context.
    $labBuilder = @(& docker --context $contextName buildx inspect $builderName)
    if ($LASTEXITCODE -ne 0) { throw 'The fixed local Buildx builder could not be inspected.' }
    $labDrivers = @($labBuilder | Where-Object { $_ -match '^Driver:' })
    $labBuilderEndpoints = @($labBuilder | Where-Object { $_ -match '^Endpoint:' })
    $labBuilderStatuses = @($labBuilder | Where-Object { $_ -match '^Status:' })
    if ($labDrivers.Count -ne 1 -or $labDrivers[0] -notmatch '^Driver:\s+docker\s*$' -or
        $labBuilderEndpoints.Count -ne 1 -or
        $labBuilderEndpoints[0] -notmatch '^Endpoint:\s+desktop-linux\s*$' -or
        $labBuilderStatuses.Count -ne 1 -or $labBuilderStatuses[0] -notmatch '^Status:\s+running\s*$') {
        throw 'Only the integrated, single-node local Docker Desktop builder is permitted.'
    }
    $composeArguments = @(
        'compose', '--project-name', 'medical-app-local-lab',
        '--project-directory', $labDirectory,
        '--env-file', (Join-Path $labDirectory 'compose.env'),
        '-f', (Join-Path $labDirectory 'compose.yaml')
    )
    switch ($Action) {
        'Check' { Invoke-LabDocker ($composeArguments + @('config', '--quiet')) }
        'Start' {
            Invoke-LabDocker ($composeArguments + @('config', '--quiet'))
            # Compose's integrated-builder selection is incompatible with this
            # Docker Desktop/Compose combination. Direct Buildx preserves the
            # verified context and builder; only the local image is loaded.
            Invoke-LabDocker @('buildx', 'build', '--builder', $builderName,
                '--file', (Join-Path $labDirectory 'Dockerfile'),
                '--tag', 'medical-app-local-backend:1.2.0', '--load', $applicationDirectory)
            Invoke-LabDocker ($composeArguments + @('up', '-d', '--no-build', '--wait', '--wait-timeout', '180', 'api'))
            Write-Output 'Isolated HTTPS lab healthy inside Docker. Use Test or VerifyPersistence; no host/browser port is published and no cloud is connected.'
        }
        'Test' {
            Invoke-LabDocker ($composeArguments + @('run', '--rm', '--no-deps', 'smoke'))
        }
        'VerifyPersistence' {
            Invoke-LabDocker ($composeArguments + @(
                'run', '--rm', '--no-deps', 'smoke', 'python', '-m',
                'tools.local_platform.smoke', '--verify-persistence'
            ))
        }
        'Status' { Invoke-LabDocker ($composeArguments + @('ps', '--all')) }
        'Stop' {
            Invoke-LabDocker ($composeArguments + @('stop'))
            Write-Output 'Only this local lab was stopped. All test volumes and history were retained.'
        }
    }
} catch {
    Write-Error $_.Exception.Message
    exit 1
} finally {
    foreach ($entry in $savedBuildEnvironment.GetEnumerator()) {
        if ($null -eq $entry.Value) {
            # Preserve absence, not an empty value (PowerShell/.NET binding differs).
            [Environment]::SetEnvironmentVariable(
                $entry.Key, [System.Management.Automation.Language.NullString]::Value, 'Process'
            )
        } else {
            [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process')
        }
    }
}
