# Offline credential tests in an isolated clone with synthetic values.
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$testRoot = [IO.Path]::GetFullPath((Join-Path $repo ('.local/auth-test-' + [guid]::NewGuid().ToString('N'))))
$expectedParent = [IO.Path]::GetFullPath((Join-Path $repo '.local'))
if ([IO.Path]::GetDirectoryName($testRoot) -ne $expectedParent) { throw 'Unsafe test directory' }
$oldEnv = @{}
foreach ($name in 'OS_PASSWORD','OS_USERNAME','OS_USER_DOMAIN_NAME','OS_PROJECT_ID','OS_AUTH_URL','OS_REGION_NAME','OTUS_SELECTEL_AUTH') {
    $oldEnv[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
try {
    New-Item -ItemType Directory -Force "$testRoot/scripts", "$testRoot/config" | Out-Null
    Copy-Item "$repo/scripts/env.ps1", "$repo/scripts/operator.ps1" "$testRoot/scripts/"
    Copy-Item "$repo/config/selectel-auth.json" "$testRoot/config/"
    function Read-Host {
        param($Prompt, [switch]$AsSecureString)
        if ($AsSecureString) { return ConvertTo-SecureString 'synthetic-password' -AsPlainText -Force }
        if ($Prompt -like '*UUID*') { return ('a' * 32) }
        return 'synthetic-user'
    }
    . "$testRoot/scripts/env.ps1"
    if ($env:OS_PASSWORD -ne 'synthetic-password') { throw 'Prompt contract failed' }
    if (Test-Path "$testRoot/.local/operator.json") { throw 'Review persisted credentials' }
    . "$testRoot/scripts/env.ps1" -SaveLocal
    Assert-OtusPrivateFile "$testRoot/.local/operator.json"
    function Read-Host { throw 'UseSession must not prompt' }
    $env:OS_PASSWORD = 'synthetic-session-password'
    . "$testRoot/scripts/env.ps1" -SaveLocal -UseSession
    $env:OS_PASSWORD = 'changed'
    . "$testRoot/scripts/env.ps1" -Development
    if ($env:OS_PASSWORD -ne 'synthetic-session-password') { throw 'Session save failed' }
    $failed = $false
    try { . "$testRoot/scripts/env.ps1" -UseSession } catch { $failed = $true }
    if (-not $failed) { throw 'UseSession without SaveLocal accepted' }
    $env:OS_PASSWORD = ''
    $failed = $false
    try { . "$testRoot/scripts/env.ps1" -SaveLocal -UseSession } catch { $failed = $true }
    if (-not $failed) { throw 'Incomplete session accepted' }
    . "$testRoot/scripts/env.ps1" -Development
    if ($env:OS_PASSWORD -ne 'synthetic-session-password') { throw 'Failed save modified credentials' }
    function Read-Host {
        param($Prompt, [switch]$AsSecureString)
        if ($AsSecureString) { return ConvertTo-SecureString 'synthetic-password' -AsPlainText -Force }
        if ($Prompt -like '*UUID*') { return ('a' * 32) }
        return 'synthetic-user'
    }
    . "$testRoot/scripts/env.ps1" -SaveLocal
    $env:OS_PASSWORD = 'changed'
    . "$testRoot/scripts/env.ps1" -Development
    if ($env:OS_PASSWORD -ne 'synthetic-password') { throw 'Explicit load failed' }
    . "$testRoot/scripts/env.ps1" -Dev
    $failed = $false
    try { . "$testRoot/scripts/env.ps1" -SaveLocal -Development } catch { $failed = $true }
    if (-not $failed) { throw 'Incompatible switches accepted' }
    # Ordinary prompt must ignore an existing file and stale selector.
    $env:OTUS_SELECTEL_AUTH = 'missing-file'
    . "$testRoot/scripts/env.ps1"
    if (Test-Path Env:OTUS_SELECTEL_AUTH) { throw 'Legacy selector survived' }
    $path = "$testRoot/.local/operator.json"
    $acl = Get-Acl -LiteralPath $path
    $everyone = [Security.Principal.SecurityIdentifier]::new('S-1-1-0')
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($everyone,'Read','Allow'))
    Set-Acl -LiteralPath $path -AclObject $acl
    $failed = $false
    try { . "$testRoot/scripts/env.ps1" -Development } catch { $failed = $true }
    if (-not $failed) { throw 'Shared credential file accepted' }
    Write-Output 'PowerShell credentials contract PASS'
} finally {
    foreach ($name in $oldEnv.Keys) { [Environment]::SetEnvironmentVariable($name, $oldEnv[$name], 'Process') }
    if ([IO.Path]::GetDirectoryName($testRoot) -ne $expectedParent) { throw 'Unsafe cleanup target' }
    if (Test-Path -LiteralPath $testRoot) { Remove-Item -LiteralPath $testRoot -Recurse -Force }
}
