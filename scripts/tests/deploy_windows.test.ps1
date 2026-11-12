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
foreach ($taskFunctionName in @('Test-SamePath', 'Get-VerifiedRunner', 'Get-AppListeners', 'Get-DeploymentRunners')) {
    $taskDefinition = $taskAst.Find({
        param($Node)
        $Node -is [Management.Automation.Language.FunctionDefinitionAst] -and $Node.Name -eq $taskFunctionName
    }, $false)
    if ($null -eq $taskDefinition) { throw "Missing function: $taskFunctionName" }
    . ([ScriptBlock]::Create($taskDefinition.Extent.Text))
}

$taskPythonPath = 'C:\RAG test\.venv\Scripts\python.exe'
$taskManagePath = 'C:\RAG test\manage.py'
$script:taskTestProcesses = @()
$script:taskTestListeners = @()
$script:taskTestCimFailure = $false
$script:taskTestNetworkFailure = $false
$taskPassed = 0

function Get-CimInstance {
    [CmdletBinding()]
    param([string]$ClassName)
    if ($ClassName -ne 'Win32_Process') { throw 'Unexpected CIM class.' }
    if ($script:taskTestCimFailure) { throw 'Process inspection denied.' }
    return $script:taskTestProcesses
}

function Get-NetTCPConnection {
    [CmdletBinding()]
    param([string]$State)
    if ($State -ne 'Listen') { throw 'Unexpected socket inspection.' }
    if ($script:taskTestNetworkFailure) { throw 'Listener inspection denied.' }
    return $script:taskTestListeners
}

function New-TestProcess([int]$ProcessId, [string]$ExecutablePath, [string]$CommandLine) {
    [PSCustomObject]@{
        ProcessId = $ProcessId
        ExecutablePath = $ExecutablePath
        CommandLine = $CommandLine
        CreationDate = [datetime]'2026-11-12T12:00:00Z'
    }
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

Write-Host "$taskPassed deployment regression checks passed on PowerShell $($PSVersionTable.PSVersion)."
