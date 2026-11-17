param(
    [string]$CodexExe,
    [string]$PythonExe,
    [string]$CompanionArt
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$installRoot = Join-Path $env:LOCALAPPDATA 'ScreenMentor'
$marketRoot = Join-Path $installRoot 'marketplace'
$pluginRoot = Join-Path $marketRoot 'screen-mentor'
$marketManifest = Join-Path $marketRoot '.agents/plugins/marketplace.json'
$runtime = Join-Path $installRoot 'runtime'
$runtimePython = Join-Path $runtime 'Scripts/python.exe'
$installUtf8 = [Text.UTF8Encoding]::new($false)
$artFiles = @('idle.gif', 'reading.gif', 'thinking.gif', 'ready.gif')
if (-not $CompanionArt) {
    $bundledArt = Join-Path $PSScriptRoot 'assets/companion'
    if (Test-Path -LiteralPath $bundledArt -PathType Container) {
        $CompanionArt = $bundledArt
    }
}
if ($CompanionArt) {
    foreach ($file in $artFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $CompanionArt $file) -PathType Leaf)) {
            throw "Companion artwork is missing $file."
        }
    }
}

if (-not $CodexExe) {
    $found = Get-Command codex -ErrorAction SilentlyContinue
    if ($found) {
        $CodexExe = $found.Source
    } else {
        $CodexExe = Get-Process -Name codex -ErrorAction SilentlyContinue |
            Where-Object Path | Select-Object -First 1 -ExpandProperty Path
    }
}
if (-not $CodexExe) {
    throw 'Open Codex desktop first, or supply -CodexExe.'
}

if (-not (Test-Path -LiteralPath $runtimePython)) {
    if (-not $PythonExe) {
        $PythonExe = (Get-Command python.exe).Source
    }
    & $PythonExe -m venv $runtime
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Screen Mentor Python environment.' }
}
& $runtimePython -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Could not install Screen Mentor dependencies.' }

foreach ($relative in @('.codex-plugin', 'skills/screen-mentor', 'scripts')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $pluginRoot $relative) | Out-Null
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $marketManifest) | Out-Null
foreach ($relative in @('.codex-plugin/plugin.json', 'skills/screen-mentor/SKILL.md', 'scripts/native.py', 'scripts/server.py', 'scripts/practice.py', 'scripts/target.py', 'scripts/companion.py', 'scripts/window_reader.py')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $relative) -Destination (Join-Path $pluginRoot $relative) -Force
}
if ($CompanionArt) {
    $artTarget = Join-Path $pluginRoot 'assets/companion'
    New-Item -ItemType Directory -Force -Path $artTarget | Out-Null
    foreach ($file in @($artFiles + 'SOURCE.md')) {
        $inputFile = Join-Path $CompanionArt $file
        if (Test-Path -LiteralPath $inputFile -PathType Leaf) {
            Copy-Item -LiteralPath $inputFile -Destination $artTarget -Force
        }
    }
}
foreach ($module in @('capture.py', 'overlay.py')) {
    Copy-Item -LiteralPath (Join-Path $repo $module) -Destination (Join-Path $pluginRoot "scripts/$module") -Force
}

$mcpPath = Join-Path $PSScriptRoot '.mcp.json'
$mcp = Get-Content -Raw -Encoding UTF8 -LiteralPath $mcpPath | ConvertFrom-Json
$runtimeResolved = & $runtimePython -c 'import os, sys; print(os.path.realpath(sys.executable))'
if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the Screen Mentor interpreter path.' }
$mcp.mcpServers.screen_mentor.command = $runtimeResolved
[IO.File]::WriteAllText((Join-Path $pluginRoot '.mcp.json'), ($mcp | ConvertTo-Json -Depth 12), $installUtf8)
[IO.File]::WriteAllText($marketManifest, @'
{
  "name": "screen-mentor-local",
  "interface": {"displayName": "Screen Mentor Local"},
  "plugins": [
    {
      "name": "screen-mentor",
      "source": {"source": "local", "path": "./screen-mentor"},
      "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
      "category": "Productivity"
    }
  ]
}
'@, $installUtf8)

& $CodexExe plugin marketplace add $marketRoot
if ($LASTEXITCODE -ne 0) { throw 'Could not register the local marketplace.' }
& $CodexExe plugin add 'screen-mentor@screen-mentor-local'
if ($LASTEXITCODE -ne 0) { throw 'Could not install the Screen Mentor plugin.' }

$desktop = [Environment]::GetFolderPath('DesktopDirectory')
$manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $pluginRoot '.codex-plugin/plugin.json') | ConvertFrom-Json
$shortcutPath = Join-Path $desktop ($manifest.interface.displayName + '.lnk')
if (Test-Path -LiteralPath $shortcutPath -PathType Leaf) {
    $oldLink = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
    $oldLauncher = & $runtimePython -X utf8 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' (Join-Path $pluginRoot 'scripts/launch.py')
    if ($oldLink.Arguments.StartsWith('-B "' + $oldLauncher + '" ')) {
        Remove-Item -LiteralPath $shortcutPath
    }
}
