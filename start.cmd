@echo off
rem Doppelklick-Start fuer Windows. Umgeht die PowerShell-Skriptsperre nur fuer diesen Aufruf.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
pause
