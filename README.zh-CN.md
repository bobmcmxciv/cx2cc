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
- 支持可选的上游备用 API Key，并带有限重试
- 提供 macOS LaunchAgent 管理脚本
- 提供 Windows EXE 包、前台启动、静默启动和开机自启动脚本

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
- Batches、Files、token counting、prompt caching、Anthropic server-side tools、structured outputs、原生 thinking blocks 等能力尚未完整实现。
- 流式 token usage 取决于上游返回的 chunk；流式模式下 input tokens 可能显示为 `0`。
- 实际模型表现和工具调用可靠性取决于你配置的 OpenAI 兼容上游。

## 环境要求

- 源码运行需要 Python 3.10+。Windows release zip 内含 `cx2cc.exe`，普通用户不需要安装 Python。
- 一个 OpenAI Chat Completions 兼容的上游接口
- 一个上游 API Key，可通过请求头 `x-api-key` 传入，也可配置为环境变量 fallback key

## Windows EXE 快速开始

从 GitHub Release 页面下载 `cx2cc-windows-x64.zip`，解压后把 `.env.example` 复制为与 `cx2cc.exe` 同目录下的 `.env`。

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

在当前 checkout 下安装并启动 LaunchAgent：

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

构建脚本会生成 `dist\cx2cc-windows-x64.zip` 和 SHA256 文件。

## 安全说明

- 不要提交或上传 `.env`、日志、API Key 或生成的自启动文件。
- 代理会把 prompts 和工具结果转发给你配置的上游服务商。
- 默认情况下 cx2cc 只监听 localhost。
- 日志不会打印完整 API Key；但上游错误体仍可能包含敏感上下文，因此 `logs/` 仍应保持私有。

## 许可证

MIT
