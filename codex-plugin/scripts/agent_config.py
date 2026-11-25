"""Local MCP configuration examples for desktop Agent clients."""
import json
from pathlib import Path
import sys


CLIENTS = (
    ("generic", "Claude Desktop / Claude Code / Cursor / Cline",
     "将此配置合并到客户端的 MCP JSON。可保留同一文件里的其他工具。"),
    ("vscode", "VS Code / GitHub Copilot",
     "运行「MCP: Open User Configuration」或打开项目的 .vscode/mcp.json，再合并配置。"),
    ("opencode", "OpenCode 2",
     "将此配置合并到 opencode.json 或 opencode.jsonc 的 mcp.servers。"),
    ("opencode-v1", "OpenCode 1",
     "将此配置合并到 opencode.json 或 opencode.jsonc 的 mcp。"),
    ("hermes", "Hermes Agent",
     "将此配置合并到 Hermes 的 config.yaml。已有 mcp_servers 时，只添加 screen-mentor 条目。"),
)


def configuration(client):
    python = Path(sys.executable).with_name("python.exe")
    if not python.is_file():
        python = Path(sys.executable)
    launcher = python.parent.parent.parent / "mcp" / "launch.py"
    if not launcher.is_file():
        launcher = Path(__file__).with_name("bridge.py")
    command = {"command": str(python), "args": ["-B", str(launcher)]}
    if client == "hermes":
        return ("mcp_servers:\n  screen-mentor:\n"
                f"    command: {json.dumps(command['command'], ensure_ascii=False)}\n"
                f"    args: {json.dumps(command['args'], ensure_ascii=False)}")
    if client in {"opencode", "opencode-v1"}:
        servers = {"screen-mentor": {"type": "local", "command": [command["command"], *command["args"]]}}
        return json.dumps({"mcp": {"servers": servers} if client == "opencode" else servers},
                          ensure_ascii=False, indent=2)
    if client == "vscode":
        return json.dumps({"servers": {"screen-mentor": {"type": "stdio", **command}}},
                          ensure_ascii=False, indent=2)
    return json.dumps({"mcpServers": {"screen-mentor": command}}, ensure_ascii=False, indent=2)
