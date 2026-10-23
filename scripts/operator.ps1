# Credential file operations are explicit and confined to this clone.
function Get-OtusAuthSchema {
    Get-Content -LiteralPath (Join-Path $PSScriptRoot '../config/selectel-auth.json') -Raw -Encoding UTF8 | ConvertFrom-Json
}
function Assert-OtusValues($Values) {
    foreach ($property in (Get-OtusAuthSchema).fields.PSObject.Properties) {
        if ($Values[$property.Name] -isnot [string] -or [string]::IsNullOrWhiteSpace($Values[$property.Name])) {
            throw 'Все реквизиты должны быть заполнены строками.'
        }
    }
}
function Assert-OtusPrivateFile($Path) {
    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Ссылки для credentials запрещены.' }
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid.Value) { throw 'Другой владелец файла credentials.' }
    foreach ($rule in $acl.Access) {
        if ($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -ne $sid.Value) {
            throw 'Credentials должны быть доступны только текущему пользователю.'
        }
    }
}
function Get-OtusSession {
    $values = @{}
    foreach ($property in (Get-OtusAuthSchema).fields.PSObject.Properties) {
        $values[$property.Name] = [Environment]::GetEnvironmentVariable($property.Value.env, 'Process')
    }
    Assert-OtusValues $values
    return $values
}
function Save-OtusSession {
    $values = Get-OtusSession
    try {
        $folder = Join-Path $PSScriptRoot '../.local'
        if (Test-Path -LiteralPath $folder) {
            if ((Get-Item -LiteralPath $folder).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Ссылка вместо .local запрещена.' }
        } else { New-Item -ItemType Directory -Path $folder | Out-Null }
        $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $acl = [Security.AccessControl.DirectorySecurity]::new()
        $acl.SetOwner($sid)
        $acl.SetAccessRuleProtection($true, $false)
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
        # Persist only owner/access changes. Set-Acl may also attempt to write
        # the SACL on Windows PowerShell and require SeSecurityPrivilege.
        $directory = Get-Item -LiteralPath $folder -ErrorAction Stop
        if ($directory.PSObject.Methods['SetAccessControl']) {
            $directory.SetAccessControl($acl)
        } else {
            [IO.FileSystemAclExtensions]::SetAccessControl($directory, $acl)
        }
        $path = Join-Path $folder 'operator.json'
        if (Test-Path -LiteralPath $path) { Assert-OtusPrivateFile $path }
        $values | ConvertTo-Json | Set-Content -LiteralPath $path -Encoding UTF8 -ErrorAction Stop
        Assert-OtusPrivateFile $path
    } finally { $values.Clear() }
}
