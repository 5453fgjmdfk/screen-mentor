# 屏幕伴学 · Screen Mentor

屏幕伴学是一个 Windows 上的 Codex 插件。可以在原来的学习聊天里接着做实操。Codex 看你指定的练习窗口，在软件上叠加鼠标穿透的提示卡和演示，你自己操作。

## 能做什么

- 用鼠标动画演示点击和拖动。拖动落点用圆圈标示。
- 指向输入框，在桌面面板提供可复制的内容，也能在屏幕上显示快捷键。
- 跟随普通桌面软件的新窗口。在面板上暂停、继续、结束，或者把问题和截图发回原聊天。

## 安装

需要 Windows、Python 3.11 或更新版本，以及已登录的 Codex 桌面版。

在 Codex 运行时，从仓库根目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\codex-plugin\install.ps1
```

安装程序会创建独立的 Python 环境并注册本地插件。完成后退出并重开 Codex。插件目录里的本地来源叫 **Screen Mentor Local**。

## 开始练习

在已有的学习聊天里使用「屏幕伴学」。例如：

> 用屏幕伴学，接着刚才的内容带我在 Proteus 里练习。我要自己操作。

启动后切回练习窗口，跟着画面提示操作即可。若原聊天中断，在原聊天说「继续伴学」。打开其他软件之前，可以在开始时告诉 Codex 需要跟随哪些应用。

插件只观察获准的练习窗口，画面作为工具结果进入当前 Codex 对话。插件本身不另存截图文件。视觉判断使用当前 Codex 会话的模型和用量，无需单独配置模型 API Key。

## 源码

- `codex-plugin/` 包含 Codex 插件配置、练习面板和窗口跟踪。
- `overlay.py` 实现透明引导层。
- `capture.py` 提供 Windows DPI 初始化及截图辅助功能。

项目使用 MIT 许可证。
