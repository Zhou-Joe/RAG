# Run from a normal PowerShell session on the Windows app host after updating source:
#   & .\scripts\deploy_windows.ps1
# Restarts only this checkout's web app and schedulers. Leaves MinerU and FRP running.
# No Git operations are performed. Database backups are retained in output\backups.
# Requires CIM/process inspection and permission to stop this checkout's processes.
#Requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateRange(1, 30)]
    [int]$HealthTimeoutSeconds = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$taskRepoRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$taskPythonPath = Join-Path $taskRepoRoot '.venv\Scripts\python.exe'
$taskManagePath = Join-Path $taskRepoRoot 'manage.py'
$taskDatabasePath = Join-Path $taskRepoRoot 'db.sqlite3'
$taskOutputPath = Join-Path $taskRepoRoot 'output'
$taskBackupDirectory = Join-Path $taskOutputPath 'backups'
$taskLogDirectory = Join-Path $taskRepoRoot 'logs'
$taskRunStamp = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
$taskBackupPath = $null
$taskDeployLock = $null
$taskStoppedRunners = [Collections.Generic.List[object]]::new()
$taskNewHandles = [Collections.Generic.List[object]]::new()

foreach ($taskRequiredPath in @($taskPythonPath, $taskManagePath)) {
    if (-not (Test-Path -LiteralPath $taskRequiredPath -PathType Leaf)) {
        throw "Required deployment file is missing: $taskRequiredPath"
    }
}
$taskPythonPath = (Resolve-Path -LiteralPath $taskPythonPath).Path
$taskManagePath = (Resolve-Path -LiteralPath $taskManagePath).Path
$taskBasePythonPath = $taskPythonPath

function Test-SamePath([string]$Left, [string]$Right) {
    if (-not $Left -or -not $Right) { return $false }
    try {
        return [StringComparer]::OrdinalIgnoreCase.Equals(
            [IO.Path]::GetFullPath($Left), [IO.Path]::GetFullPath($Right))
    } catch { return $false }
}

function Get-BasePythonPath {
    $taskBasePaths = @(& $taskPythonPath -c 'import sys; print(sys._base_executable or sys.executable)')
    if ($LASTEXITCODE -ne 0 -or $taskBasePaths.Count -ne 1 -or
            -not (Test-Path -LiteralPath ([string]$taskBasePaths[0]) -PathType Leaf)) {
        throw 'Could not identify the virtual environment base Python; no processes were stopped.'
    }
    return (Resolve-Path -LiteralPath ([string]$taskBasePaths[0])).Path
}

function Get-VerifiedRunner($Process, [string]$ExpectedPythonPath = $taskPythonPath) {
    if ($null -eq $Process -or -not (Test-SamePath ([string]$Process.ExecutablePath) $ExpectedPythonPath)) {
        return
    }
    # Accept the existing launcher's bytecode, buffering and UTF-8 options.
    # Keep Python options case-sensitive: -X and -x have different meanings.
    # A -c/-m wrapper containing a script name must not be treated as manage.py.
    $taskMatch = [regex]::Match([string]$Process.CommandLine,
        '^(?:"[^"]+"|\S+)\s+(?<python>(?:(?:-B|-u|-X\s+utf8)\s+)*)(?:"(?<manage>[^"]+)"|(?<manage>\S+))\s+(?<mode>runserver|run_feed_scheduler|run_report_scheduler)(?:\s+(?<args>.*?))?\s*$')
    if (-not $taskMatch.Success) { return }
    $taskManageArgument = $taskMatch.Groups['manage'].Value
    # Existing start_all.bat uses manage.py with this checkout's private Python.
    # Other relative script paths and scripts outside this checkout are rejected.
    if ($taskManageArgument -notin @('manage.py', '.\manage.py') -and
        -not (Test-SamePath $taskManageArgument $taskManagePath)) { return }
    $taskMode = $taskMatch.Groups['mode'].Value.ToLowerInvariant()
    $taskArgumentText = $taskMatch.Groups['args'].Value.Trim()
    if ($taskArgumentText -match '["''\r\n]') { return }
    [string[]]$taskRunnerArguments = @()
    if ($taskArgumentText) { $taskRunnerArguments = @($taskArgumentText -split '\s+') }
    [string[]]$taskPythonArguments = @()
    $taskPythonArgumentText = $taskMatch.Groups['python'].Value.Trim()
    if ($taskPythonArgumentText) { $taskPythonArguments = @($taskPythonArgumentText -split '\s+') }

    if ($taskMode -eq 'runserver') {
        $taskAddressSeen = $false
        foreach ($taskArgument in $taskRunnerArguments) {
            if ($taskArgument -in @('--noreload', '--nothreading', '--insecure')) { continue }
            if (-not $taskAddressSeen -and $taskArgument -match '^(?:(?:127\.0\.0\.1|localhost|0\.0\.0\.0):)?8000$') {
                $taskAddressSeen = $true
                continue
            }
            return
        }
        # No explicit address means Django's default localhost:8000.
    } elseif ($taskArgumentText -and $taskArgumentText -notmatch '^--interval(?:=|\s+)(?:[5-9]|[1-5][0-9]|60)$') {
        return
    }

    return [PSCustomObject]@{
        ProcessId = [int]$Process.ProcessId
        LauncherProcessId = [int]$Process.ProcessId
        CreationDate = $Process.CreationDate
        ExecutablePath = [string]$Process.ExecutablePath
        CommandLine = [string]$Process.CommandLine
        ManageArgument = $taskManageArgument
        PythonArguments = $taskPythonArguments
        Mode = $taskMode
        Arguments = $taskRunnerArguments
    }
}

function Get-VerifiedRunners([object[]]$Processes) {
    $taskLaunchers = @($Processes | ForEach-Object {
        $taskCandidate = Get-VerifiedRunner $_
        if ($null -ne $taskCandidate) { $taskCandidate }
    })
    foreach ($taskLauncher in $taskLaunchers) {
        $taskLauncher
        if (Test-SamePath $taskBasePythonPath $taskPythonPath) { continue }
        foreach ($taskProcess in $Processes) {
            # Windows venv python.exe launches the base interpreter as a child.
            # The shared base interpreter alone does not establish ownership.
            if ($null -eq $taskProcess -or [int]$taskProcess.ParentProcessId -ne $taskLauncher.ProcessId -or
                    $taskProcess.CreationDate -lt $taskLauncher.CreationDate) { continue }
            $taskChild = Get-VerifiedRunner $taskProcess -ExpectedPythonPath $taskBasePythonPath
            if ($null -ne $taskChild -and $taskChild.Mode -ceq $taskLauncher.Mode -and
                    ($taskChild.Arguments -join ' ') -ceq ($taskLauncher.Arguments -join ' ') -and
                    ($taskChild.PythonArguments -join ' ') -ceq ($taskLauncher.PythonArguments -join ' ')) {
                $taskChild.LauncherProcessId = $taskLauncher.ProcessId
                $taskChild
            }
        }
    }
}

function Test-RunnerIdentity($Process, $Runner) {
    return ($null -ne $Process -and $null -ne $Runner -and
        $Process.CreationDate -eq $Runner.CreationDate -and
        (Test-SamePath ([string]$Process.ExecutablePath) $Runner.ExecutablePath) -and
        [string]$Process.CommandLine -ceq $Runner.CommandLine)
}

function Get-AppListeners {
    # Enumerate successfully before filtering; an inspection failure must not look like a free port.
    return @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Where-Object { $_.LocalPort -eq 8000 })
}

function Get-DeploymentRunners {
    # Rejected processes must emit no pipeline item. An explicit $null would be
    # retained in @(...), then property access would fail under strict mode.
    $taskProcesses = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $taskRunners = @(Get-VerifiedRunners -Processes $taskProcesses)
    foreach ($taskListener in @(Get-AppListeners)) {
        $taskOwner = @($taskRunners | Where-Object { $_.ProcessId -eq $taskListener.OwningProcess -and $_.Mode -eq 'runserver' })
        if (-not $taskOwner.Count) {
            throw "Port 8000 belongs to an unverified process (PID $($taskListener.OwningProcess)); no processes were stopped."
        }
    }
    foreach ($taskProcess in $taskProcesses) {
        $taskUsesOwnPython = Test-SamePath ([string]$taskProcess.ExecutablePath) $taskPythonPath
        if (-not $taskUsesOwnPython -and (Test-SamePath ([string]$taskProcess.ExecutablePath) $taskBasePythonPath)) {
            $taskLinkedParent = @($taskRunners | Where-Object {
                $_.ProcessId -eq $_.LauncherProcessId -and $_.ProcessId -eq $taskProcess.ParentProcessId
            })
            $taskBaseCandidate = Get-VerifiedRunner $taskProcess -ExpectedPythonPath $taskBasePythonPath
            $taskUsesOwnPython = $taskLinkedParent.Count -gt 0 -or
                ($null -ne $taskBaseCandidate -and (Test-SamePath $taskBaseCandidate.ManageArgument $taskManagePath))
        }
        if ($taskUsesOwnPython -and
            [string]$taskProcess.CommandLine -match '\b(?:run_feed_scheduler|run_report_scheduler)\b' -and
            -not (@($taskRunners | Where-Object { $_.ProcessId -eq $taskProcess.ProcessId }).Count)) {
            throw "An unrecognized scheduler uses this checkout's Python (PID $($taskProcess.ProcessId)); refusing to start a duplicate."
        }
    }
    return $taskRunners
}

function Stop-VerifiedRunner($Runner) {
    $taskCurrent = Get-CimInstance Win32_Process -Filter ("ProcessId = " + $Runner.ProcessId) -ErrorAction Stop
    if ($null -eq $taskCurrent) { return }
    if (-not (Test-RunnerIdentity $taskCurrent $Runner)) {
        throw "Process identity changed for PID $($Runner.ProcessId); refusing to stop it."
    }
    Stop-Process -Id $Runner.ProcessId -ErrorAction Stop
    try { Wait-Process -Id $Runner.ProcessId -Timeout 10 -ErrorAction Stop }
    catch {
        if (Get-Process -Id $Runner.ProcessId -ErrorAction SilentlyContinue) { throw }
    }
}

function Start-RagRunner([string]$Mode, [string[]]$RunnerArguments, [string]$LogSuffix,
        [string[]]$PythonArguments = @('-u', '-X', 'utf8')) {
    $taskLaunchArguments = @($PythonArguments) + @(('"' + $taskManagePath + '"'), $Mode) + @($RunnerArguments)
    $taskOutLog = Join-Path $taskLogDirectory ("$taskRunStamp-$LogSuffix.out.log")
    $taskErrorLog = Join-Path $taskLogDirectory ("$taskRunStamp-$LogSuffix.err.log")
    return Start-Process -FilePath $taskPythonPath -ArgumentList $taskLaunchArguments `
        -WorkingDirectory $taskRepoRoot -WindowStyle Hidden -RedirectStandardOutput $taskOutLog `
        -RedirectStandardError $taskErrorLog -PassThru -ErrorAction Stop
}

function Register-NewRunner($Handle, [string]$Mode) {
    $taskNewEntry = [PSCustomObject]@{ Handle = $Handle; Runner = $null }
    $taskNewHandles.Add($taskNewEntry)
    for ($taskAttempt = 0; $taskAttempt -lt 10; $taskAttempt++) {
        $taskNewProcesses = @(Get-CimInstance Win32_Process -ErrorAction Stop)
        $taskOwned = @(Get-VerifiedRunners -Processes $taskNewProcesses |
            Where-Object { $_.LauncherProcessId -eq $Handle.Id -and $_.Mode -eq $Mode })
        $taskVerified = @($taskOwned | Where-Object { $_.ProcessId -eq $Handle.Id })
        if ($taskVerified.Count) { $taskNewEntry.Runner = $taskVerified[0] }
        $taskChildren = @($taskOwned | Where-Object { $_.ProcessId -ne $Handle.Id })
        if ($taskVerified.Count -and ((Test-SamePath $taskBasePythonPath $taskPythonPath) -or $taskChildren.Count)) {
            foreach ($taskChild in $taskChildren) {
                $taskNewHandles.Add([PSCustomObject]@{ Handle = $Handle; Runner = $taskChild })
            }
            return
        }
        Start-Sleep -Milliseconds 100
    }
    throw "Could not verify the newly launched $Mode (PID $($Handle.Id))."
}

function Invoke-RagManage([string[]]$Arguments) {
    & $taskPythonPath $taskManagePath @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Django command '$($Arguments[0])' failed (exit $LASTEXITCODE)." }
}

function Restore-PreviousRunners {
    if (-not $taskStoppedRunners.Count) { return }
    Write-Warning 'Restarting previously running commands using the current source. The database is not rolled back.'
    $taskRestoredCommands = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($taskRunner in @($taskStoppedRunners | Sort-Object { if ($_.Mode -eq 'runserver') { 0 } else { 1 } })) {
        $taskRestoreKey = $taskRunner.Mode + ' ' + ($taskRunner.Arguments -join ' ')
        if (-not $taskRestoredCommands.Add($taskRestoreKey)) { continue }
        try {
            $taskSameRunner = @(Get-CimInstance Win32_Process -ErrorAction Stop | ForEach-Object {
                $taskCandidate = Get-VerifiedRunner $_
                if ($taskCandidate -and $taskCandidate.Mode -eq $taskRunner.Mode -and
                    ($taskCandidate.Arguments -join ' ') -ceq ($taskRunner.Arguments -join ' ')) { $taskCandidate }
            })
            if ($taskSameRunner.Count) { continue }
            if ($taskRunner.Mode -eq 'runserver' -and @(Get-AppListeners).Count) {
                Write-Warning 'Port 8000 is occupied; the previous web command was not restarted.'
                continue
            }
            $null = Start-RagRunner -Mode $taskRunner.Mode -RunnerArguments $taskRunner.Arguments `
                -PythonArguments $taskRunner.PythonArguments -LogSuffix ('restore-' + $taskRunner.ProcessId)
        } catch { Write-Warning "Could not restore $($taskRunner.Mode). Inspect deployment logs and start that command manually." }
    }
}

try {
    $null = New-Item -ItemType Directory -Path $taskOutputPath -Force
    $taskDeployLock = [IO.File]::Open((Join-Path $taskOutputPath 'deploy-windows.lock'),
        [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)

    # Complete all process/port inspection before stopping anything.
    $taskBasePythonPath = Get-BasePythonPath
    $taskRunners = @(Get-DeploymentRunners)
    $null = New-Item -ItemType Directory -Path $taskBackupDirectory -Force
    $null = New-Item -ItemType Directory -Path $taskLogDirectory -Force
    foreach ($taskRunner in @($taskRunners | Sort-Object @{ Expression = { if ($_.Mode -eq 'runserver') { 1 } else { 0 } } },
            @{ Expression = { if ($_.ProcessId -ne $_.LauncherProcessId) { 0 } else { 1 } } })) {
        Write-Host "Stopping verified $($taskRunner.Mode), PID $($taskRunner.ProcessId)."
        $taskStoppedRunners.Add($taskRunner)
        Stop-VerifiedRunner $taskRunner
    }
    if (@(Get-AppListeners).Count) { throw 'Port 8000 is still occupied; refusing to launch another app.' }

    if (Test-Path -LiteralPath $taskDatabasePath -PathType Leaf) {
        $taskBackupPath = Join-Path $taskBackupDirectory ("rag-db-$taskRunStamp.sqlite3")
        $taskBackupCode = @'
import pathlib, sqlite3, sys
source = sqlite3.connect(pathlib.Path(sys.argv[1]).resolve().as_uri() + '?mode=ro', uri=True)
try:
    destination = sqlite3.connect(sys.argv[2])
    try:
        source.backup(destination)
    finally:
        destination.close()
finally:
    source.close()
'@
        & $taskPythonPath -c $taskBackupCode $taskDatabasePath $taskBackupPath
        if ($LASTEXITCODE -ne 0) { throw 'SQLite backup failed; migrations were not started.' }
        Write-Host "Database backup: $taskBackupPath"
    }
    Invoke-RagManage -Arguments @('migrate', '--noinput')
    Invoke-RagManage -Arguments @('collectstatic', '--noinput')
    Invoke-RagManage -Arguments @('check')

    $taskApp = Start-RagRunner -Mode 'runserver' -RunnerArguments @('127.0.0.1:8000', '--noreload') -LogSuffix 'web'
    Register-NewRunner $taskApp 'runserver'
    $taskDeadline = [DateTime]::UtcNow.AddSeconds($HealthTimeoutSeconds)
    $taskHealthy = $false
    $taskHealthError = 'The app did not respond.'
    while ([DateTime]::UtcNow -lt $taskDeadline) {
        try {
            $taskResponse = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/accounts/login/' `
                -UseBasicParsing -MaximumRedirection 0 -TimeoutSec 1 -ErrorAction Stop
            if ($taskResponse.StatusCode -eq 200) {
                $taskHealthListeners = @(Get-AppListeners)
                if (-not $taskHealthListeners.Count) { throw 'The responding app has no inspectable port listener.' }
                foreach ($taskListener in $taskHealthListeners) {
                    $taskOwnerProcess = Get-CimInstance Win32_Process -Filter ("ProcessId = " + $taskListener.OwningProcess) -ErrorAction Stop
                    $taskExpectedOwner = @($taskNewHandles | Where-Object {
                        $null -ne $_.Runner -and $_.Runner.Mode -eq 'runserver' -and
                            $_.Runner.ProcessId -eq $taskListener.OwningProcess
                    })
                    if (-not $taskExpectedOwner.Count -or
                            -not (Test-RunnerIdentity $taskOwnerProcess $taskExpectedOwner[0].Runner)) {
                        throw 'The responding port is not owned by the newly launched app.'
                    }
                }
                $taskHealthy = $true
                break
            }
        } catch { $taskHealthError = $_.Exception.Message }
        $taskRemaining = ($taskDeadline - [DateTime]::UtcNow).TotalMilliseconds
        if ($taskRemaining -gt 0) { Start-Sleep -Milliseconds ([int][Math]::Min(1000, $taskRemaining)) }
    }
    if (-not $taskHealthy) { throw "Local health check failed after $HealthTimeoutSeconds seconds. $taskHealthError See $taskLogDirectory." }

    $taskScheduler = Start-RagRunner -Mode 'run_feed_scheduler' -RunnerArguments @('--interval', '30') -LogSuffix 'scheduler'
    Register-NewRunner $taskScheduler 'run_feed_scheduler'
    Start-Sleep -Seconds 1
    $taskScheduler.Refresh()
    if ($taskScheduler.HasExited) { throw 'The scheduler exited during startup. Inspect the scheduler logs.' }
    Write-Host 'Deployment complete. Local health verified: http://127.0.0.1:8000/accounts/login/'
    Write-Host 'Public access uses the existing FRP configuration; the public entry was not remotely checked.'
    Write-Host "Web and scheduler logs: $taskLogDirectory"
} catch {
    $taskFailure = $_
    foreach ($taskEntry in @($taskNewHandles | Sort-Object {
        if ($null -ne $_.Runner -and $_.Runner.ProcessId -ne $_.Runner.LauncherProcessId) { 0 } else { 1 }
    })) {
        try {
            if ($null -ne $taskEntry.Runner) { Stop-VerifiedRunner $taskEntry.Runner }
            elseif (Get-Process -Id $taskEntry.Handle.Id -ErrorAction SilentlyContinue) {
                Write-Warning "The new PID $($taskEntry.Handle.Id) could not be verified; inspect it before retrying deployment."
            }
        } catch { Write-Warning "Could not stop new PID $($taskEntry.Handle.Id); inspect it before retrying deployment." }
    }
    Restore-PreviousRunners
    if ($taskBackupPath) { Write-Warning "The SQLite backup remains available at $taskBackupPath" }
    throw $taskFailure
} finally {
    if ($null -ne $taskDeployLock) { $taskDeployLock.Dispose() }
}
