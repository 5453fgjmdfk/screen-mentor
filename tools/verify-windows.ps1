param([string]$PythonExe = 'python')

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$plugin = Join-Path $repo 'codex-plugin'
$scripts = Join-Path $plugin 'scripts'
$rootModules = @((Join-Path $repo 'capture.py'), (Join-Path $repo 'overlay.py'))

& $PythonExe -m pip --disable-pip-version-check check
if ($LASTEXITCODE -ne 0) { throw 'Python dependency check failed.' }

foreach ($module in $rootModules) {
    if (-not (Test-Path -LiteralPath $module -PathType Leaf)) { throw "Missing runtime source: $module" }
}
& $PythonExe -m compileall -q @rootModules $scripts (Join-Path $plugin 'tests')
if ($LASTEXITCODE -ne 0) { throw 'Python compilation failed.' }

$tokens = $null
$parseErrors = $null
$installer = [System.Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $plugin 'install.ps1'), [ref]$tokens, [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) { throw ($parseErrors -join [Environment]::NewLine) }

$fileList = $installer.Find({
    param($node)
    $node -is [System.Management.Automation.Language.AssignmentStatementAst] -and
    $node.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
    $node.Left.VariablePath.UserPath -eq 'files'
}, $false)
if (-not $fileList) { throw 'Installer payload list is missing.' }
$payload = @($fileList.Right.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.StringConstantExpressionAst]
}, $false) | ForEach-Object { $_.Value })

foreach ($relative in $payload) {
    if (-not (Test-Path -LiteralPath (Join-Path $plugin $relative) -PathType Leaf)) {
        throw "Installer source file is missing: $relative"
    }
}
# The stable launcher is copied separately, before it is activated.
if (-not (Test-Path -LiteralPath (Join-Path $scripts 'mcp_launch.py') -PathType Leaf)) {
    throw 'The stable MCP launcher is missing.'
}
foreach ($module in (Get-ChildItem -LiteralPath $scripts -File -Filter '*.py')) {
    if ($module.Name -ne 'mcp_launch.py' -and "scripts/$($module.Name)" -cnotin $payload) {
        throw "Runtime module is missing from the installer payload: $($module.Name)"
    }
}

$manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $plugin '.codex-plugin/plugin.json') | ConvertFrom-Json
$mcp = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $plugin '.mcp.json') | ConvertFrom-Json
if ($manifest.name -cne 'screen-mentor' -or $manifest.version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$' -or
    $manifest.skills -cne './skills/' -or $manifest.mcpServers -cne './.mcp.json' -or
    -not $mcp.mcpServers.screen_mentor.command -or
    $mcp.mcpServers.screen_mentor.args -cnotcontains 'scripts/bridge.py') {
    throw 'The plugin or MCP manifest does not match the runtime layout.'
}
foreach ($required in @('.codex-plugin/plugin.json', 'skills/screen-mentor/SKILL.md')) {
    if ($required -cnotin $payload) { throw "Installer payload is missing: $required" }
}
$protocolVersion = & $PythonExe -B -c 'import sys; sys.path.insert(0, sys.argv[1]); from protocol import VERSION; print(VERSION)' $scripts
if ($LASTEXITCODE -ne 0 -or $protocolVersion -cne $manifest.version) {
    throw 'The plugin and MCP runtime versions differ.'
}
Write-Host 'Installer syntax, payload, and plugin manifests passed.'

$runTests = @'
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

repo = Path(sys.argv[1])
sys.path.insert(0, str(repo))
with TemporaryDirectory(prefix="screen-mentor-verify-") as folder:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["SCREEN_MENTOR_PROFILE"] = str(Path(folder) / "profile")
    tests = unittest.defaultTestLoader.discover(str(repo / "codex-plugin" / "tests"))
    if not tests.countTestCases():
        sys.exit("No regression tests were discovered.")
    result = unittest.TextTestRunner(verbosity=2).run(tests)
sys.exit(not result.wasSuccessful())
'@
$runTests | & $PythonExe -X utf8 -B -W error::ResourceWarning - $repo
if ($LASTEXITCODE -ne 0) { throw 'Windows regression tests failed.' }
