@echo off
REM DevinChat send - simple message sending
REM Usage: send <room> <message>        (sends to a room with your saved name)
REM Usage: send <message>               (sends to general with your saved name)
REM Usage: send --name YourName <message>  (sets your name first time)

setlocal enabledelayedexpansion

REM Check for saved name
set NAMEFILE=%USERPROFILE%\.devinchat_name
if exist "%NAMEFILE%" (
    set /p SENDER=<"%NAMEFILE%"
) else (
    set SENDER=
)

REM Parse args
if /i "%~1"=="--name" (
    set SENDER=%~2
    echo %~2>"%NAMEFILE%"
    shift
    shift
)

if "%SENDER%"=="" (
    echo First time? Set your name: send --name YourName "your message"
    exit /b 1
)

REM If first arg is a known room name, use it; otherwise send to general
set ROOMS=general voice-changer-team narrator-vc-suggestions builder-and-user dm-test
set ROOM=general
set MSGSTART=1

REM Check if first arg matches a room
for %%R in (%ROOMS%) do (
    if /i "%~1"=="%%R" (
        set ROOM=%%R
        set MSGSTART=2
    )
)

REM Build message from remaining args
set MSG=
:loop
if "%~1"=="" goto done
if !MSGSTART! leq 1 (
    set MSG=!MSG! %~1
)
set /a MSGSTART-=1
shift
goto loop
:done

REM Remove leading space
set MSG=%MSG:~1%

if "%MSG%"=="" (
    echo Usage: send [room] "your message"
    exit /b 1
)

python "%~dp0send.py" "%ROOM%" "%SENDER%" "%MSG%"
