@echo off
chcp 65001 >nul
cd /d "C:\ALM_TPilot"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$base='C:\ALM_TPilot'; $py=Join-Path $base 'venv\Scripts\python.exe'; $envp=Join-Path $base '.env.TPilot'; $reg=Join-Path $base 'manager_registry.py'; $main=Join-Path $base 'main.py'; $keys=& $py $reg --env $envp list-active-keys; if(-not $keys){Write-Host 'NO ACTIVE MANAGERS FOUND'}; foreach($k in $keys){$k=($k+'').Trim(); if(!$k){continue}; $dir=Join-Path $base ('runtime\managers\'+$k); New-Item -ItemType Directory -Force -Path $dir | Out-Null; Start-Process -FilePath $py -ArgumentList @($main,'--env',$envp,'--manager',$k) -WorkingDirectory $base -RedirectStandardOutput (Join-Path $dir ($k+'.log')) -RedirectStandardError (Join-Path $dir ($k+'.err.log')); Write-Host ('START MANAGER OK: '+$k)}"

echo START ALL MANAGERS OK

