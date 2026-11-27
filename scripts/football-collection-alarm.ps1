#Requires -Version 5.1

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$VerbosePreference = 'SilentlyContinue'
$DebugPreference = 'SilentlyContinue'

$alarmDir = Join-Path $env:LOCALAPPDATA 'FootballCollectionAlarm'
$invariant = [cultureinfo]::InvariantCulture
$beijingOffset = [timespan]::FromHours(8)

function Get-BeijingDay {
    return ([datetimeoffset](Get-Date)).ToOffset($beijingOffset).ToString('yyyy-MM-dd', $invariant)
}

function Get-AttemptCount([string]$Day) {
    if (-not [IO.File]::Exists($quotaPath)) { return 0 }
    $saved = [IO.File]::ReadAllText($quotaPath)
    if ($saved -cnotmatch '\A(\d{4}-\d{2}-\d{2}):([1-6])\z') { throw 'Invalid quota state.' }
    $comparison = [string]::CompareOrdinal($Matches[1], $Day)
    if ($comparison -gt 0) { throw 'Quota is from a future day.' }
    if ($comparison -lt 0) { return 0 }
    return [int]$Matches[2]
}

function Save-AttemptCount([string]$Day, [int]$Count) {
    $pending = "$quotaPath.pending"
    $data = [Text.Encoding]::ASCII.GetBytes("${Day}:$Count")
    $stream = [IO.File]::Open($pending, 'Create', 'Write', 'None')
    try {
        $stream.Write($data, 0, $data.Length)
        $stream.Flush($true)
    } finally {
        $stream.Dispose()
    }
    if ([IO.File]::Exists($quotaPath)) {
        [IO.File]::Replace($pending, $quotaPath, [NullString]::Value)
    } else {
        [IO.File]::Move($pending, $quotaPath)
    }
}

function Get-RequestFailure($Failure) {
    $code = [int]$Failure.Exception.Response.StatusCode
    if ($code -ge 100 -and $code -le 599) { return "request_failed $code" }
    return 'request_failed -'
}

function Invoke-CollectionAlarm {
    $tokenPath = Join-Path $alarmDir 'token.dpapi'
    if (-not [IO.File]::Exists($tokenPath)) { return 'missing_token -' }
    try {
        # Keyless SecureString conversion uses Windows DPAPI for the current user.
        $secureToken = ConvertTo-SecureString -String ([IO.File]::ReadAllText($tokenPath))
        try {
            $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
            try {
                $headers = @{
                    Authorization = 'Bearer ' + [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
                    Accept = 'application/vnd.github+json'
                    'X-GitHub-Api-Version' = '2022-11-28'
                }
            } finally {
                [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
            }
        } finally {
            $secureToken.Dispose()
        }
    } catch {
        return 'unreadable_token -'
    }

    $day = Get-BeijingDay
    if ((Get-AttemptCount $day) -ge 6) { return 'daily_limit -' }

    # Use only the fixed API endpoints and never follow redirects.
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $request = @{
        Headers = $headers
        UserAgent = 'Football-Collection-Alarm'
        TimeoutSec = 15
        MaximumRedirection = 0
        UseBasicParsing = $true
    }
    try {
        $response = Invoke-WebRequest @request -Method Get -Uri 'https://api.github.com/repos/qw5563680/FOOTBALL/actions/workflows/football-watchdog.yml/runs?per_page=100'
    } catch {
        return (Get-RequestFailure $_)
    }
    if ($response.StatusCode -ne 200) { return "request_failed $([int]$response.StatusCode)" }

    try {
        $runs = (ConvertFrom-Json -InputObject $response.Content).workflow_runs
        if ($runs -isnot [array]) { return 'invalid_response 200' }
        $cutoff = ([datetimeoffset](Get-Date)).AddMinutes(-20)
        foreach ($run in $runs) {
            if ([string]::IsNullOrEmpty($run.status) -or $null -eq $run.updated_at) {
                return 'invalid_response 200'
            }
            $updated = [datetimeoffset]$run.updated_at
            if ($run.status -cne 'completed' -or $updated -ge $cutoff) { return 'recent_run 200' }
        }
    } catch {
        return 'invalid_response 200'
    }

    # Reserve the attempt before POST. A timeout or crash cannot refund a sent request.
    $day = Get-BeijingDay
    $count = Get-AttemptCount $day
    if ($count -ge 6) { return 'daily_limit -' }
    Save-AttemptCount $day ($count + 1)
    try {
        $response = Invoke-WebRequest @request -Method Post -Uri 'https://api.github.com/repos/qw5563680/FOOTBALL/actions/workflows/football-watchdog.yml/dispatches' `
            -ContentType 'application/json' -Body '{"ref":"main","inputs":{"source":"collection-machine-alarm"}}'
    } catch {
        return (Get-RequestFailure $_)
    }
    if ($response.StatusCode -eq 204) { return 'dispatched 204' }
    return "request_failed $([int]$response.StatusCode)"
}

try {
    [void][IO.Directory]::CreateDirectory($alarmDir)
    $quotaPath = Join-Path $alarmDir 'quota.txt'
    $logPath = Join-Path $alarmDir 'alarm.log'
    $lock = [IO.File]::Open((Join-Path $alarmDir 'alarm.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
} catch {
    # The scheduler already ignores overlaps. An exclusive lock also covers direct launches.
    return
}

try {
    try {
        $result = Invoke-CollectionAlarm
    } catch {
        $result = 'local_error -'
    }
    $time = ([datetimeoffset](Get-Date)).UtcDateTime.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", $invariant)
    $line = "$time $result$([Environment]::NewLine)"
    if ([IO.File]::Exists($logPath) -and ([IO.FileInfo]$logPath).Length + $line.Length -gt 16384) {
        [IO.File]::WriteAllText($logPath, $line, [Text.Encoding]::ASCII)
    } else {
        [IO.File]::AppendAllText($logPath, $line, [Text.Encoding]::ASCII)
    }
} catch {
    # A local logging failure must not print exception or credential details.
} finally {
    $lock.Dispose()
}
