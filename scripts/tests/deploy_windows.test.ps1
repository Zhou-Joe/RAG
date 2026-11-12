# Standalone regression checks; no deployment or real process operations run.
# Run with Windows PowerShell 5.1 or PowerShell 7:
#   powershell.exe -NoProfile -File scripts\tests\deploy_windows.test.ps1
#Requires -Version 5.1
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$taskScriptPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'deploy_windows.ps1'
$taskTokens = $null
$taskParseErrors = $null
$taskAst = [Management.Automation.Language.Parser]::ParseFile(
    $taskScriptPath, [ref]$taskTokens, [ref]$taskParseErrors)
if ($taskParseErrors.Count) { throw ($taskParseErrors | Out-String) }

# Load only production inspection functions, never the deployment entry point.
foreach ($taskFunctionName in @('Test-SamePath', 'Get-VerifiedRunner', 'Get-AppListeners', 'Get-DeploymentRunners',
        'Get-VerifiedRunners', 'Test-RunnerIdentity', 'Register-NewRunner', 'Stop-VerifiedRunner',
        'Start-RagRunner', 'Restore-PreviousRunners')) {
    $taskDefinition = $taskAst.Find({
        param($Node)
        $Node -is [Management.Automation.Language.FunctionDefinitionAst] -and $Node.Name -eq $taskFunctionName
    }, $false)
    if ($null -eq $taskDefinition) { throw "Missing function: $taskFunctionName" }
    . ([ScriptBlock]::Create($taskDefinition.Extent.Text))
}

$taskPythonPath = 'C:\RAG test\.venv\Scripts\python.exe'
$taskBasePythonPath = $taskPythonPath
$taskManagePath = 'C:\RAG test\manage.py'
$taskRepoRoot = 'C:\RAG test'
$taskLogDirectory = 'C:\RAG test\logs'
$taskRunStamp = 'regression'
$script:taskTestProcesses = @()
$script:taskTestListeners = @()
$script:taskTestCimFailure = $false
$script:taskTestNetworkFailure = $false
$script:taskTestLaunches = [Collections.Generic.List[object]]::new()
$script:taskTestTreeMode = $false
$script:taskTestFailFeedStart = $false
$script:taskTestNextPid = 80000
$script:taskTestStops = [Collections.Generic.List[int]]::new()
$script:taskTestManageCalls = [Collections.Generic.List[string]]::new()
$script:taskTestHealthCalls = 0
$taskPassed = 0

function Get-CimInstance {
    [CmdletBinding()]
    param([string]$ClassName, [string]$Filter)
    if ($ClassName -ne 'Win32_Process') { throw 'Unexpected CIM class.' }
    if ($script:taskTestCimFailure) { throw 'Process inspection denied.' }
    if ($Filter) {
        if ($Filter -notmatch '^ProcessId = (\d+)$') { throw 'Unexpected process filter.' }
        $taskRequestedPid = [int]$Matches[1]
        return @($script:taskTestProcesses | Where-Object { $_.ProcessId -eq $taskRequestedPid })
    }
    return $script:taskTestProcesses
}

function Get-NetTCPConnection {
    [CmdletBinding()]
    param([string]$State)
    if ($State -ne 'Listen') { throw 'Unexpected socket inspection.' }
    if ($script:taskTestNetworkFailure) { throw 'Listener inspection denied.' }
    return $script:taskTestListeners
}

function Start-Process {
    [CmdletBinding()]
    param([string]$FilePath, [string[]]$ArgumentList, [string]$WorkingDirectory,
        [string]$WindowStyle, [string]$RedirectStandardOutput, [string]$RedirectStandardError,
        [switch]$PassThru)
    $script:taskTestLaunches.Add([PSCustomObject]@{
        FilePath = $FilePath
        Arguments = $ArgumentList
        WorkingDirectory = $WorkingDirectory
        WindowStyle = $WindowStyle
    })
    if ($script:taskTestFailFeedStart -and $ArgumentList -contains 'run_feed_scheduler') {
        throw 'Mock scheduler startup failed.'
    }
    if ($script:taskTestTreeMode) {
        $script:taskTestNextPid += 2
        $taskRootId = $script:taskTestNextPid
        $taskRoot = New-TestProcess $taskRootId $taskPythonPath ('"' + $taskPythonPath + '" ' + ($ArgumentList -join ' '))
        $taskChild = New-TestProcess ($taskRootId + 1) $taskBasePythonPath ('"' + $taskBasePythonPath + '" ' + ($ArgumentList -join ' ')) $taskRootId
        $script:taskTestProcesses = @($script:taskTestProcesses) + @($taskRoot, $taskChild)
        if ($ArgumentList -contains 'runserver') {
            $script:taskTestListeners = @([PSCustomObject]@{ LocalPort = 8000; OwningProcess = $taskChild.ProcessId })
        }
        $taskHandle = [PSCustomObject]@{ Id = $taskRootId; HasExited = $false }
        $taskHandle | Add-Member -MemberType ScriptMethod -Name Refresh -Value { }
        return $taskHandle
    }
    return [PSCustomObject]@{ Id = 50001 }
}

function New-TestProcess([int]$ProcessId, [string]$ExecutablePath, [string]$CommandLine, [int]$ParentProcessId = 0) {
    [PSCustomObject]@{
        ProcessId = $ProcessId
        ParentProcessId = $ParentProcessId
        ExecutablePath = $ExecutablePath
        CommandLine = $CommandLine
        CreationDate = [datetime]'2026-11-12T12:00:00Z'
    }
}

function Stop-Process {
    [CmdletBinding()]
    param([int]$Id)
    $script:taskTestStops.Add($Id)
    $script:taskTestProcesses = @($script:taskTestProcesses | Where-Object { $_.ProcessId -ne $Id })
    $script:taskTestListeners = @($script:taskTestListeners | Where-Object { $_.OwningProcess -ne $Id })
}

function Wait-Process {
    [CmdletBinding()]
    param([int]$Id, [int]$Timeout)
}

function Get-BasePythonPath { return $taskBasePythonPath }

function Invoke-RagManage([string[]]$Arguments) { $script:taskTestManageCalls.Add($Arguments[0]) }

function Invoke-WebRequest {
    [CmdletBinding()]
    param([string]$Uri, [switch]$UseBasicParsing, [int]$MaximumRedirection, [int]$TimeoutSec)
    if ($Uri -cne 'http://127.0.0.1:8000/accounts/login/') { throw 'Unexpected health endpoint.' }
    $script:taskTestHealthCalls++
    return [PSCustomObject]@{ StatusCode = 200 }
}

function Assert-Throws([ScriptBlock]$Action, [string]$Expected) {
    try { & $Action }
    catch {
        if ($_.Exception.Message -notlike $Expected) { throw }
        return
    }
    throw "Expected an error matching '$Expected'."
}

$taskUnrelated = New-TestProcess 10 'C:\Windows\notepad.exe' 'notepad.exe'
$taskOtherPython = New-TestProcess 11 $taskPythonPath ('"' + $taskPythonPath + '" other.py')
$taskNoPath = New-TestProcess 12 '' ''
$taskWeb = New-TestProcess 101 $taskPythonPath ('"' + $taskPythonPath + '" "' + $taskManagePath + '" runserver 127.0.0.1:8000 --noreload')
$taskScheduler = New-TestProcess 102 $taskPythonPath ('"' + $taskPythonPath + '" manage.py run_feed_scheduler --interval 30')
$taskReportScheduler = New-TestProcess 103 $taskPythonPath ('"' + $taskPythonPath + '" .\manage.py run_report_scheduler --interval=30')

# Rejected candidates must emit no null element under strict mode.
foreach ($taskRejected in @($null, $taskUnrelated, $taskOtherPython, $taskNoPath)) {
    if (@(Get-VerifiedRunner $taskRejected).Count -ne 0) { throw 'Rejected process leaked a pipeline item.' }
}
$taskPassed++

# Reproduce the reported failure with unrelated processes before a scheduler.
$script:taskTestProcesses = @($taskUnrelated, $taskOtherPython, $taskNoPath, $taskScheduler)
$taskResult = @(Get-DeploymentRunners)
if ($taskResult.Count -ne 1 -or $taskResult[0].ProcessId -ne 102) { throw 'Mixed process filtering failed.' }
$taskPassed++

# An idle host and a host containing only unrelated processes are valid.
foreach ($taskInventory in @(@(), @($taskUnrelated, $taskOtherPython, $taskNoPath))) {
    $script:taskTestProcesses = $taskInventory
    if (@(Get-DeploymentRunners).Count -ne 0) { throw 'Idle host produced a runner.' }
}
$taskPassed++

# Keep all verified modes; ignore listeners on other ports.
$script:taskTestProcesses = @($taskUnrelated, $taskWeb, $taskScheduler, $taskReportScheduler)
$script:taskTestListeners = @(
    [PSCustomObject]@{ LocalPort = 8000; OwningProcess = 101 },
    [PSCustomObject]@{ LocalPort = 8888; OwningProcess = 900 }
)
$taskResult = @(Get-DeploymentRunners)
if ($taskResult.Count -ne 3) { throw 'Verified runners were dropped.' }
$taskPassed++

# Unknown owners and unrecognized schedulers must still abort before changes.
$script:taskTestListeners = @([PSCustomObject]@{ LocalPort = 8000; OwningProcess = 900 })
Assert-Throws { Get-DeploymentRunners } '*Port 8000 belongs to an unverified process*'
$taskPassed++

$script:taskTestListeners = @()
$taskUnknownScheduler = New-TestProcess 104 $taskPythonPath ('"' + $taskPythonPath + '" manage.py run_feed_scheduler --interval 1')
$script:taskTestProcesses = @($taskUnrelated, $taskUnknownScheduler)
Assert-Throws { Get-DeploymentRunners } '*unrecognized scheduler*104*'
$taskPassed++

# Never interpret failed inspection as an empty inventory or a free port.
$script:taskTestCimFailure = $true
Assert-Throws { Get-DeploymentRunners } '*Process inspection denied*'
$script:taskTestCimFailure = $false
$taskPassed++

$script:taskTestNetworkFailure = $true
Assert-Throws { Get-DeploymentRunners } '*Listener inspection denied*'
$script:taskTestNetworkFailure = $false
$taskPassed++

# Actual legacy invocation reported by the user: options precede manage.py.
$taskFlaggedScheduler = New-TestProcess 13900 $taskPythonPath ('"' + $taskPythonPath + '" -B -u -X utf8 manage.py run_report_scheduler --interval 30')
$script:taskTestProcesses = @($taskUnrelated, $taskFlaggedScheduler)
$taskResult = @(Get-DeploymentRunners)
if ($taskResult.Count -ne 1 -or $taskResult[0].ProcessId -ne 13900 -or
        ($taskResult[0].PythonArguments -join '|') -cne '-B|-u|-X|utf8') {
    throw 'Legacy scheduler options were not recognized and retained.'
}
$taskPassed++

foreach ($taskOptions in @('-B', '-u', '-X utf8', '-u -B -X utf8')) {
    $taskFlagged = New-TestProcess 201 $taskPythonPath ('"' + $taskPythonPath + '" ' + $taskOptions + ' "' + $taskManagePath + '" runserver 127.0.0.1:8000 --noreload')
    if (@(Get-VerifiedRunner $taskFlagged).Count -ne 1) { throw "Unsupported valid options: $taskOptions" }
    $taskPassed++
}

# Never mistake code/module wrappers or a different -x option for manage.py.
foreach ($taskOptions in @('-c "import sys"', '-m django', '-x utf8', '-X presite=other')) {
    $taskWrapped = New-TestProcess 202 $taskPythonPath ('"' + $taskPythonPath + '" ' + $taskOptions + ' manage.py run_report_scheduler --interval 30')
    if (@(Get-VerifiedRunner $taskWrapped).Count) { throw "Unexpected wrapper recognition: $taskOptions" }
    $taskPassed++
}
$taskOutside = New-TestProcess 203 $taskPythonPath ('"' + $taskPythonPath + '" -B -u -X utf8 "C:\Another project\manage.py" run_report_scheduler --interval 30')
if (@(Get-VerifiedRunner $taskOutside).Count) { throw 'A different checkout was accepted.' }
$taskPassed++

# Launches are mocked: verify logging options, identity and hidden windows.
$null = Start-RagRunner -Mode 'runserver' -RunnerArguments @('127.0.0.1:8000', '--noreload') -LogSuffix 'web'
$taskLaunch = $script:taskTestLaunches[0]
if (($taskLaunch.Arguments[0..2] -join '|') -cne '-u|-X|utf8' -or
        $taskLaunch.FilePath -cne $taskPythonPath -or $taskLaunch.WorkingDirectory -cne $taskRepoRoot -or
        $taskLaunch.WindowStyle -cne 'Hidden') { throw 'New runner launch options are incorrect.' }
$taskPassed++

# Rollback must preserve both flagged and unflagged original invocations.
$script:taskTestProcesses = @($taskUnrelated)
foreach ($taskOriginal in @($taskFlaggedScheduler, $taskScheduler)) {
    $taskStoppedRunners = [Collections.Generic.List[object]]::new()
    $taskOriginalRunner = Get-VerifiedRunner $taskOriginal
    $taskStoppedRunners.Add($taskOriginalRunner)
    $script:taskTestLaunches.Clear()
    Restore-PreviousRunners 3>$null
    $taskExpectedArguments = @($taskOriginalRunner.PythonArguments) + @(('"' + $taskManagePath + '"'), $taskOriginalRunner.Mode) + @($taskOriginalRunner.Arguments)
    if ($script:taskTestLaunches.Count -ne 1 -or
            ($script:taskTestLaunches[0].Arguments -join '|') -cne ($taskExpectedArguments -join '|')) {
        throw 'Original Python options were changed during rollback.'
    }
    $taskPassed++
}

# Windows venv launcher and listening interpreter have different executable paths.
$taskBasePythonPath = 'C:\Python314\python.exe'
$taskBaseWeb = New-TestProcess 301 $taskBasePythonPath ('"' + $taskBasePythonPath + '" "' + $taskManagePath + '" runserver 127.0.0.1:8000 --noreload') $taskWeb.ProcessId
$script:taskTestProcesses = @($taskUnrelated, $taskWeb, $taskBaseWeb)
$script:taskTestListeners = @([PSCustomObject]@{ LocalPort = 8000; OwningProcess = 301 })
$taskResult = @(Get-DeploymentRunners)
if ($taskResult.Count -ne 2 -or $taskResult[1].ProcessId -ne 301 -or
        $taskResult[1].LauncherProcessId -ne $taskWeb.ProcessId) { throw 'The verified venv runtime child was lost.' }
$taskPassed++

foreach ($taskBadChild in @(
    (New-TestProcess 302 $taskBasePythonPath $taskBaseWeb.CommandLine 999),
    (New-TestProcess 303 'C:\Another Python\python.exe' $taskBaseWeb.CommandLine $taskWeb.ProcessId),
    (New-TestProcess 304 $taskBasePythonPath ($taskBaseWeb.CommandLine + ' --insecure') $taskWeb.ProcessId)
)) {
    $taskResult = @(Get-VerifiedRunners -Processes @($taskWeb, $taskBadChild))
    if ($taskResult.Count -ne 1) { throw 'An unrelated runtime child was accepted.' }
    $taskPassed++
}
$taskOldChild = New-TestProcess 305 $taskBasePythonPath $taskBaseWeb.CommandLine $taskWeb.ProcessId
$taskOldChild.CreationDate = $taskWeb.CreationDate.AddSeconds(-1)
if (@(Get-VerifiedRunners -Processes @($taskWeb, $taskOldChild)).Count -ne 1) { throw 'A stale parent PID was trusted.' }
$taskPassed++

# A base interpreter alone never grants ownership, including an orphan scheduler.
$taskOrphan = New-TestProcess 306 $taskBasePythonPath ('"' + $taskBasePythonPath + '" "' + $taskManagePath + '" run_report_scheduler --interval 30') 999
$script:taskTestProcesses = @($taskOrphan)
$script:taskTestListeners = @()
Assert-Throws { Get-DeploymentRunners } '*unrecognized scheduler*306*'
$taskPassed++

$taskExpectedChild = @(Get-VerifiedRunners -Processes @($taskWeb, $taskBaseWeb))[1]
$taskReusedPid = New-TestProcess 301 $taskBasePythonPath $taskBaseWeb.CommandLine $taskWeb.ProcessId
$taskReusedPid.CreationDate = $taskBaseWeb.CreationDate.AddSeconds(1)
if (-not (Test-RunnerIdentity $taskBaseWeb $taskExpectedChild) -or
        (Test-RunnerIdentity $taskReusedPid $taskExpectedChild)) { throw 'Runtime PID reuse was not rejected.' }
$taskPassed++

# Exercise the actual deployment try/catch/finally with all process, network and
# Django commands mocked. Only temporary lock/log directories are created.
$taskTestRoot = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) ('output\review_tmp\deploy-flow-' + [Guid]::NewGuid().ToString('N'))
$taskOutputPath = $taskTestRoot
$taskBackupDirectory = Join-Path $taskTestRoot 'backups'
$taskLogDirectory = Join-Path $taskTestRoot 'logs'
$taskDatabasePath = Join-Path $taskTestRoot 'no-database.sqlite3'
$taskDeployLock = $null
$taskBackupPath = $null
$taskStoppedRunners = [Collections.Generic.List[object]]::new()
$taskNewHandles = [Collections.Generic.List[object]]::new()
$HealthTimeoutSeconds = 1
$script:taskTestTreeMode = $true
$script:taskTestLaunches.Clear()
$script:taskTestStops.Clear()
$taskBaseReport = New-TestProcess 13901 $taskBasePythonPath ('"' + $taskBasePythonPath + '" -B -u -X utf8 manage.py run_report_scheduler --interval 30') 13900
$script:taskTestProcesses = @($taskUnrelated, $taskFlaggedScheduler, $taskBaseReport)
$script:taskTestListeners = @()
$taskMainTry = @($taskAst.EndBlock.Statements | Where-Object { $_ -is [Management.Automation.Language.TryStatementAst] })
if ($taskMainTry.Count -ne 1) { throw 'Could not identify deployment entry point.' }
. ([ScriptBlock]::Create($taskMainTry[0].Extent.Text)) 6>$null
if (-not $taskHealthy -or $script:taskTestHealthCalls -ne 1 -or
        ($script:taskTestStops -join '|') -cne '13901|13900' -or
        ($script:taskTestManageCalls -join '|') -cne 'migrate|collectstatic|check' -or
        $script:taskTestLaunches.Count -ne 2 -or $taskNewHandles.Count -ne 4) {
    throw 'The complete venv deployment workflow failed.'
}
$taskPassed++

# A failed new scheduler must clean up both web processes and restore just one
# copy of the original report scheduler with its original Python options.
$taskOutputPath = Join-Path $taskTestRoot 'rollback'
$taskBackupDirectory = Join-Path $taskOutputPath 'backups'
$taskLogDirectory = Join-Path $taskOutputPath 'logs'
$taskDatabasePath = Join-Path $taskOutputPath 'no-database.sqlite3'
$taskDeployLock = $null
$taskBackupPath = $null
$taskStoppedRunners = [Collections.Generic.List[object]]::new()
$taskNewHandles = [Collections.Generic.List[object]]::new()
$script:taskTestLaunches.Clear()
$script:taskTestStops.Clear()
$script:taskTestProcesses = @($taskUnrelated, $taskFlaggedScheduler, $taskBaseReport)
$script:taskTestListeners = @()
$script:taskTestFailFeedStart = $true
Assert-Throws { . ([ScriptBlock]::Create($taskMainTry[0].Extent.Text)) 3>$null 6>$null } '*Mock scheduler startup failed*'
$taskRemaining = @(Get-VerifiedRunners -Processes $script:taskTestProcesses)
if ($script:taskTestStops.Count -ne 4 -or $script:taskTestListeners.Count -ne 0 -or
        $taskRemaining.Count -ne 2 -or @($taskRemaining | Where-Object { $_.Mode -ne 'run_report_scheduler' }).Count -or
        ($script:taskTestLaunches[2].Arguments[0..3] -join '|') -cne '-B|-u|-X|utf8') {
    throw 'Venv cleanup or restoration after scheduler failure was incorrect.'
}
$taskPassed++

Write-Host "$taskPassed deployment regression checks passed on PowerShell $($PSVersionTable.PSVersion)."
