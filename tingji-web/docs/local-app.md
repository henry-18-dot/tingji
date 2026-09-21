# Windows 本地运行

也可以直接复制 [交给本地 AI 的部署指令](../../docs/local-ai.md)。当前启动器依赖 Windows DPAPI，只绑定本机地址，不需要开放网络端口。

## 安装与检查

先将仓库下载到一个新目录。以下命令在仓库根目录的 PowerShell 中运行，建议 Python 3.12。依赖包含 OCR 运行库，安装前确认下载量和可用空间。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\tingji-web\requirements.txt

# 将示例路径改成自己的新私有目录；不要指向已有听记数据。
$tingjiData = 'D:\Private\tingji-data'
if (Test-Path -LiteralPath $tingjiData) { throw '目录已存在，请另选新目录。' }
New-Item -ItemType Directory -Path $tingjiData | Out-Null
$env:TINGJI_DATA_DIR = $tingjiData
.\.venv\Scripts\python.exe .\tingji-web\scripts\start-local.py --data-dir $tingjiData --check
.\.venv\Scripts\python.exe .\tingji-web\scripts\start-local.py --data-dir $tingjiData --no-worker
```

初次检查不配置服务商凭据。`--check` 只检查配置，会在指定目录生成本机加密文件；它不启动网页或处理任务。`--no-worker` 用于首次界面和文字导入验收，暂停后台队列。

启动后访问 [http://127.0.0.1:8765](http://127.0.0.1:8765)。占用端口时添加 `--port 8766`，使用对应新地址。终端按 `Ctrl+C` 停止服务。

## 再次启动

打开新终端后，仍在仓库根目录运行；这次使用同一个数据目录。

```powershell
$tingjiData = 'D:\Private\tingji-data'
$env:TINGJI_DATA_DIR = $tingjiData
.\.venv\Scripts\python.exe .\tingji-web\scripts\start-local.py --data-dir $tingjiData --no-worker
```

需要 AI 功能时，先在本机受限配置中设置自己的凭据。配置项以 `tingji_web/config.py` 为准，当前启动器不会自动读取 `.env` 文件。不要把密钥写进上述命令、聊天或仓库。确认费用后去掉 `--no-worker`；队列中已有任务也会继续处理。

AI 整理使用 `DEEPSEEK_API_KEY`。录音转写另需火山 ASR、私有 TOS、FFmpeg；PPT 转换需要 LibreOffice。密钥未配置时先使用文字导入与阅读。

## 文字导入

仓库提供 `scripts/import-legacy.py` 和 `tingji_web/legacy_import.py`。它们接受旧版数据目录或 `tingji-transcripts-v1` 原文包；普通 TXT 需要先按已有格式转换，并保留原件。

导入前让本机 AI 明确设置目标 `DATABASE_URL`，核对本地账号并试读。默认只检查，加 `--apply` 才写入；使用 `transcripts_only` 追加导入，不触发付费整理。不要沿用任何网站数据库配置。

## 备份

停止服务后复制整个指定数据目录，包括数据库、录音、原文、图片和 `app-secret.dpapi`。数据文件留在私有位置，不提交到 Git。加密文件绑定当前 Windows 账户，换电脑后需重新配置凭据。

## 修改前端

仓库已包含配套静态资源，普通启动不需要 Node.js。修改前端后使用 Node.js 重新生成资源：

```powershell
node .\tingji-web\scripts\build-web-assets.mjs
node .\tingji-web\scripts\build-web-assets.mjs --check
```
