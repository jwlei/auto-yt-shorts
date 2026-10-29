# Creates an "AutoShorts" shortcut on the Desktop and in the Start menu that
# launches the web UI (scripts/start_webapp.ps1) on port 8800. Safe to re-run.
#   powershell -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1

$root = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $PSScriptRoot 'start_webapp.ps1'
$shell = New-Object -ComObject WScript.Shell

foreach ($folder in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
    $shortcut = $shell.CreateShortcut((Join-Path $folder 'AutoShorts.lnk'))
    $shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`""
    $shortcut.WorkingDirectory = $root
    $shortcut.Description = 'Open the AutoShorts control panel'
    # Clapperboard-style "video" icon from Windows' own icon library.
    $shortcut.IconLocation = Join-Path $env:SystemRoot 'System32\imageres.dll,18'
    $shortcut.Save()
    Write-Output "Created $(Join-Path $folder 'AutoShorts.lnk')"
}
