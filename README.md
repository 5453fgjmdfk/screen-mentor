<img src="codex-plugin/assets/app-icon.png" alt="屏幕伴学图标" width="88">

# 屏幕伴学 · Screen Mentor

Windows 桌面学伴。可以独立聊天和记事，也可以在已有的 Codex 对话里接着练习。同一个浮窗会显示步骤、键盘快捷键和可复制文字，点击和拖动由你自己完成。

## 下载与安装

从 [GitHub Releases](https://github.com/5453fgjmdfk/screen-mentor/releases) 下载 Windows ZIP 并解压。需要 Windows 和 Python 3.11+，安装时会下载独立环境的依赖。

- 双击 `install-desktop.cmd`，可独立使用桌面学伴。
- 打开 Codex 后双击 `install-codex.cmd`，可同时安装 Codex 插件。安装后完全退出并重新打开 Codex。

桌面快捷方式是「屏幕伴学 - 桌面学伴」和「屏幕伴学 - 外观管理」。日常聊天在「设置模型」里填兼容 Chat Completions 的地址和模型名。「记住：…」和「回忆：关键词」可以离线用。Codex 伴学沿用原对话的模型与用量。

## 版本回退

先退出托盘里的桌面学伴和 Codex，再从 Releases 或 Tags 下载目标版本到新目录并运行该版安装入口。回到桌面学伴后，已保存的对话、记忆和皮肤仍在用户目录的 `.codex/screen-mentor`。较早的标签提供 Codex 插件，安装步骤见该标签的说明。

公开包使用原创「窗边小龙」，支持导入自备皮肤。项目及公开素材使用 [MIT 许可证](LICENSE)。

[详细使用说明](codex-plugin/README.md) · [版本日志](codex-plugin/CHANGELOG.md)
