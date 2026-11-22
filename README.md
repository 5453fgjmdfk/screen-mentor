<img src="codex-plugin/assets/app-icon.png" alt="屏幕伴学图标" width="88">

# 屏幕伴学 · Screen Mentor

Windows 桌面学伴。可以独立聊天和记事，也可以在已有的 Codex 对话里接着练习。同一个浮窗会显示步骤、键盘快捷键和可复制文字，点击和拖动由你自己完成。

**当前版本 v0.21.0**。新增多 Agent 接入，修复屏幕框选并改善伴学时的角色切换和鲸鱼娘眨眼动画。

在管理面板可配置成员、对话、协作和各自的外观。在「模型」为每位学伴选择默认或独立接口。「设置 → Agent 接入」可复制其他 MCP 客户端的配置。「设置」可调整面板颜色、工作目录和关闭行为。

## 下载与安装

从 [GitHub Releases](https://github.com/5453fgjmdfk/screen-mentor/releases) 下载 Windows ZIP 并解压。需要 Windows 和 Python 3.11+，安装时会下载独立环境的依赖。

- 双击 `install-desktop.cmd`，可独立使用桌面学伴。
- 打开 Codex 后双击 `install-codex.cmd`，可同时安装 Codex 插件。安装后完全退出并重新打开 Codex。

桌面快捷方式是「屏幕伴学 - 桌面学伴」和「屏幕伴学 - 外观管理」。日常聊天在管理面板的「模型」页填兼容 Chat Completions 的地址和模型名。「记住：…」和「回忆：关键词」可以离线用。Codex 伴学沿用原对话的模型与用量。

## 版本回退

先退出托盘里的桌面学伴和 Codex，再从 Releases 或 Tags 下载目标版本到新目录并运行该版安装入口。回到桌面学伴后，已保存的对话、记忆和皮肤仍在用户目录的 `.codex/screen-mentor`。较早的标签提供 Codex 插件，安装步骤见该标签的说明。

公开包内置原创「窗边小龙」和同人皮肤「鲸鱼娘 · 海雾蓝」，也支持导入自备皮肤。项目代码、「窗边小龙」和应用图标采用 [MIT 许可证](LICENSE)。鲸鱼娘的角色图和动画采用 [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/deed.zh-hans)。[鲸鱼娘素材署名与改动](codex-plugin/assets/companion/whale-maid/SOURCE.md)

[详细使用说明](codex-plugin/README.md) · [版本日志](codex-plugin/CHANGELOG.md)
