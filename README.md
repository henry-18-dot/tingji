# 听记

录音转写、课堂笔记和课件阅读。当前应用代码位于 `tingji-web/`，可以部署到自己的 Windows 电脑，笔记与文件保存在自己指定的目录。

![把指令交给 AI、安装源码、打开本机网页](docs/images/local-ai-flow.svg)

**[交给本地 AI 部署 →](docs/local-ai.md)** 复制部署指令给能操作本机文件和终端的 AI。它会检查环境、安装依赖，并在 `http://127.0.0.1:8765` 打开应用。首次使用暂停后台任务，先验收网页和文字导入。

本地阅读与导入文字不需要模型服务。AI 整理需要自己的 DeepSeek 凭据；录音转写另需火山 ASR、私有 TOS 和 FFmpeg，按服务商账户计费。密钥只在本机填写，不发到聊天或提交到仓库。本地部署不代表 AI 完全离线运行。

| 路径 | 用途 |
| --- | --- |
| [部署指令与例子](docs/local-ai.md) | 交给本地 AI 安装、导入文字 |
| [手动启动说明](tingji-web/docs/local-app.md) | Windows 环境、启动、停止与备份 |
| `tingji-web/tingji_web/` | 当前接口、队列、转写与整理代码 |
| `tingji-web/public/` | 当前界面、使用指南与静态资源 |
| `tingji-web/tingji_web/default_prompt.md` | 整理提示词 |
| `tingji/`、`web/`、`server.py` | 保留的旧版代码；新版启动器仍使用 `tingji/storage.py` 的 Windows 加密 |

新安装请使用 `tingji-web/scripts/start-local.py`。根目录的旧启动器用于保留旧版环境。
