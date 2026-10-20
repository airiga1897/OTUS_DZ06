# Основной режим: окружение текущей сессии. -SaveLocal — явный режим разработки.
param([switch]$SaveLocal)
$settings = Get-Content -LiteralPath (Join-Path $PSScriptRoot '../config/selectel-auth.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$values = @{auth_url=$settings.auth_url; region=$settings.region}
foreach ($property in $settings.fields.PSObject.Properties) {
    $field = $property.Value
    if ($field.secret) {
        $secure = Read-Host $field.prompt -AsSecureString
        if ($secure.Length -eq 0) { $secure.Dispose(); throw 'Пароль пуст.' }
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $value = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer); $secure.Dispose() }
    } else {
        $value = Read-Host $field.prompt
        if ([string]::IsNullOrWhiteSpace($value)) { throw 'Все реквизиты должны быть заполнены.' }
    }
    $values[$property.Name] = $value
    Set-Item -Path ('Env:' + $field.env) -Value $value
}
$env:OS_AUTH_URL = $settings.auth_url
$env:OS_REGION_NAME = $settings.region
Remove-Item Env:OTUS_SELECTEL_AUTH -ErrorAction SilentlyContinue
if ($SaveLocal) {
    $folder = Join-Path $PSScriptRoot '../.local'
    New-Item -ItemType Directory -Path $folder -Force | Out-Null
    $path = Join-Path $folder 'operator.json'
    New-Item -ItemType File -Path $path -Force | Out-Null
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = New-Object Security.AccessControl.FileSecurity
    $acl.SetOwner($sid)
    $acl.SetAccessRuleProtection($true, $false)
    $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'Allow')
    $acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $path -AclObject $acl
    $values | ConvertTo-Json | Set-Content -LiteralPath $path -Encoding UTF8
    $env:OTUS_SELECTEL_AUTH = (Resolve-Path $path).Path
    Write-Host 'Выбран локальный режим разработки. После работы удалите файл реквизитов.'
} else {
    Write-Host 'Реквизиты загружены в окружение текущей сессии; файл не создавался.'
}
Remove-Variable values,value,secure,pointer -ErrorAction SilentlyContinue
