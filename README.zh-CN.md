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
- 兼容 `GET /v1/models`，并标注每个模型名实际由哪个模型服务
- `POST /v1/chat/completions`（别名 `/openai/v1/chat/completions`）：OpenAI Chat Completions 透传，给原生说 OpenAI 协议的客户端用
- `POST /v1/responses`（别名 `/openai/v1/responses`）：OpenAI Responses 透传，Codex CLI 0.135 起只发 Responses
- `POST /v1/alpha/search`：Codex CLI 0.158 起的独立网页搜索透传
- `POST /v1/images/generations` 与 `POST /v1/images/edits`：OpenAI Images 形状的出图与参考图编辑透传（上游提供 gpt-image-2 时可用），`gpt-image` skill 用它
- Claude Code 的 WebSearch 映射到上游原生 `web_search`，搜索结果还原成 `server_tool_use` / `web_search_tool_result` 块
- 模型解析：去掉 `[1m]` 之类的窗口后缀、别名表、放行名单并入上游目录、未知模型直接拒绝而不是悄悄替换
- `GET /usage` 透传上游的额度/限额 JSON，供 CC Switch 展示用量
- `GET /accounts` 透传上游的账号池视图，适用于上游用多个订阅轮换的场景
- 支持流式 SSE 事件转换
- 支持 Claude Code 常见流程里的文本、图片、工具调用和工具结果转换
- 按会话生成稳定的 `prompt_cache_key`，让上游自动 prompt 缓存跨轮持续命中，并把缓存读取量回报给客户端
- `message_start` 里带锚定式的提示词大小估算，Claude Code 的上下文用量条不再停在 0
- 工具结果里返回的图片，作为下一条 user 消息转给模型
- 可选的多用户网关（[`gateway/`](gateway/README.md)）：个人 API Key、权限范围、模型白名单、限流与额度、逐次调用审计和网页控制台
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

- 只暴露 `/v1/messages`、`/v1/chat/completions`、`/v1/responses`、`/v1/alpha/search`、`/v1/images/generations`、`/v1/images/edits`（OpenAI 协议接口另有 `/openai/v1/` 别名）、`/v1/models`、`/usage`、`/accounts` 和 `/health`。
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

## 模型解析

每个请求的模型名在发往上游前按同一套规则处理：

1. 去掉末尾的窗口标记，例如 `[1m]`。
2. 按 `CX2CC_MODEL_ALIASES`（`旧名=新名,旧名=新名`）改写，可以在不动客户端的情况下把钉在旧模型上的机器整体挪到新默认模型。
3. 结果在放行名单里就直接使用：`CX2CC_MODEL_PASSTHROUGH` 加上上游自己 `/models` 公布的模型（缓存十分钟）。
4. Claude Code 自带的模型名（`claude-*`、`opus`、`sonnet`、`haiku` 等）和不带模型的请求，使用 `CX2CC_UPSTREAM_MODEL`。
5. 其他名字返回 400 并列出可用模型（默认 `CX2CC_UNKNOWN_MODEL=reject`）；设为 `default` 则恢复旧行为，悄悄用默认模型服务。

`GET /v1/models` 转发上游目录，并标注本代理的处理：`default_model`、`aliases`、`unknown_model_policy`，以及被改写条目上的 `served_as`。`?refresh=1` 会转给上游以跳过缓存。设置 `CX2CC_REPORT_UPSTREAM_MODEL=true` 后，响应里写的是实际服务请求的模型。

## 网页搜索

Claude Code 的 WebSearch 会以强制工具调用的方式发送 Anthropic 托管搜索工具（`web_search_<日期>`）。cx2cc 把它映射到上游原生的 `web_search` 工具（`allowed_domains` 转成上游的 `filters`，`blocked_domains` 与 `max_uses` 没有对应项），再根据上游的搜索调用和 `url_citation` 标注，重建 `server_tool_use`、`web_search_tool_result` 块以及 `usage.server_tool_use`，流式与非流式都支持。Codex CLI 自己的搜索不需要映射：`/v1/responses` 与 `/v1/alpha/search` 都是逐字节透传。

## OpenAI 协议透传

给原生说 OpenAI 协议的客户端（SDK、Chatbox、Codex CLI 等）用的入口，与 `/v1/messages` 共用上游、Key 轮换和缓存 key 策略，但不做协议转换：

- `POST /v1/chat/completions`：请求体原样转发，只做三件事：按上面的规则解析模型；客户端没带时自动补上会话级 `prompt_cache_key`；流式请求补 `stream_options.include_usage`。响应逐字节返回。
- `POST /v1/responses`：Codex CLI 0.135 起只发 Responses，SSE 原样返回。
- `POST /v1/alpha/search`：Codex CLI 0.158 起在客户端侧调用独立搜索，请求与响应原样转发。
- `POST /v1/images/generations`、`POST /v1/images/edits`：OpenAI Images 形状，`edits` 额外带 `images`（最多 16 张 data URL 参考图，顺序有意义）。单张图约 15 秒，`n` 最大 4，超时 600 秒。
- 错误使用 OpenAI 的 `{"error": {"message", "type", "code"}}` 形状；`/v1/messages` 的回复风格段不会注入这些接口。

## 回复风格注入

cx2cc 会在每个 `/v1/messages` 请求的 system prompt 末尾追加一段风格约定。用 Claude 形态的 harness 驱动 OpenAI 风格模型时，输出容易碎片化——每次工具调用配一小段旁白，而不是成段的完整叙述；默认约定（`translator.DEFAULT_STYLE_PROMPT`，中文）针对性地要求：先说结论、成段写作、不为每个工具调用配旁白、不复述工具输出、结构服从内容、长度与信息量匹配。

- 风格段**只追加、不前置**：客户端原始 system prompt 保持字节级前缀不变，上游前缀缓存跨轮继续命中（改动文本后仅首个请求一次性 miss）。会话级 `prompt_cache_key` 只哈希客户端原始 system prompt，不受影响。
- 客户端完全没发 system prompt 时，风格段单独作为 system 消息发送。
- `CX2CC_STYLE=off` 关闭注入。
- `CX2CC_STYLE_PROMPT` 覆盖默认值：值是可读文件路径时用文件内容，否则按字面文本使用。

## 用量端点

`GET /usage`（别名 `GET /v1/usage`）把上游的用量/额度 JSON 转发给客户端，让 CC Switch 之类的工具通过 cx2cc 查看剩余额度，而不必直连上游。

- Key 处理与 `/v1/messages` 一致：调用方的 `x-api-key` / `Authorization: Bearer` 会作为上游 bearer token 透传，key 无效时返回上游的 `401`。
- 上游地址默认为配置的 base URL 去掉末尾 `/v1` 再拼上 `/usage`（`https://example.com/v1` → `https://example.com/usage`），可用 `CX2CC_USAGE_URL` 指向其他地址。
- cx2cc 不解释响应内容：上游 HTTP 200 返回什么 JSON，客户端就收到什么，字段结构由上游决定。上游没有用量端点时会返回 404，仅转发状态码。

在 CC Switch 中，在供应商卡片上启用用量查询，用自定义脚本请求 `{{baseUrl}}/usage`（请求头 `x-api-key: {{apiKey}}`），再按上游实际字段写提取逻辑即可。

`GET /accounts`（别名 `GET /v1/accounts`）是同样的透传，用于上游同时挂了多个订阅、需要知道当前由哪个账号服务的场景。查询串会一并转发，因此 `?usage=0` 之类的上游过滤参数经过 cx2cc 依然有效；上游没有该端点时返回 404，原样转发。

## 多用户网关

cx2cc 本身不认证任何人，只把调用方的 Key 转给上游。多人共用一个入口时，在前面加一层 [cx2cc-gateway](gateway/README.md)：每人一把 Key（别名、使用人、权限范围、可选的模型白名单、每分钟请求数 / 并发 / 每日 / 近 7 日额度、有效期、带宽限期的轮换、吊销），网关核验后换成 cx2cc 唯一认可的内部凭据转发，流式响应不缓冲地返回，每次调用写一条审计（谁、何时、从哪里、哪个模型、多少 token、耗时、失败原因，不记录提示词和回复）。`/admin/` 下的网页控制台有管理员、Key 管理员（可以创建 Key，只看得到自己创建的）和审计员三种角色，持 Key 的人也可以用自己的 Key 登录查看用量。网关是独立的 aiohttp 服务，自带测试和 Docker 镜像，cx2cc 本身不需要任何改动。

`site/index.html` 是部署根路径上的项目介绍页。

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
| `CX2CC_UPSTREAM_MODEL` | 否 | `gpt-5.5` | 默认上游模型：Claude 模型名、不带模型的请求，以及 `CX2CC_UNKNOWN_MODEL=default` 时的未知模型都用它。 |
| `CX2CC_MODEL_ALIASES` | 否 | 无 | `旧名=新名,旧名=新名` 形式的改写，在去掉窗口后缀之后、检查放行名单之前生效。 |
| `CX2CC_MODEL_PASSTHROUGH` | 否 | 无 | 逗号分隔的、客户端可以直接请求的模型名，与上游 `/models` 列表合并。 |
| `CX2CC_UNKNOWN_MODEL` | 否 | `reject` | `reject` 对未知模型名返回 400；`default` 用 `CX2CC_UPSTREAM_MODEL` 服务。 |
| `CX2CC_REPORT_UPSTREAM_MODEL` | 否 | 关 | 设为 `1`/`true`/`yes`/`on` 时，响应中报告上游实际服务的模型，而不是回显客户端请求的模型名。 |
| `CX2CC_PROMPT_CACHE_KEY` | 否 | 开 | 设为 `off` 时不再向上游发送会话级 `prompt_cache_key`。 |
| `CX2CC_STYLE` | 否 | 开 | 设为 `off` 时不再向 system prompt 追加回复风格段。 |
| `CX2CC_STYLE_PROMPT` | 否 | 内置 | 覆盖默认风格段：可读文件路径（取文件内容）或字面文本。 |
| `CX2CC_USAGE_URL` | 否 | 派生 | `GET /usage` 背后的上游地址；默认为 base URL 去掉 `/v1` 后缀再拼上 `/usage`。 |
| `CX2CC_HOST` | 否 | `127.0.0.1` | 本地监听地址。 |
| `CX2CC_PORT` | 否 | `8901` | 本地监听端口。 |

## 开发

```bash
python -m pytest
python -m compileall server.py translator.py prompt_estimate.py start-cx2cc.py
```

网关有自己的依赖和测试：

```bash
cd gateway
python -m pip install -r requirements-dev.txt
python -m pytest
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
