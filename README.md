# 听记

本地录音转写与知识笔记工具。支持多段录音整理、课表关联、离线阅读和编辑。

笔记采用短句、列表和公式。章节可折叠；点击陌生词，可查看讲解、复制追问。阅读设置支持术语配色和公式变量联动，正文可显示 Mermaid 关系图与从属树。

## 启动

Windows 安装 Python 3.11 或更高版本、FFmpeg，并将 `python`、`ffmpeg`、`ffprobe` 加入 PATH。在项目目录运行：

```powershell
python -m pip install -r requirements.txt
powershell -NoProfile -ExecutionPolicy Bypass -File .\launcher.ps1
```

之后可双击「打开听记.cmd」或「听记.vbs」。服务地址是 http://127.0.0.1:8765/。

在「设置」填写豆包语音、DeepSeek 和私有 TOS 存储凭据。异步转写使用 `volc.seedasr.auc`；知识笔记默认使用 `deepseek-flash`。这些服务按各自账户计费。图片课表识别另需安装带中文语言包的 Tesseract；文字 PDF 可直接提取。

首次启动显示空课表，可在应用中导入。已有 `config/course-schedule.json` 时继续使用个人课表；已安装的桌面应用仍读取本机 `config/desktop-app.json`。

## 代码入口

| 路径 | 用途 |
| --- | --- |
| `server.py`、`tingji/` | 本地接口、转写、整理与数据保存 |
| `web/` | 录音、笔记、课表和阅读界面 |
| [完整提示词](docs/note-standard-v5/deepseek-system.md) | 本地知识笔记与提示词编辑页共用 |
| [正文样例](docs/note-standard-v5/style-reference.md) | 简洁中文讲解的格式参考 |
| [探索方向](docs/note-standard-v5/exploration-directions.md) | 文末问题的候选方向 |
| `tests/`、`web/qa/` | 后端测试与阅读组件检查 |

提示词编辑页： http://127.0.0.1:8765/prompt-lab.html 。在界面修改提示词也会改写上面的提示词文件，可用 Git 保存版本。录音整理产生的首次原文快照保存在本机 `data/prompt-lab/`。

运行后端测试：

```powershell
python -m unittest discover -s tests
```

## 版本管理

本仓库保存本地软件及通用提示词。录音、笔记、密钥、个人课表和本机配置保存在本地，已由 `.gitignore` 排除。云端网站单独维护。

改好并检查后，在项目目录提交：

```powershell
git add .
git commit -m "说明这次改动"
git push
```

KaTeX、Mermaid 的前端依赖随项目保存，许可证见 `web/vendor/`。
