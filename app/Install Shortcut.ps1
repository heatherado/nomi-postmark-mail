$ErrorActionPreference = 'Stop'
$pythonWindow = (Get-Command pythonw.exe -ErrorAction Stop).Source
$scriptPath = Join-Path $PSScriptRoot 'Companion App.pyw'
if (-not (Test-Path -LiteralPath $scriptPath)) { throw 'Companion App.pyw is missing.' }
$desktopDirectory = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktopDirectory 'Nomi Postmark Mail.lnk'
if (Test-Path -LiteralPath $shortcutPath) { throw 'The shortcut already exists. It was left unchanged.' }
$shellObject = New-Object -ComObject WScript.Shell
$shortcut = $shellObject.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonWindow
$shortcut.Arguments = '"' + $scriptPath + '"'
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.Description = 'Nomi Postmark Mail v2.0 — Lamplit Desk'
$iconPath = Join-Path $PSScriptRoot 'assets\lamplit-mail-v1.ico'
if (Test-Path -LiteralPath $iconPath) { $shortcut.IconLocation = $iconPath }
$shortcut.Save()
Write-Output ('Created: ' + $shortcutPath)
