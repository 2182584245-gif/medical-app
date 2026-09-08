#requires -Version 5.1
<#
Source-only publication, never a release/data migration.
Default: read-only preview with SHA-256 manifest metadata. Nothing is copied.
Run from this project's root. Destination is deliberately not configurable.

  .\tools\sync_repository_source.ps1
  .\tools\sync_repository_source.ps1 -AsJson
  .\tools\sync_repository_source.ps1 -Apply -Confirm SOURCE_ONLY `
      -ApprovedManifestSha256 <reviewed preview digest>

The approval means the listed source paths and sensitive-pattern candidates
have been reviewed. A regex scan cannot prove that source contains no secrets.
No values matching a sensitive pattern are emitted, only relative file paths.
Existing root release files are never modified or deleted. Within application,
only hash-journaled files may be replaced; externally changed files are refused.
Interrupted copies keep a pending journal and can be retried without adoption
of unknown files. Stale owned source files are retained, never deleted.
#>
[CmdletBinding()]
param(
    [switch]$Apply,
    [string]$Confirm = '',
    [string]$ApprovedManifestSha256 = '',
    [switch]$AsJson,
    [switch]$ListFiles,
    [switch]$SelfTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:SourceRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)).TrimEnd('\')
$script:RepositoryRoot = 'C:\Users\wuyuf\Desktop\medical-app'
$script:TargetRoot = Join-Path $script:RepositoryRoot 'application'
$script:Owner = 'medical-app-source-sync-v1'
$script:ManifestName = '.source-sync-manifest.json'
$script:SourceDirectories = @('src', 'server', 'tests', 'tools', 'packaging', 'assets', 'android')
$script:TopFiles = @(
    '.gitignore', 'pyproject.toml', 'README.md', 'requirements.txt',
    'requirements-server.txt', 'requirements-lock.txt', 'requirements-dev.txt'
)
$script:TextExtensions = @(
    '.py', '.ps1', '.cmd', '.bat', '.cjs', '.js', '.html', '.css', '.xml',
    '.toml', '.ini', '.cfg', '.conf', '.gradle', '.properties', '.md', '.txt',
    '.json', '.mako', '.spec', '.iss', '.sql', '.svg', '.java', '.kt', '.kts', '.pro'
)
$script:ImageExtensions = @('.png', '.ico', '.jpg', '.jpeg', '.webp')
$script:SpecialTextFiles = @(
    'tools/railway_probe/.python-version',
    'tools/railway_platform/Dockerfile',
    'tools/railway_platform/Dockerfile.dockerignore',
    'tools/local_platform/Dockerfile',
    'tools/local_platform/Dockerfile.dockerignore',
    'tools/local_platform/compose.yaml',
    'tools/local_platform/compose.env'
)
$script:ModelExtensions = @('.mdl', '.fst', '.int', '.stats', '.mat', '.ie', '.dubm')
$script:ExcludedDirectories = @(
    '.git', '.local', '.gradle', '.idea', '.vscode', '__pycache__', 'node_modules',
    '.pytest_cache', '.ruff_cache', '.mypy_cache', 'htmlcov', 'work', 'outputs',
    'output', 'logs', 'log', 'backups', 'backup', 'uploads', 'private-android-signing'
)
# These identify review candidates, not a claim that the matched value is real.
$script:SensitivePatterns = @(
    '(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{20,}',
    '(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{30,}',
    'github_pat_[A-Za-z0-9_]{30,}',
    '(?<![A-Za-z0-9_])sbp_[A-Za-z0-9]{20,}',
    '-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----',
    '(?i)postgres(?:ql)?(?:\+psycopg)?://[^/\s:]+:[^@\s]+@',
    'eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}',
    '(?im)^\s*(?:[A-Z_]*(?:PASSWORD|API_KEY|TOKEN_PEPPER|CLIENT_SECRET))[\s"'']*[:=]\s*["''][A-Za-z0-9_+/=.-]{24,}["'']'
)

function Stop-Sync([string]$Message) {
    $failure = [InvalidOperationException]::new('Source-only publication safety check failed.')
    $failure.Data['source_sync_public_message'] = $Message
    throw $failure
}

function Assert-SafeRelativePath([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path) -or $Path.Contains('\') -or
        $Path.Contains(':') -or $Path -match '[\x00-\x1f<>"|?*]' -or $Path.StartsWith('/') -or
        @($Path.Split('/') | Where-Object { $_ -eq '' -or $_ -eq '.' -or $_ -eq '..' }).Count -gt 0) {
        Stop-Sync 'Unsafe relative source path was refused.'
    }
    foreach ($segment in $Path.Split('/')) {
        if ($segment -match '[. ]$' -or $segment -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)') {
            Stop-Sync 'Windows device name or ambiguous relative path was refused.'
        }
    }
}

function Assert-NoReparseAncestors([string]$Path) {
    $cursor = [IO.Path]::GetFullPath($Path)
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                Stop-Sync 'A symbolic link or junction in a source/destination path was refused.'
            }
        }
        $parent = [IO.Directory]::GetParent($cursor)
        if ($null -eq $parent) { break }
        $cursor = $parent.FullName
    }
}

function Get-ChildPath([string]$Root, [string]$Relative) {
    Assert-SafeRelativePath $Relative
    $path = [IO.Path]::GetFullPath((Join-Path $Root $Relative.Replace('/', '\')))
    if (-not $path.StartsWith($Root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Sync 'A path outside the intended source-only directory was refused.'
    }
    Assert-NoReparseAncestors $path
    return $path
}

function Test-ExcludedDirectory([string]$Name) {
    return $script:ExcludedDirectories -contains $Name -or
        $Name -match '^(?:\.?venv(?:$|[-_])|build(?:$|[-_])|dist(?:$|[-_])|release(?:$|[-_])|wheelhouse(?:$|[-_])|\.env)' -or
        $Name -match '\.egg-info$'
}

function Test-AllowedFile([string]$Relative) {
    Assert-SafeRelativePath $Relative
    $parts = $Relative.Split('/')
    if ($parts.Count -eq 1) { return $script:TopFiles -contains $Relative }
    if ($script:SourceDirectories -notcontains $parts[0]) { return $false }
    foreach ($part in $parts[0..($parts.Count - 2)]) {
        if (Test-ExcludedDirectory $part) { return $false }
    }
    $name = $parts[-1]
    if ($name -match '(?i)^(?:\.env(?:\..*)?|local\.properties|google-services\.json)$' -or
        $name -match '(?i)(?:\.db(?:-(?:wal|shm))?|\.sqlite(?:3)?(?:-(?:wal|shm))?|\.log|\.py[co]|\.pfx|\.p12|\.pem|\.key|\.jks|\.keystore|\.dpapi(?:\.xml)?|\.bak|\.zip|\.apk|\.aab|\.exe|\.dll|\.pyd)$' -or
        $name -match '(?i)(?:credentials|service[-_]?account|signing-password|runtime-secret).*\.json$') {
        return $false
    }
    $extension = [IO.Path]::GetExtension($name).ToLowerInvariant()
    if ($script:TextExtensions -contains $extension) { return $true }
    if ($name -in @('README', 'LICENSE', 'NOTICE', '.gitignore', 'gradlew')) { return $true }
    if ($script:SpecialTextFiles -contains $Relative) { return $true }
    if ($Relative -eq 'android/gradle/wrapper/gradle-wrapper.jar') { return $true }
    if (($parts[0] -eq 'assets' -or $Relative.StartsWith('android/app/src/main/res/')) -and
        $script:ImageExtensions -contains $extension) { return $true }
    return $Relative.StartsWith('assets/vosk-model-small-cn-0.22/') -and
        $script:ModelExtensions -contains $extension
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-TextSha256([string]$Text) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash(
            [Text.Encoding]::UTF8.GetBytes($Text)))).Replace('-', '').ToLowerInvariant()
    } finally { $algorithm.Dispose() }
}

function Get-SourceFiles {
    $selected = [Collections.Generic.List[object]]::new()
    $excluded = [Collections.Generic.List[string]]::new()
    $pending = [Collections.Generic.Stack[string]]::new()
    foreach ($name in $script:SourceDirectories) {
        $path = Get-ChildPath $script:SourceRoot $name
        if (Test-Path -LiteralPath $path -PathType Container) { $pending.Push($name) }
    }
    foreach ($name in $script:TopFiles) {
        $path = Get-ChildPath $script:SourceRoot $name
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $selected.Add([pscustomobject]@{ relative = $name; full_path = $path })
        }
    }
    while ($pending.Count -gt 0) {
        $relative = $pending.Pop()
        foreach ($item in Get-ChildItem -LiteralPath (Get-ChildPath $script:SourceRoot $relative) -Force) {
            $child = $relative + '/' + $item.Name
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                Stop-Sync "Source symbolic link or junction refused: $child"
            }
            if ($item.PSIsContainer) {
                if (Test-ExcludedDirectory $item.Name) { $excluded.Add($child + '/') }
                else { $pending.Push($child) }
            } elseif (Test-AllowedFile $child) {
                $selected.Add([pscustomobject]@{ relative = $child; full_path = $item.FullName })
            } else { $excluded.Add($child) }
        }
    }
    return [pscustomobject]@{ files = @($selected | Sort-Object relative); excluded = @($excluded | Sort-Object) }
}

function Read-OwnedManifest {
    $path = Join-Path $script:TargetRoot $script:ManifestName
    $owned = @{}
    $script:ManifestReadSha256 = $null
    if (-not (Test-Path -LiteralPath $script:TargetRoot)) { return $owned }
    if (-not (Test-Path -LiteralPath $path)) {
        if (@(Get-ChildItem -LiteralPath $script:TargetRoot -Force).Count -gt 0) {
            Stop-Sync 'Existing non-empty application directory has no ownership manifest; it will not be adopted.'
        }
        return $owned
    }
    Assert-NoReparseAncestors $path
    if ((Get-Item -LiteralPath $path).Length -gt 8MB) { Stop-Sync 'Oversized ownership manifest refused.' }
    $script:ManifestReadSha256 = Get-Sha256 $path
    $manifest = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    if ((Get-Sha256 $path) -cne $script:ManifestReadSha256) { Stop-Sync 'Ownership manifest changed while being read.' }
    if ($manifest.owner -ne $script:Owner -or $manifest.target -ne $script:TargetRoot -or
        $manifest.version -ne 1 -or $manifest.source -ne $script:SourceRoot) {
        Stop-Sync 'Unknown ownership manifest refused.'
    }
    foreach ($entry in $manifest.files) {
        Assert-SafeRelativePath $entry.path
        if (-not (Test-AllowedFile $entry.path) -or $owned.ContainsKey($entry.path) -or
            ($entry.sha256 -and $entry.sha256 -notmatch '^[0-9a-f]{64}$') -or
            ($entry.pending_sha256 -and $entry.pending_sha256 -notmatch '^[0-9a-f]{64}$') -or
            (-not $entry.sha256 -and -not $entry.pending_sha256)) {
            Stop-Sync 'Invalid owned source-file entry refused.'
        }
        $owned[$entry.path] = $entry
    }
    return $owned
}

function New-PublicationPlan {
    if ([IO.Path]::GetFullPath((Get-Location).Path).TrimEnd('\') -ne $script:SourceRoot) {
        Stop-Sync 'Run this tool from its own project root, not another working directory.'
    }
    if ($script:TargetRoot -ne 'C:\Users\wuyuf\Desktop\medical-app\application') {
        Stop-Sync 'Fixed destination check failed.'
    }
    Assert-NoReparseAncestors $script:SourceRoot
    Assert-NoReparseAncestors $script:TargetRoot
    if (-not (Test-Path -LiteralPath (Join-Path $script:RepositoryRoot '.git') -PathType Container)) {
        Stop-Sync 'The intended desktop Git repository was not found.'
    }
    $inventory = Get-SourceFiles
    $owned = Read-OwnedManifest
    $files = [Collections.Generic.List[object]]::new()
    $findings = [Collections.Generic.List[string]]::new()
    $total = [long]0
    foreach ($file in $inventory.files) {
        $relative = $file.relative
        $source = $file.full_path
        $length = (Get-Item -LiteralPath $source).Length
        $hash = Get-Sha256 $source
        $extension = [IO.Path]::GetExtension($relative).ToLowerInvariant()
        $isText = $script:TextExtensions -contains $extension -or
            $script:SpecialTextFiles -contains $relative -or
            [IO.Path]::GetFileName($relative) -in @('README', 'LICENSE', 'NOTICE', '.gitignore', 'gradlew')
        if ($isText) {
            if ($length -gt 8MB) { Stop-Sync "Oversized text source requires review: $relative" }
            $content = [IO.File]::ReadAllText($source)
            foreach ($pattern in $script:SensitivePatterns) {
                if ([regex]::IsMatch($content, $pattern)) { $findings.Add($relative); break }
            }
            $content = $null
        }
        if ((Get-Sha256 $source) -cne $hash) { Stop-Sync "Source changed during sensitive scan: $relative" }
        $target = Get-ChildPath $script:TargetRoot $relative
        $targetHash = $null
        if (Test-Path -LiteralPath $target) {
            if (-not $owned.ContainsKey($relative) -or
                -not (Test-Path -LiteralPath $target -PathType Leaf)) {
                Stop-Sync "Unknown destination file will not be overwritten: $relative"
            }
            $targetHash = Get-Sha256 $target
            if ($targetHash -ne $owned[$relative].sha256 -and
                $targetHash -ne $owned[$relative].pending_sha256) {
                Stop-Sync "Externally modified owned source will not be overwritten: $relative"
            }
        }
        $action = if ($targetHash -eq $hash) { 'unchanged' } elseif ($targetHash) { 'update' } else { 'create' }
        $files.Add([ordered]@{ path = $relative; bytes = $length; sha256 = $hash; target_sha256 = $targetHash; action = $action })
        $total += $length
    }
    $plan = [ordered]@{
        owner = $script:Owner; source = $script:SourceRoot; target = $script:TargetRoot
        file_count = $files.Count; total_bytes = $total
        sensitive_candidate_paths = @($findings | Sort-Object -Unique)
        excluded_paths = $inventory.excluded
        retained_stale_owned_paths = @($owned.Keys | Where-Object { $_ -notin @($files | ForEach-Object { $_.path }) } | Sort-Object)
        files = @($files)
    }
    # Ordinal, serializer-independent fingerprint is stable across PowerShell 5/7.
    $fingerprint = [Collections.Generic.List[string]]::new()
    $fingerprint.Add('owner|' + $script:Owner)
    $fingerprint.Add('source|' + $script:SourceRoot)
    $fingerprint.Add('target|' + $script:TargetRoot)
    foreach ($file in $files) {
        $fingerprint.Add('file|' + $file.path + '|' + $file.bytes + '|' + $file.sha256 + '|' + $file.target_sha256 + '|' + $file.action)
    }
    foreach ($candidate in $plan.sensitive_candidate_paths) { $fingerprint.Add('review|' + $candidate) }
    foreach ($excluded in $plan.excluded_paths) { $fingerprint.Add('excluded|' + $excluded) }
    foreach ($stale in $plan.retained_stale_owned_paths) { $fingerprint.Add('retained|' + $stale) }
    $canonical = $fingerprint.ToArray()
    [Array]::Sort($canonical, [StringComparer]::Ordinal)
    $digest = Get-TextSha256 ($canonical -join "`n")
    return [pscustomobject]@{ plan = $plan; digest = $digest; owned = $owned }
}

function Save-OwnedManifest($Entries) {
    $path = Join-Path $script:TargetRoot $script:ManifestName
    Assert-NoReparseAncestors $path
    $temporary = Join-Path $script:TargetRoot ('.source-sync-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $document = [ordered]@{
        owner = $script:Owner; version = 1; source = $script:SourceRoot; target = $script:TargetRoot
        files = @($Entries.Values | Sort-Object path)
    }
    $stream = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes(($document | ConvertTo-Json -Depth 8))
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } finally { $stream.Dispose() }
    if (Test-Path -LiteralPath $path) {
        if (-not $script:ManifestReadSha256 -or (Get-Sha256 $path) -cne $script:ManifestReadSha256) {
            Stop-Sync 'Unknown or externally changed ownership manifest will not be replaced.'
        }
        # $null binds to an empty string for this .NET string parameter in
        # PowerShell; NullString preserves an actual null backup path.
        [IO.File]::Replace($temporary, $path, [System.Management.Automation.Language.NullString]::Value)
    } else {
        if ($script:ManifestReadSha256) { Stop-Sync 'Ownership manifest disappeared; re-run preview.' }
        [IO.File]::Move($temporary, $path)
    }
    $script:ManifestReadSha256 = Get-Sha256 $path
}

function Invoke-ApprovedCopy($Preview) {
    if ($Confirm -cne 'SOURCE_ONLY' -or $ApprovedManifestSha256 -cne $Preview.digest) {
        Stop-Sync 'Copy requires SOURCE_ONLY and the exact SHA-256 digest of a reviewed current preview.'
    }
    $mutex = [Threading.Mutex]::new($false, 'Local\MedicalAppSourceSyncV1')
    $locked = $false
    try {
        $locked = $mutex.WaitOne(0)
        if (-not $locked) { Stop-Sync 'Another source-sync operation is already running.' }
        $fresh = New-PublicationPlan
        if ($fresh.digest -cne $Preview.digest) { Stop-Sync 'Source or destination changed after preview; review a new preview.' }
        if (-not (Test-Path -LiteralPath $script:TargetRoot)) {
            New-Item -ItemType Directory -Path $script:TargetRoot | Out-Null
        }
        $owned = $fresh.owned
        foreach ($file in $fresh.plan.files) {
            $owned[$file.path] = [ordered]@{
                path = $file.path; sha256 = $file.target_sha256; pending_sha256 = $file.sha256
            }
        }
        # Journal all intended hashes before touching any source destination.
        Save-OwnedManifest $owned
        foreach ($file in $fresh.plan.files) {
            if ($file.action -eq 'unchanged') { continue }
            $source = Get-ChildPath $script:SourceRoot $file.path
            $target = Get-ChildPath $script:TargetRoot $file.path
            $parent = Split-Path -Parent $target
            if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent | Out-Null }
            $temporary = Join-Path $parent ('.source-sync-' + [Guid]::NewGuid().ToString('N') + '.tmp')
            $inputStream = [IO.File]::Open($source, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
            try {
                $outputStream = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
                try { $inputStream.CopyTo($outputStream); $outputStream.Flush($true) }
                finally { $outputStream.Dispose() }
            } finally { $inputStream.Dispose() }
            if ((Get-Sha256 $temporary) -cne $file.sha256) { Stop-Sync "Source changed during copy; pending journal retained: $($file.path)" }
            Assert-NoReparseAncestors $target
            if (Test-Path -LiteralPath $target) {
                if (-not $file.target_sha256 -or (Get-Sha256 $target) -cne $file.target_sha256) {
                    Stop-Sync "Destination changed during copy; pending journal retained: $($file.path)"
                }
                [IO.File]::Replace($temporary, $target, [System.Management.Automation.Language.NullString]::Value)
            } else {
                if ($file.target_sha256) { Stop-Sync 'An owned destination disappeared during copy; re-run preview.' }
                [IO.File]::Move($temporary, $target)
            }
            if ((Get-Sha256 $target) -cne $file.sha256) { Stop-Sync 'Destination verification failed; pending journal retained.' }
        }
        foreach ($file in $fresh.plan.files) {
            $target = Get-ChildPath $script:TargetRoot $file.path
            if ((Get-Sha256 $target) -cne $file.sha256) { Stop-Sync 'Final source verification failed; pending journal retained.' }
            $owned[$file.path] = [ordered]@{ path = $file.path; sha256 = $file.sha256; pending_sha256 = $null }
        }
        Save-OwnedManifest $owned
    } finally {
        if ($locked) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}

function Invoke-PureSelfTest {
    $cases = @{
        'src/ollama_chat_app/data/database.py' = $true
        'server/platform_admin.py' = $true
        'tools/sync_repository_source.ps1' = $true
        'tools/railway_probe/.python-version' = $true
        'tools/railway_probe/.env' = $false
        'tools/railway_probe/credentials.json' = $false
        'tools/railway_platform/Dockerfile' = $true
        'tools/railway_platform/Dockerfile.dockerignore' = $true
        'tools/railway_platform/.env' = $false
        'tools/railway_platform/runtime-secret.json' = $false
        'tools/railway_platform/history.sqlite' = $false
        'tools/other/Dockerfile' = $false
        'android/gradle/wrapper/gradle-wrapper.jar' = $true
        'android/app/src/main/java/cn/healthlife/local/MainActivity.java' = $true
        'assets/vosk-model-small-cn-0.22/am/final.mdl' = $true
        'packaging/private-android-signing/healthlife-release.jks' = $false
        'server/.local/config.json' = $false
        'server/.env.example' = $false
        'android/.gradle/cache.json' = $false
        'android/app/build/result.txt' = $false
        'src/x/history.sqlite3' = $false
        'tests/output/records.json' = $false
        'tools/key.pem' = $false
        'src/thing.egg-info/PKG-INFO' = $false
        'android/local.properties' = $false
        'assets/personal-data.csv' = $false
        'README.md' = $true
        'requirements-server.txt' = $true
        'data/users.json' = $false
    }
    foreach ($entry in $cases.GetEnumerator()) {
        if ((Test-AllowedFile $entry.Key) -ne $entry.Value) { Stop-Sync "Filter self-test failed: $($entry.Key)" }
    }
    foreach ($bad in @('../escape.py', '/absolute.py', 'src//x.py', 'src/../x.py', 'C:/x.py', 'src\x.py', 'src/NUL.py', 'src/name.')) {
        $rejected = $false
        try { Assert-SafeRelativePath $bad } catch { $rejected = $true }
        if (-not $rejected) { Stop-Sync 'Path containment self-test failed.' }
    }
    if ((Get-TextSha256 'abc') -ne 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad') {
        Stop-Sync 'SHA-256 self-test failed.'
    }
    return [pscustomobject]@{ status = 'pure_self_tests_passed'; checks = ($cases.Count + 9); files_written = 0 }
}

try {
    if ($SelfTest) {
        if ($Apply) { Stop-Sync 'Self-test never permits copy.' }
        Invoke-PureSelfTest
        return
    }
    $preview = New-PublicationPlan
    if ($Apply) { Invoke-ApprovedCopy $preview }
    $report = [ordered]@{
        status = $(if ($Apply) { 'source_copied_and_hash_verified' } else { 'dry_run_no_files_written' })
        manifest_sha256 = $preview.digest
        file_count = $preview.plan.file_count
        total_bytes = $preview.plan.total_bytes
        target = $script:TargetRoot
        sensitive_candidate_paths = $preview.plan.sensitive_candidate_paths
        excluded_path_count = $preview.plan.excluded_paths.Count
        retained_stale_owned_paths = $preview.plan.retained_stale_owned_paths
        root_release_files_changed = $false
        files_deleted = 0
    }
    if ($AsJson) { $report['manifest'] = $preview.plan; $report | ConvertTo-Json -Depth 9 }
    else {
        [pscustomobject]$report | Format-List
        if ($ListFiles) {
            $preview.plan.files | ForEach-Object { [pscustomobject]$_ } | Format-Table path, bytes, action -AutoSize
            'Excluded paths (contents not scanned):'
            $preview.plan.excluded_paths
        }
    }
} catch {
    # Driver/JSON/parser exceptions might contain input: never print their messages.
    $exceptionKind = $_.Exception.GetType().Name
    if ($null -ne $_.Exception.InnerException) { $exceptionKind = $_.Exception.InnerException.GetType().Name }
    $safeMessage = 'Filesystem or manifest operation failed. No raw error or file contents disclosed. ' +
        'Diagnostic type=' + $exceptionKind + ', script line=' + $_.InvocationInfo.ScriptLineNumber
    if ($_.Exception.Data.Contains('source_sync_public_message')) {
        $safeMessage = [string]$_.Exception.Data['source_sync_public_message']
    }
    Write-Error ('Source-only publication stopped: ' + $safeMessage)
    exit 1
}
