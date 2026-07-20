@echo off
setlocal

set "NAME=%~1"
set "BASE_DIR=%~dp0.."
set "TARGET="

if /I "%NAME%"=="readme" set "TARGET=README.md"
if /I "%NAME%"=="example_text" set "TARGET=scripts\example.txt"

if "%TARGET%"=="" (
    echo Target is not allowed: %NAME%
    exit /b 2
)

set "TARGET_PATH=%BASE_DIR%\%TARGET%"
if not exist "%TARGET_PATH%" (
    echo Target does not exist: %TARGET_PATH%
    exit /b 3
)

start "" "%TARGET_PATH%"
echo Opened: %TARGET_PATH%

