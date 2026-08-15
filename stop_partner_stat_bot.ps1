$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$found = $false
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*$root*partner_stat_bot.py*" } | ForEach-Object {
    $found = $true
    Write-Host "KILL PARTNER BOT PID $($_.ProcessId)"
    taskkill /F /T /PID $_.ProcessId 2>$null | Out-Host
}
if (-not $found) { Write-Host "PARTNER STAT BOT NOT RUNNING" }
Write-Host "STOP PARTNER STAT BOT OK"
