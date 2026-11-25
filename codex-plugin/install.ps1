param(
    [string]$CodexExe,
    [string]$PythonExe,
    [switch]$DesktopOnly,
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'ScreenMentor'),
    [string]$ShortcutDirectory = [Environment]::GetFolderPath('DesktopDirectory')
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $PSScriptRoot '.codex-plugin/plugin.json') | ConvertFrom-Json
$desktopRoot = Join-Path $InstallRoot ('versions/' + $manifest.version)
$marketRoot = Join-Path $InstallRoot 'marketplace'
$pluginRoot = Join-Path $marketRoot 'screen-mentor'
$marketManifest = Join-Path $marketRoot '.agents/plugins/marketplace.json'
$runtime = Join-Path $InstallRoot 'runtime'
$mcpRoot = Join-Path $InstallRoot 'mcp'
$launcher = Join-Path $mcpRoot 'launch.py'
$runtimePython = Join-Path $runtime 'Scripts/python.exe'
$installUtf8 = [Text.UTF8Encoding]::new($false)

if (-not $DesktopOnly -and -not $CodexExe) {
    $found = Get-Command codex -ErrorAction SilentlyContinue
    if ($found) {
        $CodexExe = $found.Source
    } else {
        $CodexExe = Get-Process -Name codex -ErrorAction SilentlyContinue |
            Where-Object Path | Select-Object -First 1 -ExpandProperty Path
    }
}
if (-not $DesktopOnly -and -not $CodexExe) {
    throw 'Open Codex desktop first, or choose Install Desktop Companion.'
}

if (-not (Test-Path -LiteralPath $runtimePython)) {
    $pythonArguments = @()
    if (-not $PythonExe) {
        $pythonLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
        $python = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($pythonLauncher) {
            $PythonExe = $pythonLauncher.Source
            $pythonArguments = @('-3')
        } elseif ($python) {
            $PythonExe = $python.Source
        } else {
            throw 'Python 3.11 or newer is required. Install it from python.org and run the installer again.'
        }
    }
    & $PythonExe @pythonArguments -c 'import sys; sys.exit(sys.version_info < (3, 11))'
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 or newer is required.' }
    & $PythonExe @pythonArguments -m venv $runtime
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Screen Mentor Python environment.' }
}
& $runtimePython -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Could not install Screen Mentor dependencies.' }

function Resolve-InstalledPath([string]$Path) {
    $previousEncoding = [Console]::OutputEncoding
    try {
        [Console]::OutputEncoding = $installUtf8
        $resolved = & $runtimePython -X utf8 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' $Path
        if ($LASTEXITCODE -ne 0) { throw "Could not resolve install path: $Path" }
        return $resolved
    } finally {
        [Console]::OutputEncoding = $previousEncoding
    }
}

$runtimeResolved = Resolve-InstalledPath $runtimePython
$runtimeWindowed = Join-Path (Split-Path -Parent $runtimeResolved) 'pythonw.exe'
$mcp = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $PSScriptRoot '.mcp.json') | ConvertFrom-Json
$mcp.mcpServers.screen_mentor.command = $runtimeResolved
$installedRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $runtimeResolved))
$mcp.mcpServers.screen_mentor.args = @('-B', (Join-Path $installedRoot 'mcp/launch.py'))
$mcpJson = $mcp | ConvertTo-Json -Depth 12

$destinations = @($desktopRoot)
if (-not $DesktopOnly) { $destinations += $pluginRoot }
$files = @(
    '.codex-plugin/plugin.json', 'skills/screen-mentor/SKILL.md',
    'scripts/native.py', 'scripts/server.py', 'scripts/practice.py', 'scripts/target.py',
    'scripts/companion.py', 'scripts/appearance.py', 'scripts/skin_assets.py', 'scripts/window_reader.py',
    'scripts/desktop.py', 'scripts/bridge.py', 'scripts/protocol.py', 'scripts/resident_agent.py',
    'scripts/agent_config.py',
    'scripts/manager.py', 'scripts/team.py', 'scripts/scheduled.py', 'scripts/diagnostics.py',
    'assets/app-icon.ico', 'assets/app-icon.png', 'assets/companion/idle.gif', 'assets/companion/reading.gif',
    'assets/companion/ready.gif', 'assets/companion/thinking.gif', 'assets/companion/peek-poses.png',
    'assets/companion/peek-blink.png', 'assets/companion/SOURCE.md',
    'assets/companion/whale-maid/idle.gif', 'assets/companion/whale-maid/reading.gif',
    'assets/companion/whale-maid/thinking.gif', 'assets/companion/whale-maid/ready.gif',
    'assets/companion/whale-maid/side.gif', 'assets/companion/whale-maid/side.png',
    'assets/companion/whale-maid/bottom.gif', 'assets/companion/whale-maid/bottom.png',
    'assets/companion/whale-maid/SOURCE.md'
)
foreach ($destination in $destinations) {
    foreach ($relative in @('.codex-plugin', 'skills/screen-mentor', 'scripts', 'assets/companion', 'assets/companion/whale-maid')) {
        New-Item -ItemType Directory -Force -Path (Join-Path $destination $relative) | Out-Null
    }
    foreach ($relative in $files) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $relative) -Destination (Join-Path $destination $relative) -Force
    }
    foreach ($module in @('capture.py', 'overlay.py')) {
        Copy-Item -LiteralPath (Join-Path $repo $module) -Destination (Join-Path $destination "scripts/$module") -Force
    }
    [IO.File]::WriteAllText((Join-Path $destination '.mcp.json'), $mcpJson, $installUtf8)
}

if (-not $DesktopOnly) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $marketManifest) | Out-Null
    $marketplace = @{
        name = 'screen-mentor-local'
        interface = @{displayName = 'Screen Mentor Local'}
        plugins = @(@{
            name = 'screen-mentor'
            source = @{source = 'local'; path = './screen-mentor'}
            policy = @{installation = 'AVAILABLE'; authentication = 'ON_INSTALL'}
            category = 'Productivity'
        })
    }
    [IO.File]::WriteAllText($marketManifest, ($marketplace | ConvertTo-Json -Depth 8), $installUtf8)
    & $CodexExe plugin marketplace add $marketRoot
    if ($LASTEXITCODE -ne 0) { throw 'Could not register the local marketplace.' }
    & $CodexExe plugin add 'screen-mentor@screen-mentor-local'
    if ($LASTEXITCODE -ne 0) { throw 'Could not install the Screen Mentor plugin.' }
}

$desktopResolved = Resolve-InstalledPath $desktopRoot
New-Item -ItemType Directory -Force -Path $ShortcutDirectory | Out-Null
$shell = New-Object -ComObject WScript.Shell
$appearanceLabel = -join (0x5916, 0x89C2, 0x7BA1, 0x7406 | ForEach-Object { [char]$_ })
$desktopLabel = -join (0x684C, 0x9762, 0x5B66, 0x4F34 | ForEach-Object { [char]$_ })
foreach ($shortcut in @(@{Label = $appearanceLabel; File = 'appearance.py'}, @{Label = $desktopLabel; File = 'desktop.py'})) {
    $path = Join-Path $ShortcutDirectory ($manifest.interface.displayName + ' - ' + $shortcut.Label + '.lnk')
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $runtimeWindowed
    $link.Arguments = '-B "' + (Join-Path $desktopResolved ('scripts/' + $shortcut.File)) + '"'
    $link.WorkingDirectory = Join-Path $desktopResolved 'scripts'
    $link.IconLocation = (Join-Path $desktopResolved 'assets/app-icon.ico') + ',0'
    $link.Save()
}
New-Item -ItemType Directory -Force -Path $mcpRoot | Out-Null
$stagedLauncher = Join-Path $mcpRoot ('launch.' + [guid]::NewGuid().ToString('N') + '.tmp')
$stagedVersion = Join-Path $mcpRoot ('version.' + [guid]::NewGuid().ToString('N') + '.tmp')
$versionPointer = Join-Path $mcpRoot 'version.txt'
try {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'scripts/mcp_launch.py') -Destination $stagedLauncher
    [IO.File]::WriteAllText($stagedVersion, $manifest.version, $installUtf8)
    & $runtimePython -B -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2]); os.replace(sys.argv[3], sys.argv[4])' $stagedLauncher $launcher $stagedVersion $versionPointer
    if ($LASTEXITCODE -ne 0) { throw 'Could not activate the local MCP entry point.' }
} finally {
    foreach ($pending in @($stagedLauncher, $stagedVersion)) {
        if (Test-Path -LiteralPath $pending) { Remove-Item -LiteralPath $pending }
    }
}
Write-Output "Screen Mentor $($manifest.version) installed."
