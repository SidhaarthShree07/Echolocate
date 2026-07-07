@echo off
REM EchoLocate Windows installer wrapper
REM Launches install.ps1 via PowerShell for users who can't run .ps1 directly

echo Launching EchoLocate installer via PowerShell...
powershell.exe -ExecutionPolicy Bypass -File "%~dp0install.ps1"
pause
