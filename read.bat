@echo off
REM DevinChat read - simple message reading
REM Usage: read              (last 10 from general)
REM Usage: read voice-changer-team   (last 10 from that room)
REM Usage: read general 5    (last 5 from general)

if "%~1"=="" (
    python "%~dp0read.py"
) else (
    python "%~dp0read.py" %*
)
