# Installs the save watcher for the current Windows user: hidden start at logon, restart after a crash,
# popups under the name "Dusklight Save Sync", Start menu shortcuts. No administrator rights are needed.
# Usage: powershell -ExecutionPolicy Bypass -File install-windows.ps1 [-SyncHome <folder>] [-Uninstall]
param(
    [string]$SyncHome = (Join-Path $env:LOCALAPPDATA 'DusklightSaveSync'),
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'
$code = $PSScriptRoot
$task = 'Dusklight Save Sync'
$menu = Join-Path ([Environment]::GetFolderPath('Programs')) 'Dusklight Save Sync'
$appKey = 'HKCU:\Software\Classes\AppUserModelId\DusklightSaveSync'

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object { $_.CommandLine -like '*save_watcher.py*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Remove-Item $menu -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $appKey -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "Removed. Saves, config and backups stay in $SyncHome"
    exit 0
}

$pythonw = (Get-Command pythonw.exe -ErrorAction Stop).Source
$python = (Get-Command python.exe -ErrorAction Stop).Source
if (-not (Test-Path (Join-Path $SyncHome 'config.json'))) {
    throw "Create $SyncHome\config.json first (see config.windows.example.json)."
}
& gh auth status --hostname github.com *> $null
if ($LASTEXITCODE -ne 0) { Write-Warning 'GitHub CLI is not logged in: run "gh auth login" once. Saves stay local until then.' }

# Popups show this name; the icon is taken from Dusklight when the config names its exe.
New-Item $appKey -Force | Out-Null
Set-ItemProperty $appKey -Name DisplayName -Value 'Dusklight Save Sync'
$config = Get-Content (Join-Path $SyncHome 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($config.launcher -and (Test-Path $config.launcher.game_exe)) {
    Add-Type -AssemblyName System.Drawing
    $icon = Join-Path $SyncHome 'dusklight.png'
    [System.Drawing.Icon]::ExtractAssociatedIcon($config.launcher.game_exe).ToBitmap().Save($icon, [System.Drawing.Imaging.ImageFormat]::Png)
    Set-ItemProperty $appKey -Name IconUri -Value $icon
}

# Logon start plus a 1-minute repetition: a crashed watcher is back within a minute; a running one is kept.
$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$code\save_watcher.py`" --home `"$SyncHome`"" -WorkingDirectory $code
# A logon repetition only begins at the next logon, so a second "from now" trigger covers the current session.
$every = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1)).Repetition
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user
$logon.Repetition = $every
$now = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$trigger = @($logon, $now)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'Two-way GitHub sync of Dusklight GCI saves (hidden, independent of Steam and the launcher).' -Force | Out-Null
Start-ScheduledTask -TaskName $task

# Start menu: state, sync now, popup test, launcher.
New-Item $menu -ItemType Directory -Force | Out-Null
$shell = New-Object -ComObject WScript.Shell
function Shortcut($name, $arguments, $description) {
    $link = $shell.CreateShortcut((Join-Path $menu "$name.lnk"))
    $link.TargetPath = "$env:SystemRoot\System32\cmd.exe"
    $link.Arguments = $arguments
    $link.WorkingDirectory = $code
    $link.Description = $description
    if ($config.launcher) { $link.IconLocation = "$($config.launcher.game_exe),0" }
    $link.Save()
}
$run = "`"$python`" `"$code\save_watcher.py`" --home `"$SyncHome`""
Shortcut 'Стан синхронізації сейвів' "/c `"$run --status & pause`"" 'Show the save sync state'
Shortcut 'Синхронізувати сейви зараз' "/c `"$run --sync-now & timeout /t 3`"" 'Ask the watcher for an immediate pass'
Shortcut 'Тест сповіщення' "/c `"$run --test-notification & timeout /t 3`"" 'Show a test popup; saves are not changed'
if ($config.launcher) {
    $launch = "/c `"`"$python`" `"$code\launcher.py`" --home `"$SyncHome`" || pause`""
    Shortcut 'Twilight Princess UA' $launch 'Update the Ukrainian translation, sync saves, start Dusklight'
    Copy-Item (Join-Path $menu 'Twilight Princess UA.lnk') ([Environment]::GetFolderPath('Desktop')) -Force
}
Write-Host "Installed. Task: '$task'. Shortcuts: $menu. Runtime folder: $SyncHome"
