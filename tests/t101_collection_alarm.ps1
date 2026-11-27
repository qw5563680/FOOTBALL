# Offline rehearsal. The current process substitutes Windows and HTTP commands.
$ErrorActionPreference = 'Stop'
$alarmPath = Join-Path $PSScriptRoot '../scripts/football-collection-alarm.ps1'
$installerPath = Join-Path $PSScriptRoot '../scripts/install-football-collection-alarm.ps1'
$alarmDir = Join-Path $env:LOCALAPPDATA 'FootballCollectionAlarm'
$tokenPath = Join-Path $alarmDir 'token.dpapi'
$logPath = Join-Path $alarmDir 'alarm.log'
$quotaPath = Join-Path $alarmDir 'quota.txt'
$env:OS = 'Windows_NT'
$env:SystemRoot = $env:LOCALAPPDATA
$env:USERNAME = 'Collector'
$env:USERDOMAIN = 'TestMachine'
$global:CollectionAlarmFixture = @{}
$global:CollectionAlarmFixture.now = [datetime]'2026-11-27T03:05:00Z'
$global:CollectionAlarmFixture.fakeToken = 'unit-test-marker-not-a-credential'
$global:CollectionAlarmFixture.sealedToken = 'dpapi-test-fixture'
$global:CollectionAlarmFixture.quotaPath = $quotaPath
$global:CollectionAlarmFixture.task = $null
$global:CollectionAlarmFixture.calls = [System.Collections.Generic.List[string]]::new()

function Assert-That($Condition, [string]$Reason) {
    if (-not $Condition) { throw $Reason }
}

function Get-Date { return $global:CollectionAlarmFixture.now }

function Read-Host([string]$Prompt, [switch]$AsSecureString) {
    Assert-That $AsSecureString 'Installer must hide the token prompt input.'
    return Microsoft.PowerShell.Security\ConvertTo-SecureString $global:CollectionAlarmFixture.fakeToken -AsPlainText -Force
}

function ConvertFrom-SecureString([securestring]$SecureString) {
    Assert-That ($PSBoundParameters.Count -eq 1 -and $args.Count -eq 0) 'Use keyless Windows DPAPI.'
    Assert-That ($SecureString.Length -eq $global:CollectionAlarmFixture.fakeToken.Length) 'Installer must encrypt prompt input.'
    return $global:CollectionAlarmFixture.sealedToken
}

function ConvertTo-SecureString([string]$String) {
    Assert-That ($PSBoundParameters.Count -eq 1 -and $args.Count -eq 0) 'Use keyless Windows DPAPI.'
    if ($String -cne $global:CollectionAlarmFixture.sealedToken) { throw "Unreadable $($global:CollectionAlarmFixture.fakeToken)" }
    return Microsoft.PowerShell.Security\ConvertTo-SecureString $global:CollectionAlarmFixture.fakeToken -AsPlainText -Force
}

class FixtureHttpException : System.Exception {
    [object]$Response

    FixtureHttpException([int]$Code, [string]$Message) : base($Message) {
        $this.Response = [pscustomobject]@{StatusCode = $Code}
    }
}

function Invoke-WebRequest {
    [CmdletBinding()]
    param(
        [string]$Uri, [string]$Method, [hashtable]$Headers, [string]$Body,
        [string]$ContentType, [string]$UserAgent, [int]$TimeoutSec,
        [int]$MaximumRedirection = -1, [switch]$UseBasicParsing
    )
    $Method = $Method.ToUpperInvariant()
    $global:CollectionAlarmFixture.calls.Add($Method)
    $workflow = 'https://api.github.com/repos/qw5563680/FOOTBALL/actions/workflows/football-watchdog.yml'
    if ($MaximumRedirection -ne 0 -or -not $UseBasicParsing -or $TimeoutSec -le 0) {
        throw 'Unsafe web request settings.'
    }
    if ($Headers.Authorization -cne "Bearer $($global:CollectionAlarmFixture.fakeToken)") { throw 'Missing Authorization header.' }
    if ($Method -ceq 'GET' -and $Uri -ceq "$workflow/runs?per_page=100") {
        if ($global:CollectionAlarmFixture.getFailure) { throw [FixtureHttpException]::new($global:CollectionAlarmFixture.getFailure, $global:CollectionAlarmFixture.fakeToken) }
        if ($global:CollectionAlarmFixture.networkFailure) { throw "Socket failed for $($global:CollectionAlarmFixture.fakeToken)" }
        return [pscustomobject]@{StatusCode = 200; Content = $global:CollectionAlarmFixture.recentRuns}
    }
    if ($Method -ceq 'POST' -and $Uri -ceq "$workflow/dispatches") {
        $request = $Body | ConvertFrom-Json
        if ($ContentType -cne 'application/json' -or $request.ref -cne 'main' -or
            $request.inputs.source -cne 'collection-machine-alarm') {
            throw 'Incorrect watchdog dispatch payload.'
        }
        if ($global:CollectionAlarmFixture.expectedReservation -and
            [IO.File]::ReadAllText($global:CollectionAlarmFixture.quotaPath) -cne $global:CollectionAlarmFixture.expectedReservation) {
            throw 'The attempt was not recorded before POST.'
        }
        if ($global:CollectionAlarmFixture.postFailure) { throw [FixtureHttpException]::new($global:CollectionAlarmFixture.postFailure, $global:CollectionAlarmFixture.fakeToken) }
        return [pscustomobject]@{StatusCode = 204}
    }
    throw 'Unexpected GitHub endpoint.'
}

function New-ScheduledTaskTrigger([switch]$Daily, [datetime]$At) {
    return [pscustomobject]@{Daily = $Daily.IsPresent; StartBoundary = $At.ToString('s')}
}

function New-ScheduledTaskAction([string]$Execute, [string]$Argument, [string]$WorkingDirectory) {
    return $PSBoundParameters
}

function New-ScheduledTaskPrincipal([string]$UserId, [string]$LogonType, [string]$RunLevel) {
    return $PSBoundParameters
}

function New-ScheduledTaskSettingsSet(
    [string]$MultipleInstances, [timespan]$ExecutionTimeLimit, [switch]$AllowStartIfOnBatteries,
    [switch]$DontStopIfGoingOnBatteries, [switch]$StartWhenAvailable
) {
    return $PSBoundParameters
}

function Register-ScheduledTask([string]$TaskName, $Trigger, $Action, $Principal, $Settings, [switch]$Force) {
    $global:CollectionAlarmFixture.calls.Add('Register')
    Assert-That ($TaskName -ceq 'Football Collection Alarm') 'Unexpected scheduled task name.'
    $global:CollectionAlarmFixture.task = [pscustomobject]@{
        State = 'Ready'; Trigger = $Trigger; Action = $Action; Principal = $Principal; Settings = $Settings
    }
    return $global:CollectionAlarmFixture.task
}

function Get-ScheduledTask {
    if ($global:CollectionAlarmFixture.task) { return $global:CollectionAlarmFixture.task.PSObject.Copy() }
}

function Disable-ScheduledTask {
    $global:CollectionAlarmFixture.calls.Add('Disable')
    if ($global:CollectionAlarmFixture.startsBeforeDisable) { $global:CollectionAlarmFixture.task.State = 'Running' }
    if ($global:CollectionAlarmFixture.task.State -ne 'Running') { $global:CollectionAlarmFixture.task.State = 'Disabled' }
    return $global:CollectionAlarmFixture.task
}

function Stop-ScheduledTask {
    $global:CollectionAlarmFixture.calls.Add('Stop')
    $global:CollectionAlarmFixture.task.State = 'Disabled'
}

function Unregister-ScheduledTask {
    $global:CollectionAlarmFixture.calls.Add('Unregister')
    $global:CollectionAlarmFixture.task = $null
}

function Invoke-AlarmCase([string]$Result, [string]$Calls, [int]$ExpectedLines) {
    $global:CollectionAlarmFixture.calls.Clear()
    $output = @(& $alarmPath *>&1)
    Assert-That ($output.Count -eq 0) 'Runtime must exit quietly on all paths.'
    Assert-That (($global:CollectionAlarmFixture.calls -join ',') -ceq $Calls) "Unexpected requests for $Result."
    $lines = @(Get-Content -LiteralPath $logPath)
    Assert-That ($lines.Length -eq $ExpectedLines) "Expected one log line for $Result."
    Assert-That ($lines[-1] -cmatch ('^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ ' + [regex]::Escape($Result) + '$')) "Unexpected log result for $Result."
    Assert-That (-not (($lines -join '') -clike "*$($global:CollectionAlarmFixture.fakeToken)*")) 'Runtime logged a credential.'
}

Assert-That ((Test-Path -LiteralPath $alarmPath) -and (Test-Path -LiteralPath $installerPath)) 'Collection alarm scripts are missing.'
$output = @(& $installerPath *>&1)
Assert-That (-not (($output -join '') -clike "*$($global:CollectionAlarmFixture.fakeToken)*")) 'Installer emitted the token.'
Assert-That ((Get-Content -Raw -LiteralPath $tokenPath) -ceq $global:CollectionAlarmFixture.sealedToken) 'Installer did not save DPAPI data.'
Assert-That ((Get-FileHash $alarmPath).Hash -ceq (Get-FileHash (Join-Path $alarmDir 'WatchdogAlarm.ps1')).Hash) 'Install a local script copy.'
Assert-That ($global:CollectionAlarmFixture.task.Principal.UserId -ceq 'TestMachine\Collector' -and
    $global:CollectionAlarmFixture.task.Principal.LogonType -ceq 'Interactive' -and $global:CollectionAlarmFixture.task.Principal.RunLevel -ceq 'Limited') 'Use the logged-on non-admin collector account.'
Assert-That ($global:CollectionAlarmFixture.task.Trigger.Count -eq 6 -and @($global:CollectionAlarmFixture.task.Trigger | Where-Object { -not $_.Daily }).Count -eq 0) 'Install six daily triggers.'
$times = $global:CollectionAlarmFixture.task.Trigger | ForEach-Object { $_.StartBoundary.Substring(11) }
Assert-That (($times -join ',') -ceq '03:05:00Z,03:35:00Z,08:05:00Z,08:35:00Z,10:05:00Z,10:35:00Z') 'Triggers must use the six explicit UTC boundaries.'
Assert-That ($global:CollectionAlarmFixture.task.Action.Execute -like '*powershell.exe' -and
    $global:CollectionAlarmFixture.task.Action.Argument -like '*-WindowStyle Hidden*"*WatchdogAlarm.ps1"*' -and
    $global:CollectionAlarmFixture.task.Action.WorkingDirectory -ceq $alarmDir) 'Task must run the installed script quietly.'
Assert-That ($global:CollectionAlarmFixture.task.Settings.MultipleInstances -ceq 'IgnoreNew' -and
    -not $global:CollectionAlarmFixture.task.Settings.StartWhenAvailable -and $global:CollectionAlarmFixture.task.Settings.AllowStartIfOnBatteries -and
    $global:CollectionAlarmFixture.task.Settings.DontStopIfGoingOnBatteries) 'Task must skip overlapping and missed triggers.'

$global:CollectionAlarmFixture.recentRuns = '{"total_count":1,"workflow_runs":[{"status":"completed","created_at":"2026-11-27T02:35:00Z","updated_at":"2026-11-27T02:44:59Z"}]}'
$global:CollectionAlarmFixture.getFailure = 0
$global:CollectionAlarmFixture.postFailure = 0
$global:CollectionAlarmFixture.networkFailure = $false
$global:CollectionAlarmFixture.expectedReservation = '2026-11-27:1'
Invoke-AlarmCase 'dispatched 204' 'GET,POST' 1
$global:CollectionAlarmFixture.expectedReservation = $null

$global:CollectionAlarmFixture.recentRuns = '{"total_count":1,"workflow_runs":[{"status":"completed","created_at":"2026-11-27T02:35:00Z","updated_at":"2026-11-27T02:45:00Z"}]}'
Invoke-AlarmCase 'recent_run 200' 'GET' 2
$global:CollectionAlarmFixture.recentRuns = '{"total_count":1,"workflow_runs":[{"status":"in_progress","created_at":"2026-11-27T02:35:00Z","updated_at":"2026-11-27T02:36:00Z"}]}'
Invoke-AlarmCase 'recent_run 200' 'GET' 3

$global:CollectionAlarmFixture.recentRuns = '{"total_count":0,"workflow_runs":[]}'
$global:CollectionAlarmFixture.postFailure = 503
4..8 | ForEach-Object { Invoke-AlarmCase 'request_failed 503' 'GET,POST' $_ }
Invoke-AlarmCase 'daily_limit -' '' 9
Assert-That ((Get-Content -Raw -LiteralPath $quotaPath) -ceq '2026-11-27:6') 'Failed dispatches must use quota.'

$global:CollectionAlarmFixture.now = [datetime]'2026-11-27T16:00:00Z'
$global:CollectionAlarmFixture.postFailure = 0
Invoke-AlarmCase 'dispatched 204' 'GET,POST' 10
Assert-That ((Get-Content -Raw -LiteralPath $quotaPath) -ceq '2026-11-28:1') 'Quota must roll over at Beijing midnight.'

$global:CollectionAlarmFixture.getFailure = 401
Invoke-AlarmCase 'request_failed 401' 'GET' 11
$global:CollectionAlarmFixture.getFailure = 302
Invoke-AlarmCase 'request_failed 302' 'GET' 12
$global:CollectionAlarmFixture.getFailure = 0
$global:CollectionAlarmFixture.networkFailure = $true
Invoke-AlarmCase 'request_failed -' 'GET' 13
$global:CollectionAlarmFixture.networkFailure = $false
$global:CollectionAlarmFixture.recentRuns = '{"total_count":1,"workflow_runs":[{}]}'
Invoke-AlarmCase 'invalid_response 200' 'GET' 14
Assert-That ((Get-Content -Raw -LiteralPath $quotaPath) -ceq '2026-11-28:1') 'Failed GETs must not use quota.'
[IO.File]::WriteAllText($quotaPath, 'corrupt state')
Invoke-AlarmCase 'local_error -' '' 15
[IO.File]::WriteAllText($quotaPath, '2026-11-28:1')

$global:CollectionAlarmFixture.calls.Clear()
$lockPath = Join-Path $alarmDir 'alarm.lock'
$lock = [IO.File]::Open($lockPath, 'Open', 'ReadWrite', 'None')
try {
    $output = @(& $alarmPath *>&1)
    Assert-That ($output.Count -eq 0 -and $global:CollectionAlarmFixture.calls.Count -eq 0) 'Overlapping alarm issued a request.'
    Assert-That (@(Get-Content -LiteralPath $logPath).Count -eq 15) 'Overlapping alarm changed the log.'
} finally {
    $lock.Dispose()
}

[IO.File]::WriteAllText($tokenPath, 'unreadable')
Invoke-AlarmCase 'unreadable_token -' '' 16
Remove-Item -LiteralPath $tokenPath
Invoke-AlarmCase 'missing_token -' '' 17
[IO.File]::WriteAllText($logPath, ('x' * 16384))
Invoke-AlarmCase 'missing_token -' '' 1
Assert-That ((Get-Item -LiteralPath $logPath).Length -lt 128) 'Oversized log should truncate before the next entry.'

$global:CollectionAlarmFixture.task.State = 'Running'
$global:CollectionAlarmFixture.calls.Clear()
$global:CollectionAlarmFixture.sealedToken = 'replacement-dpapi-fixture'
$null = & $installerPath
Assert-That (($global:CollectionAlarmFixture.calls -join ',') -ceq 'Disable,Stop,Register') 'Reinstall should replace one stopped task.'
Assert-That ((Get-Content -Raw -LiteralPath $tokenPath) -ceq $global:CollectionAlarmFixture.sealedToken) 'Reinstall did not replace the token.'
Assert-That ((Get-Content -Raw -LiteralPath $quotaPath) -ceq '2026-11-28:1') 'Reinstall reset the daily quota.'
Assert-That (Test-Path -LiteralPath $logPath) 'Reinstall removed the log.'

$global:CollectionAlarmFixture.task.State = 'Ready'
$global:CollectionAlarmFixture.startsBeforeDisable = $true
$global:CollectionAlarmFixture.calls.Clear()
$null = & $installerPath -Uninstall
Assert-That (($global:CollectionAlarmFixture.calls -join ',') -ceq 'Disable,Stop,Unregister') 'Uninstall should stop the task before unregistering.'
Assert-That ($null -eq $global:CollectionAlarmFixture.task -and -not (Test-Path -LiteralPath $alarmDir)) 'Uninstall left a task, token, log, or other installed files.'
$null = & $installerPath -Uninstall
Assert-That (-not (Test-Path -LiteralPath $alarmDir)) 'Repeated uninstall should keep the installation absent.'
Write-Output 'Offline collection alarm checks passed.'
