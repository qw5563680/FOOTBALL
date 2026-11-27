#Requires -Version 5.1

[CmdletBinding()]
param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Run this script in Windows PowerShell on the collection machine.' }

$taskName = 'Football Collection Alarm'
$taskPath = '\'
$alarmDir = Join-Path $env:LOCALAPPDATA 'FootballCollectionAlarm'
$installedScript = Join-Path $alarmDir 'WatchdogAlarm.ps1'
$task = Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction SilentlyContinue

function Stop-InstalledTask {
    $null = Disable-ScheduledTask -TaskName $taskName -TaskPath $taskPath
    $state = (Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath).State
    if ($task.State -in 'Running', 'Queued' -or $state -in 'Running', 'Queued') {
        Stop-ScheduledTask -TaskName $taskName -TaskPath $taskPath
    }
}

if ($Uninstall) {
    if ($task) {
        Stop-InstalledTask
        Unregister-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Confirm:$false
    }
    if (Test-Path -LiteralPath $alarmDir) { Remove-Item -LiteralPath $alarmDir -Recurse -Force }
    Write-Output 'Football Collection Alarm removed.'
    return
}

$source = Join-Path $PSScriptRoot 'football-collection-alarm.ps1'
if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw 'The collection alarm script is missing.' }

$token = Read-Host -Prompt 'Paste the fine-grained GitHub token (input hidden)' -AsSecureString
try {
    if ($token.Length -eq 0) { throw 'The token must not be empty.' }
    # On Windows, omitting an encryption key selects DPAPI for the current user.
    $encrypted = ConvertFrom-SecureString -SecureString $token
} finally {
    $token.Dispose()
}

if ($task) { Stop-InstalledTask }
[void][IO.Directory]::CreateDirectory($alarmDir)
[IO.File]::WriteAllText((Join-Path $alarmDir 'token.dpapi'), $encrypted)
Copy-Item -LiteralPath $source -Destination $installedScript -Force

# Explicit UTC boundaries keep the same Beijing times on a host with any time zone.
$utcDay = ([datetimeoffset](Get-Date)).UtcDateTime.Date
$triggers = foreach ($minute in 185, 215, 485, 515, 605, 635) {
    $utc = $utcDay.AddMinutes($minute)
    $trigger = New-ScheduledTaskTrigger -Daily -At $utc.ToLocalTime()
    $trigger.StartBoundary = $utc.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", [cultureinfo]::InvariantCulture)
    $trigger
}
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File `"$installedScript`""
$action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments -WorkingDirectory $alarmDir
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 2)

$null = Register-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Action $action -Trigger $triggers `
    -Principal $principal -Settings $settings -Force
Write-Output 'Football Collection Alarm installed. Check the task and alarm.log after the next scheduled time.'
