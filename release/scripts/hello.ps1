param(
    [string]$Message = "Hello from controller"
)

Write-Output "Computer: $env:COMPUTERNAME"
Write-Output "User: $env:USERNAME"
Write-Output "Time: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Output "Message: $Message"
