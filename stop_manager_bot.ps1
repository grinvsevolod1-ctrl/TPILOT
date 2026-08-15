$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$found = $false

Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -like "*$root*manager_bot.py*"
} | ForEach-Object {
    $found = $true
    Write-Host "KILL MANAGER BOT PID $($_.ProcessId)"
    taskkill /F /T /PID $_.ProcessId 2>$null | Out-Host
}

if (-not $found) {
    Write-Host "MANAGER BOT NOT RUNNING"
}

Write-Host "STOP MANAGER BOT OK"
