"""The MCP tool contract shared by the desktop and its lightweight adapters."""
import math


VERSION = "0.12.0"
COPY_TEXT = {"type": "string", "maxLength": 4000, "description": "用户需要复制粘贴的原文。只传实际输入内容。"}
POLL_WAIT_SECONDS = 40
SESSION_KEY = {"type": "string", "description": "本条对话从 start_live 收到的 practice_id。"}
PROGRESS_PHASES = {"observed": ("已看到", "reading"),
                   "checking": ("正在核对", "thinking"),
                   "concluded": ("判断小结", "thinking")}


def tool(name, description, properties, required=(), *, read_only=False):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object", "properties": properties,
            "required": list(required), "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": read_only, "destructiveHint": False, "openWorldHint": False},
    }


TOOLS = [
    tool("open_management", "打开本机屏幕伴学管理面板。用于管理成员、群聊、模型和桌面闲聊。", {}),
    tool("open_appearance", "用户明确想换肤、编辑外观或打开外观管理器时，打开本机伴学外观窗口。", {}),
    tool("start_live", "启动当前对话的伴学。恢复现有伴学时传原 practice_id。",
         {"window_id": {"type": "string", "pattern": "^[0-9]+$"}, "practice_id": SESSION_KEY,
          "additional_window_ids": {"type": "array", "maxItems": 8,
                                    "items": {"type": "string", "pattern": "^[0-9]+$"},
                                    "description": "用户已经指定要一起跟踪的其他软件窗口。"}}, ("window_id",)),
    tool("end_live", "结束本条对话的实时伴学并回到桌面学伴。用户明确要求保存学习小结时才传 summary。",
         {"practice_id": SESSION_KEY, "summary": {"type": "string", "maxLength": 1200}}, ("practice_id",)),
    tool("wait_for_practice_change", f"等待最多 {POLL_WAIT_SECONDS} 秒，返回新画面、问题、等待或明确结束。等待超时不代表结束。",
         {"practice_id": {"type": "string"}}, ("practice_id",), read_only=True),
    tool("show_practice_hint", "在练习窗口所在的屏幕上显示穿透提示卡。快捷键传 shortcut；有位置的点击、输入或拖动优先用 show_guide。",
         {"practice_id": {"type": "string"}, "text": {"type": "string", "minLength": 1, "maxLength": 240},
          "shortcut": {"type": "string", "minLength": 1, "maxLength": 50}, "text_to_type": COPY_TEXT},
         ("practice_id", "text")),
    tool("show_practice_progress", "每完成一段有新信息的观察、核对或判断，立即在角色气泡更新一句面向用户的小结。",
         {"practice_id": {"type": "string"}, "text": {"type": "string", "minLength": 1, "maxLength": 110},
          "phase": {"type": "string", "enum": list(PROGRESS_PHASES),
                    "description": "已看到的事实 observed、正在核对的问题 checking，或可公开的判断小结 concluded。"},
          "snapshot_id": {"type": "string"}}, ("practice_id", "text")),
    tool("list_windows", "查找可用于伴学的桌面窗口。可按标题或软件名筛选。",
         {"query": {"type": "string", "maxLength": 120}}, read_only=True),
    tool("observe_window", "捕获明确选定的单个 Windows 窗口，返回图片和标注用的快照 ID。",
         {"window_id": {"type": "string", "pattern": "^[0-9]+$"}, "practice_id": SESSION_KEY},
         ("window_id",), read_only=True),
    tool("show_guide", "在软件画面上指出点击或输入位置。拖动时框选起点并传入 drag_to_x、drag_to_y，显示起点到落点的箭头。坐标使用截图像素。",
         {
             "snapshot_id": {"type": "string"},
             "practice_id": SESSION_KEY,
             "x": {"type": "number", "minimum": 0},
             "y": {"type": "number", "minimum": 0},
             "width": {"type": "number", "exclusiveMinimum": 0},
             "height": {"type": "number", "exclusiveMinimum": 0},
             "text": {"type": "string", "minLength": 1, "maxLength": 40},
             "detail": {"type": "string", "maxLength": 180},
             "input_field": {"type": "boolean", "description": "是文本输入框时传 true。保留卡片和指向箭头，隐藏框线。"},
             "drag_to_x": {"type": "number", "minimum": 0},
             "drag_to_y": {"type": "number", "minimum": 0},
             "text_to_type": COPY_TEXT,
             "step": {"type": "integer", "minimum": 1, "maximum": 999},
             "seconds": {"type": "integer", "minimum": 3, "maximum": 45},
         }, ("snapshot_id", "x", "y", "width", "height", "text")),
    tool("clear_guide", "清除屏幕标注并使当前快照失效。", {"practice_id": SESSION_KEY}),
]
TOOL_SCHEMAS = {item["name"]: item["inputSchema"] for item in TOOLS}


def discovery(request):
    method = request.get("method")
    if method == "tools/call":
        return None
    response = {"jsonrpc": "2.0", "id": request["id"]}
    if method == "initialize":
        supported = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
        params = request.get("params")
        version = params.get("protocolVersion") if isinstance(params, dict) else None
        response["result"] = {
            "protocolVersion": version if version in supported else supported[-1],
            "serverInfo": {"name": "screen-mentor", "version": VERSION},
            "capabilities": {"tools": {}},
        }
    elif method == "tools/list":
        response["result"] = {"tools": TOOLS}
    elif method == "ping":
        response["result"] = {}
    else:
        response["error"] = {"code": -32601, "message": "Method not supported"}
    return response


def validated_arguments(name, arguments):
    schema = TOOL_SCHEMAS.get(name)
    if schema is None:
        raise ValueError("未知的工具。")
    fields = schema["properties"]
    if not isinstance(arguments, dict) or set(arguments) - fields.keys() or set(schema["required"]) - arguments.keys():
        raise ValueError("工具的参数不完整，或包含不支持的参数。")
    for key, value in arguments.items():
        rule = fields[key]
        if rule["type"] == "string":
            if not isinstance(value, str) or not rule.get("minLength", 0) <= len(value) <= rule.get("maxLength", 128):
                raise ValueError(f"{key} 的文字长度无效。")
            if "enum" in rule and value not in rule["enum"]:
                raise ValueError(f"{key} 不是有效选项。")
            if key == "window_id" and not valid_window_id(value):
                raise ValueError("请使用 list_windows 返回的 window_id。")
        elif rule["type"] == "array":
            if not isinstance(value, list) or len(value) > rule["maxItems"] or not all(map(valid_window_id, value)):
                raise ValueError("请提供有效的窗口列表。")
        elif rule["type"] == "boolean":
            if type(value) is not bool:
                raise ValueError(f"{key} 必须为 true 或 false。")
        else:
            allowed = (int,) if rule["type"] == "integer" else (int, float)
            if type(value) not in allowed or abs(value) > 10**7 or not math.isfinite(value):
                raise ValueError(f"{key} 必须是有效数字。")
            if not rule.get("minimum", -math.inf) <= value <= rule.get("maximum", math.inf):
                raise ValueError(f"{key} 超出范围。")
            if "exclusiveMinimum" in rule and value <= rule["exclusiveMinimum"]:
                raise ValueError(f"{key} 必须大于零。")
    return arguments


def valid_window_id(value):
    return (isinstance(value, str) and 0 < len(value) <= 20 and value.isascii()
            and value.isdecimal() and 0 < int(value) < 2**64)
