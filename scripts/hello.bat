@echo off
set "MESSAGE=%~1"
if "%MESSAGE%"=="" set "MESSAGE=Hello from controller"

echo Computer: %COMPUTERNAME%
echo User: %USERNAME%
echo Time: %DATE% %TIME%
echo Message: %MESSAGE%

