@echo off
rem Stop LogSeeker: finds whatever is listening on the port (8510 by default,
rem or the first argument: stop.cmd 8600) and, only if it is Streamlit, ends it
rem together with its child processes (search and kdb workers).

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8510"

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$port = %PORT%;" ^
  "$ids = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique;" ^
  "if (-not $ids) { Write-Host \"LogSeeker is not running on port $port.\"; exit 0 }" ^
  "foreach ($id in $ids) {" ^
  "  $p = Get-CimInstance Win32_Process -Filter \"ProcessId = $id\";" ^
  "  if ($p.CommandLine -notmatch 'streamlit') { Write-Host \"Port $port is used by $($p.Name) (PID $id), not LogSeeker - left running.\"; continue }" ^
  "  taskkill /PID $id /T /F | Out-Null;" ^
  "  Write-Host \"LogSeeker stopped (PID $id, port $port).\"" ^
  "}"

pause
