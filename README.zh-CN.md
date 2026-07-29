# cx2cc

中文文档 | [English](README.md)

cx2cc 是一个本地兼容代理：对外暴露 Anthropic Messages API 的一个常用子集，并将请求转发到 OpenAI Chat Completions 兼容的上游接口。

它主要用于 Claude Code、CC Switch，或其他支持自定义 Anthropic Base URL 的本地工具。

```text
Claude Code / CC Switch
        │ Anthropic 风格 /v1/messages
        ▼
http://127.0.0.1:8901
        │ OpenAI 风格 /chat/completions
        ▼
OpenAI-compatible upstream
```

## 功能特性

- 兼容 `POST /v1/messages`
- 兼容 `GET /v1/models`
- 支持流式 SSE 事件转换
- 支持 Claude Code 常见流程里的文本、图片、工具调用和工具结果转换
- 按会话生成稳定的 `prompt_cache_key`，让上游自动 prompt 缓存跨轮持续命中，并把缓存读取量回报给客户端
- 支持可选的上游备用 API Key，并带有限重试
- 提供 macOS arm64 / x86_64 原生可执行包和 LaunchAgent 管理脚本
- 提供 Windows x64 EXE 包、前台启动、静默启动和开机自启动脚本

## 兼容范围

cx2cc 不是 Anthropic 官方 API，也不实现 Anthropic API 的全部功能。

当前目标是覆盖 Claude Code / CC Switch 调用 `/v1/messages` 时常用的实用子集：

- 顶层 `system`
- 带文本块的 `messages`
- 用户图片块转换为 OpenAI `image_url`
- `tools` 转换为 OpenAI function tools
- `tool_choice` 的 `auto`、`any` 和指定工具
- assistant `tool_use` 和 user `tool_result`
- `max_tokens`、`temperature`、`top_p`、`stream`、`stop_sequences`
- 流式事件：`message_start`、`content_block_start`、`content_block_delta`、`content_block_stop`、`message_delta`、`message_stop`

已知限制：

- 只暴露 `/v1/messages`、`/v1/models` 和 `/health`。
- Batches、Files、token counting、Anthropic server-side tools、structured outputs、原生 thinking blocks 等能力尚未完整实现。
- Prompt 缓存依赖上游的自动缓存（见 [Prompt 缓存](#prompt-缓存)）；请求中的 Anthropic `cache_control` 标记会被忽略。
- 流式 token usage 取决于上游是否返回 usage chunk（代理会自动请求 `stream_options.include_usage`）；上游不返回时 input tokens 记为 `0`。
- 实际模型表现和工具调用可靠性取决于你配置的 OpenAI 兼容上游。

## Prompt 缓存

cx2cc 自身不缓存任何内容，但会让上游的自动 prompt 缓存持续生效并对客户端可见：

- 每个请求都携带稳定的 `prompt_cache_key`：对会话的 system prompt 和第一条 user 消息做 uuid5 哈希——同一会话各轮恒定、不同会话彼此不同，且不泄露 prompt 内容。OpenAI 风格后端用这个 key 做缓存亲和路由；没有它，同一会话的连续请求可能被负载均衡到不同缓存节点，导致明明存在的缓存无法命中。在真实 agentic 会话上，这就是逐轮命中率 ~13% 与 ~99% 的差别。
- 上游 `usage.prompt_tokens_details.cached_tokens` 会作为 `cache_read_input_tokens` 通过流式 `message_delta` 回报给客户端。
- 请求中的 Anthropic `cache_control` 标记会被接受但忽略；上游缓存是自动的，无需标注。

两个计量口径注意点（继承自 OpenAI usage 语义）：

- 回报的 `input_tokens` **已包含**缓存命中部分；`cache_read_input_tokens` 是它的子集而非额外量。按 Anthropic 语义求和（`input + cache_read`）的统计工具会把缓存命中重复计一次。
- `cache_creation_input_tokens` 恒为 `0`；OpenAI 风格上游没有"写缓存"计费。

如果你的上游会拒绝未知请求字段，设置 `CX2CC_PROMPT_CACHE_KEY=off` 关闭该功能。

## 环境要求

- 源码运行需要 Python 3.10+。Windows 和 macOS release 包均内含原生可执行文件，普通用户不需要安装 Python。
- 一个 OpenAI Chat Completions 兼容的上游接口
- 一个上游 API Key，可通过请求头 `x-api-key` 或 `Authorization: Bearer` 传入，也可配置为环境变量 fallback key

## Windows EXE 快速开始

从 GitHub Release 页面下载与版本对应的 `cx2cc-vX.Y.Z-windows-x64.zip`，解压后把 `.env.example` 复制为与 `cx2cc.exe` 同目录下的 `.env`。

```powershell
copy .env.example .env
notepad .env
```

然后双击 `cx2cc.exe`。不带参数时会打开一个简单的 GUI 窗口，可以：

- 查看代理是否在运行、上游是否已配置
- 启动后台服务
- 打开配置文件 `.env`
- 打开日志文件夹
- 复制本地 Base URL

命令行模式仍然可用：

```powershell
.\cx2cc.exe gui      # 打开 GUI（双击默认行为）
.\cx2cc.exe serve    # 前台运行服务
.\cx2cc.exe start    # 后台启动服务
.\cx2cc.exe health   # 打印健康检查
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8901/health
```

日志会写入 `cx2cc.exe` 同目录下的 `logs\`。关闭 GUI 不会停止用 `start` 启动的后台服务。

## macOS 可执行文件快速开始

根据 Mac 架构下载对应文件：Apple 芯片使用 `cx2cc-vX.Y.Z-macos-arm64.tar.gz`，Intel 芯片使用 `cx2cc-vX.Y.Z-macos-x86_64.tar.gz`。解压后进入目录并创建配置：

```bash
tar -xzf cx2cc-vX.Y.Z-macos-arm64.tar.gz
cd cx2cc-vX.Y.Z-macos-arm64
cp .env.example .env
```

编辑 `.env` 后可直接前台运行：

```bash
./cx2cc serve
curl http://127.0.0.1:8901/health
```

需要后台常驻时，使用包内 LaunchAgent 脚本：

```bash
./cx2cc.sh install
./cx2cc.sh start
./cx2cc.sh status
```

macOS 产物是原生 raw Mach-O 可执行文件，不是 `.app`，不提供 universal2，并且当前未签名、未公证。首次运行如被 Gatekeeper 阻止，请在确认文件校验值无误后，到“系统设置 → 隐私与安全性”中允许运行；不要从不可信来源下载或绕过校验。

## 校验发布文件

每个压缩包旁均提供 `.sha256` 文件。Windows PowerShell 可执行：

```powershell
Get-FileHash .\cx2cc-vX.Y.Z-windows-x64.zip -Algorithm SHA256
Get-Content .\cx2cc-vX.Y.Z-windows-x64.zip.sha256
```

macOS 可执行：

```bash
shasum -a 256 -c cx2cc-vX.Y.Z-macos-arm64.tar.gz.sha256
```

## 源码安装

```bash
git clone https://github.com/<owner>/cx2cc.git
cd cx2cc
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`，至少设置：

```env
CX2CC_UPSTREAM_BASE_URL=https://your-openai-compatible-host/v1
```

也可以配置备用 key：

```env
CX2CC_UPSTREAM_API_KEY=your-upstream-key
# 或
CX2CC_UPSTREAM_API_KEYS=key1,key2
```

如果客户端请求里带了 `x-api-key`，请求头里的 key 会优先于 `.env` 中的 fallback key。

## 从源码运行

```bash
python start-cx2cc.py serve
```

健康检查：

```bash
curl http://127.0.0.1:8901/health
```

## 配置 Claude Code / CC Switch

将 Anthropic Base URL 指向本地代理：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8901
```

然后按你的客户端支持的方式提供上游 key。常见方式：

```bash
export ANTHROPIC_AUTH_TOKEN=your-upstream-key
```

也可以在 `.env` 中配置 `CX2CC_UPSTREAM_API_KEY` / `CX2CC_UPSTREAM_API_KEYS` 作为 fallback key。

除非你明确希望其他机器访问本代理，否则保持 `CX2CC_HOST=127.0.0.1`。如果绑定到 `0.0.0.0`，请自行增加认证、防火墙或反向代理保护。

## macOS 后台服务

在源码 checkout 或解压后的 macOS release 目录中安装并启动 LaunchAgent：

```bash
chmod +x cx2cc.sh cx2cc-wrapper.sh
./cx2cc.sh install
./cx2cc.sh start
```

管理服务：

```bash
./cx2cc.sh status
./cx2cc.sh restart
./cx2cc.sh log
./cx2cc.sh stop
./cx2cc.sh uninstall
```

脚本会根据当前目录动态写入 `~/Library/LaunchAgents/com.cx2cc.proxy.plist`。生成的 plist 是本机文件，不应提交到仓库。

## Windows 使用

如果当前目录存在 `cx2cc.exe`，Windows 辅助脚本会自动使用 exe；如果是没有 exe 的源码目录，则 fallback 到 Python。

前台运行：

```bat
run.bat
```

或直接运行：

```powershell
.\cx2cc.exe serve
```

后台启动辅助脚本：

```bat
start-cx2cc.bat
```

或直接运行：

```powershell
.\cx2cc.exe start
```

静默 PowerShell 启动：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\start-cx2cc.ps1
```

安装开机自启动：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\install-startup.ps1
```

移除开机自启动：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\uninstall-startup.ps1
```

Windows 脚本会根据脚本自身位置定位 release/源码目录。release 包优先使用 `cx2cc.exe`；源码目录会优先尝试 `py -3`，再尝试 `python`，不依赖固定 Python 安装路径。

## 环境变量

| 变量 | 是否必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `CX2CC_UPSTREAM_BASE_URL` | 是 | 无 | OpenAI 兼容 API base URL，例如 `https://example.com/v1`。 |
| `CX2CC_UPSTREAM_API_KEY` | 否 | 无 | 请求未提供 `x-api-key` 时使用的备用上游 key。 |
| `CX2CC_UPSTREAM_API_KEYS` | 否 | 无 | 逗号或换行分隔的多个备用 key；优先于 `CX2CC_UPSTREAM_API_KEY`。 |
| `CX2CC_UPSTREAM_MODEL` | 否 | `gpt-5.5` | 每个请求向上游申请的模型名。 |
| `CX2CC_REPORT_UPSTREAM_MODEL` | 否 | 关 | 设为 `1`/`true`/`yes`/`on` 时，响应中报告上游实际服务的模型，而不是回显客户端请求的模型名。 |
| `CX2CC_PROMPT_CACHE_KEY` | 否 | 开 | 设为 `off` 时不再向上游发送会话级 `prompt_cache_key`。 |
| `CX2CC_HOST` | 否 | `127.0.0.1` | 本地监听地址。 |
| `CX2CC_PORT` | 否 | `8901` | 本地监听端口。 |

## 开发

```bash
python -m pytest
python -m compileall server.py translator.py start-cx2cc.py
```

在 Windows 上构建 EXE：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\build-exe.ps1
```

构建脚本默认生成 `dist\cx2cc-dev-windows-x64.zip` 和 SHA256 文件；可通过 `-Version vX.Y.Z` 指定版本名。

## 安全说明

- `.env`、日志、虚拟环境、构建缓存和 Git 元数据不会进入 release 包；不要自行提交或上传这些文件及 API Key。
- 代理会把 prompts 和工具结果转发给你配置的上游服务商。
- 默认情况下 cx2cc 只监听 `127.0.0.1`。
- `/health` 只报告上游是否已配置，不返回上游地址；日志和客户端错误也不记录或转发上游 URL、错误正文及完整 API Key。日志仍应保持私有。

## 许可证

MIT
