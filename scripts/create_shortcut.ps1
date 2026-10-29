# Creates an "AutoShorts" shortcut on the Desktop and in the Start menu that
# runs start_webapp.pyw: starts the control panel (port 8800) with no console
# window and opens it in the default browser. Safe to re-run.
#   powershell -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1

$root = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $root 'start_webapp.pyw'

# pythonw / pyw run Python without a console window.
$pythonw = (Get-Command pyw -ErrorAction SilentlyContinue).Source
if (-not $pythonw) { $pythonw = (Get-Command pythonw).Source }

$shell = New-Object -ComObject WScript.Shell
foreach ($folder in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
    $shortcut = $shell.CreateShortcut((Join-Path $folder 'AutoShorts.lnk'))
    $shortcut.TargetPath = $pythonw
    $shortcut.Arguments = "`"$launcher`""
    $shortcut.WorkingDirectory = $root
    $shortcut.Description = 'Open the Auto YT Shorts control panel'
    # Clapperboard-style "video" icon from Windows' own icon library.
    $shortcut.IconLocation = Join-Path $env:SystemRoot 'System32\imageres.dll,18'
    $shortcut.Save()
    Write-Output "Created $(Join-Path $folder 'AutoShorts.lnk')"
}
