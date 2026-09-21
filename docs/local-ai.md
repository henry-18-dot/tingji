# 交给本地 AI 部署听记

准备 Windows 电脑、能读写文件和运行命令的 AI，以及自己导出的笔记或转写文字。本地启动器使用 Windows 账户加密，其他系统需要先适配启动配置。

![把指令交给 AI、下载并安装、打开本机网页](images/local-ai-flow.svg)

## 复制部署指令

将下面整段复制给本机 AI。不要在聊天中附上 API 密钥。

```text
请帮我在自己的电脑上部署听记，源码：https://github.com/henry-18-dot/tingji 。

先检查操作系统、现有 Python 和目标目录。读取仓库的 tingji-web/docs/local-app.md、tingji-web/scripts/start-local.py、tingji-web/requirements.txt 和 tingji-web/tingji_web/config.py，按当前代码实施。本地启动器依赖 Windows DPAPI；如果不是 Windows，先说明需要的适配，不要直接运行它或改成公开服务。

把源码和独立 Python 环境装到我选定的新目录。保留已有文件和数据，不连接原网站的数据库，也不寻找开发者凭据。优先采用 Python 3.12，安装 tingji-web/requirements.txt 中的依赖。FFmpeg、LibreOffice、OCR 工具按实际需要安装；大型下载或付费操作先说明。

先在不调用付费服务的模式下完成安装和文字导入。TINGJI_DATA_DIR 和启动器 --data-dir 都指向本次新建的私有数据目录，避免读取任何旧库。先运行 --check；初次用 --no-worker 启动，只监听 127.0.0.1，默认端口 8765。不开放防火墙、端口转发或公网入口，不创建开机自启。

复用 tingji-web/scripts/import-legacy.py 和 tingji_web/legacy_import.py 的 transcripts_only 文字导入流程。先检查我提供的导出文件；普通 TXT 可在保留原件后转换为现有 tingji-transcripts-v1 格式并计算规定的内容哈希，再用明确的本地账号和目标数据库试读、导入。不要把任意文件当作已符合格式的导入包，不覆盖已有笔记，不在验收时自动整理全文。

需要 AI 整理时，说明如何在本机受限配置中填写我自己的 DEEPSEEK_API_KEY。不要让我把密钥发到聊天，不打印密钥，也不提交到 Git。录音转写另需火山 ASR、私有 TOS 和 FFmpeg；先说明上传的数据与费用，由我决定是否启用。不要许诺完全离线的 AI 整理或免费转写。

最后检查网页能打开、导入的文字能阅读和下载、数据确实保存在指定目录。告诉我网页地址、启动与停止方式、备份位置，以及哪些功能还未配置。保留原文。完成这些检查就停止。
```

## 给 AI 的补充例子

指定新目录：

```text
源码放在 D:\Apps\tingji，数据放在 D:\Private\tingji-data。先检查两个目录是否已存在；有内容就保留，并另选新目录。只在我的电脑上打开网页，首次暂停后台任务。
```

导入已有文字：

```text
我已把自己导出的转写文字放在 D:\Private\tingji-export。先检查文件格式，再按仓库的 transcripts_only 流程试读。向我展示条目数、重复项和目标本地账号，然后追加导入；原件保留。不要自动调用 AI 整理或重新转写录音。
```

安装后打开 [本机听记](http://127.0.0.1:8765)。这个链接只有本机服务启动后才可访问；端口有冲突时使用 AI 告知的新地址。[手动启动与备份](../tingji-web/docs/local-app.md)提供具体命令。

## 数据和服务

| 功能 | 需要什么 |
| --- | --- |
| 打开界面、阅读、导入文字 | 本机环境和自己的导出文件 |
| AI 整理、课件翻译与匹配 | 自己的 DeepSeek 凭据，按请求计费 |
| 录音转写 | 自己的火山 ASR、私有 TOS、FFmpeg；录音会上传至配置的服务 |
| PPT 转换、图片文字识别 | 按实际功能安装 LibreOffice、OCR 运行依赖 |

`--no-worker` 暂停后台队列，首次验收还应不配置服务密钥、不点击需要 AI 的操作。确认凭据和费用后，再由本机 AI 协助启用后台处理。

关闭服务后备份整个私有数据目录。Windows 加密文件与当前账户绑定，换电脑时需要重新配置凭据；录音、原文、数据库和图片应一并保留。
