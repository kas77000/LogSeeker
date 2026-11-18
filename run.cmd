@echo off
rem Start LogSeeker (double-click, or run from any folder).
rem Goes through run.ps1 with the execution policy bypassed for this one call,
rem so it works where unsigned .ps1 scripts are blocked.
rem Stop it with stop.cmd, or Ctrl+C in this window.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" -LocalOnly %*

pause
