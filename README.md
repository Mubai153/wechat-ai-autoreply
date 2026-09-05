# 微信 AI 自动回复

一个运行在 Windows 本机的微信 4.x AI 自动回复工具。它可以同时监听最多三个指定联系人，分别读取各自最近的真实聊天记录作为上下文，通过 CC Switch 当前选中的 API 生成回复，并提供“仅预览”和“自动发送”两种模式。

> [!WARNING]
> 本项目不是微信官方接口，而是通过第三方适配器读取本机微信数据，并通过 Windows UIAutomation（UIA）操作微信。使用前请审查项目及第三方依赖源码，先用测试账号和“仅预览”模式验证。使用者需自行承担账号限制、误回复和隐私泄露等风险。

## 功能特性

- 最多同时监听三个指定联系人，避免误处理其他会话
- 默认直连 CC Switch 本地路由，不在项目中保存上游 API Key
- 在 CC Switch 的 Codex 页切换供应商后，下一条回复自动使用对应 API 和模型，无需重启本程序
- 仍可选用 Codex CLI、LM Studio 本地模型或支持 `/v1/chat/completions` 的 OpenAI 兼容接口
- 启动时及每次回复前读取真实双方聊天记录，并过滤程序自己的 `AI：` 回复
- 默认使用不移动鼠标的 UIAutomation 后台发送方式
- 提供桌面控制台、纯命令行模式和 Windows 可执行程序打包脚本
- 默认关闭自动发送，可先观察生成结果再决定是否启用
- 可选图片理解、回复冷却、语气画像、日志轮转和缓存自动清理

## 适用范围与限制

- 仅支持 **Windows** 和 **微信桌面版 4.x**，不能部署到 Linux 服务器、Docker 或没有登录微信的云主机。
- 微信和本程序必须运行在同一台电脑、同一个 Windows 用户会话中；锁屏、退出微信或微信升级都可能影响监听和发送。
- 当前最多可配置三个目标联系人，建议给每个联系人设置唯一备注名；多个备注名用逗号分隔。
- 文本消息可直接处理；普通图片需显式开启图片识别；动画表情和其他非文本消息会跳过。
- 自动回复统一带有 `AI：` 前缀，用于向对方说明消息来源并防止程序回复自己。
- 项目依赖非官方微信适配器，微信版本变化后可能需要等待适配器更新。

## 运行前准备

请先安装或准备以下软件：

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows，建议 Windows 10/11 |
| 微信 | 微信桌面版 4.x，并已登录 |
| Python | [Python 3.10 或更高版本](https://www.python.org/downloads/windows/)，建议 3.12；安装时勾选“Add Python to PATH” |
| Git | [Git for Windows](https://git-scm.com/download/win)，用于下载项目和第三方适配器 |
| 模型服务 | 推荐安装并配置 [CC Switch](https://github.com/farion1231/cc-switch)；也可使用 Codex CLI、LM Studio 或 OpenAI 兼容接口 |

可以在 PowerShell 中检查环境：

```powershell
python --version
git --version
```

默认的 CC Switch 模式需要先打开 CC Switch，在“设置 → 路由 → 本地路由”中开启总开关和 **Codex** 路由，并在 Codex 页选择一个供应商。默认监听地址为 `http://127.0.0.1:15721`。如果选择 `OpenAI Official`，还需要按 [Codex CLI 官方文档](https://learn.chatgpt.com/docs/codex/cli) 完成一次 ChatGPT 登录。

可以用以下命令检查 Codex 登录（仅 OpenAI Official 需要）：

```powershell
codex --version
codex login status
```

## 从零安装

以下命令均在 PowerShell 中执行。

### 1. 下载项目

```powershell
git clone https://github.com/Mubai153/wechat-ai-autoreply.git
Set-Location wechat-ai-autoreply
```

如果你通过 GitHub 下载了 ZIP，请先解压，再在 PowerShell 中进入解压后的项目目录。后续命令都假定当前目录是项目根目录，即能看到 `main.py` 和 `requirements.txt` 的目录。

### 2. 创建虚拟环境并安装依赖

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

命令直接使用虚拟环境中的 Python，因此不需要手动激活虚拟环境，也不会受 PowerShell 激活脚本策略影响。

### 3. 安装微信 4.x 适配器

```powershell
.\install_wechat_adapter.ps1
```

此脚本会把 [wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica) 的当前 `main` 分支下载到本地 `.vendor/` 并以开发模式安装。目前脚本没有固定到特定 commit，因此不同时间安装的上游代码可能不同。它会读取本机微信数据库并操作微信界面，请在运行前自行审查其源码。

如果 PowerShell 提示禁止运行脚本，可只为当前终端临时放行后重试：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install_wechat_adapter.ps1
```

### 4. 创建配置文件

```powershell
Copy-Item .env.example .env
notepad .env
```

`.env` 包含联系人、模型服务等本机配置，已被 `.gitignore` 忽略，不要提交或分享它。回复方案会单独保存到 `data/reply_profiles.json`，同样只保留在本机。

## 配置模型

在以下三种方式中选择一种。默认推荐 CC Switch。

### 方式 A：CC Switch（默认）

先在 CC Switch 中开启本地路由和 Codex 路由，再在 `.env` 中填写：

```dotenv
LLM_PROVIDER=ccswitch
CCSWITCH_BASE_URL=http://127.0.0.1:15721/v1
CCSWITCH_MODEL=
WECHAT_TARGETS=联系人A,联系人B,联系人C
# 旧版单联系人配置仍兼容；WECHAT_TARGETS 有值时优先使用它。
WECHAT_TARGET=联系人A
AUTO_SEND=false
```

`CCSWITCH_MODEL` 建议留空。程序会在每次生成前读取 CC Switch 管理的 `~/.codex/config.toml`，自动取得当前模型；因此在 CC Switch 的 **Codex** 页切换 API/供应商后，下一条微信回复就会使用新的供应商。上游 API Key 仍只保存在 CC Switch 中，项目只向本机路由发送请求。

如果 CC Switch 使用了自定义端口，再同步修改 `CCSWITCH_BASE_URL`。当前模式调用 Responses API，并由 CC Switch 自动转换到供应商实际使用的 Responses、Chat Completions 或 Anthropic 协议。

### 方式 B：Codex CLI

确保已经完成上面的 Codex CLI 登录，然后在 `.env` 中至少填写：

```dotenv
LLM_PROVIDER=codex_cli
CODEX_COMMAND=codex
CODEX_MODEL=
WECHAT_TARGETS=联系人A,联系人B,联系人C
AUTO_SEND=false
```

`CODEX_MODEL` 留空时使用当前 Codex 配置的默认模型。每条待回复消息都会启动一次临时、只读的 `codex exec`，因此会使用当前登录账号的 Codex 用量，并受到该账号可用模型、速率和使用额度的限制。

如果 `codex` 没有加入 PATH，也可以把 `CODEX_COMMAND` 改成 `codex.exe` 的绝对路径。

### 方式 C：LM Studio 本地模型

先在 [LM Studio](https://lmstudio.ai/) 中下载并加载模型，然后打开本地服务器（Developer → Start Server）。LM Studio 默认提供 OpenAI 兼容接口 `http://127.0.0.1:1234/v1`。

在 `.env` 中填写：

```dotenv
LLM_PROVIDER=lmstudio
LMSTUDIO_BASE_URL=http://127.0.0.1:1234/v1
LMSTUDIO_MODEL=
LMSTUDIO_API_KEY=lm-studio
```

在桌面控制台的“AI 与图片”页面，点击“刷新模型”即可自动读取 LM Studio 的本地模型列表，并从下拉菜单选择模型；也可以手动输入模型 ID。`LMSTUDIO_MODEL` 可以填写 LM Studio 页面或 `/v1/models` 返回的模型 ID；留空时程序会在第一次生成回复前自动选择返回列表中的第一个模型。LM Studio 必须保持服务器已启动并且已经加载模型。程序对 LM Studio 请求关闭隐藏思考，以避免 Qwen 推理模型耗尽回复字数上限；图片理解还需要加载支持视觉输入的模型。

### 方式 D：OpenAI 兼容接口

接口必须兼容 OpenAI Chat Completions。`LLM_BASE_URL` 填写到 API 版本层（通常以 `/v1` 结尾），不要填写完整的 `/chat/completions` 路径：

```dotenv
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://example.com/v1
LLM_API_KEY=替换为你的密钥
LLM_MODEL=替换为服务商提供的模型名
WECHAT_TARGETS=联系人A,联系人B,联系人C
AUTO_SEND=false
```

如果要识别图片，所选模型和服务还必须支持在 Chat Completions 中接收图片 Data URL。不同兼容服务的行为并不完全相同，请以服务商文档为准。

## 首次运行

首次运行请保持 `AUTO_SEND=false`，按以下顺序验证：

> [!IMPORTANT]
> “仅生成预览”只表示不会自动发送；点击界面的手动发送按钮仍会真实发出消息。程序仍会读取最近聊天记录并传给你选择的模型服务，默认最多读取 100 条。测试前请确认联系人和聊天内容适合交给该服务，必要时使用测试账号或减小 `MAX_HISTORY_MESSAGES`。

1. 启动微信桌面端并确认目标联系人可以正常收发消息。
2. 检查必填配置，并确认所选模型客户端可以初始化：

   ```powershell
   .\.venv\Scripts\python.exe main.py --check
   ```

3. 打开桌面控制台：

   ```powershell
   .\.venv\Scripts\python.exe main.py
   ```

4. 在界面中确认目标联系人和模型配置，保持“仅生成预览”，点击“开始监听”。
5. 让目标联系人发送一条测试消息，确认界面能收到消息并生成合理回复。
6. 如需发送，可在预览模式中手动发送当前回复；充分测试后再切换到“自动发送”。

`--check` 不会请求模型，也不会检查微信登录、联系人是否存在、数据库读取或发送链路。配置未完成时也可以先打开桌面控制台，在设置页面补齐并保存。

监听器启动时会把当前最新消息作为起点，只处理之后收到的新消息，不会自动回复启动前的历史消息。历史消息只会作为模型上下文使用。

GUI 打开时会按 `.env` 的 `AUTO_SEND` 选择初始模式，但必须点击“开始监听”才会运行。“回复方案”页可新建多套方案、通过下拉框切换编辑，并为每个监听联系人分配方案；方案保存在 `data/reply_profiles.json`，首次使用会把原 `.env` 中的回复规则作为“默认方案”。界面上的模式切换只影响当前运行，若要改变下次启动的默认值，请在设置页修改并保存 `AUTO_SEND`。命令行参数会覆盖本次运行的发送模式，但不会修改 `.env`。

## 日常使用

| 命令 | 行为 |
| --- | --- |
| `.\.venv\Scripts\python.exe main.py` | 打开桌面控制台 |
| `.\.venv\Scripts\python.exe main.py --gui` | 强制打开桌面控制台 |
| `.\.venv\Scripts\python.exe main.py --dry-run` | 无界面监听，只生成并记录回复，不发送 |
| `.\.venv\Scripts\python.exe main.py --send` | 无界面监听，并真实自动发送回复 |
| `.\.venv\Scripts\python.exe main.py --headless` | 无界面监听，是否发送由 `.env` 中的 `AUTO_SEND` 决定 |
| `.\.venv\Scripts\python.exe main.py --check` | 检查必填配置和模型依赖，不连接微信或请求模型 |
| `.\.venv\Scripts\python.exe main.py --test-send "测试内容"` | 添加 `AI：` 前缀后，向目标联系人真实发送该内容 |
| `.\.venv\Scripts\python.exe main.py --test-reply "测试提示"` | 不读取聊天历史，调用模型生成、添加 `AI：` 前缀并真实发送 |

> [!CAUTION]
> `--send`、`--test-send` 和 `--test-reply` 都会真实发送微信消息；`--headless` 在 `AUTO_SEND=true` 时也会真实发送。不要在联系人配置未确认时使用。

无界面模式可按 `Ctrl+C` 停止。修改 Python 代码、`.env` 或第三方适配器后，请停止并重启程序；已经运行的进程不会自动加载改动。

## 打包为 Windows 应用

完成依赖安装和 `.env` 配置后，在项目根目录执行：

```powershell
.\build_app.ps1
```

构建成功后会生成：

- `dist\微信自动回复\微信自动回复.exe`
- 项目根目录下的 `启动微信自动回复.lnk`

双击快捷方式或 EXE 即可打开桌面控制台。微信和 CC Switch（默认模式）需在运行该程序的电脑上保持运行；使用 OpenAI Official 时 Codex 登录也必须有效。

> [!IMPORTANT]
> 打包脚本会把当前 `.env` 和现有 `data/` 复制到应用目录，其中可能包含 API Key、联系人信息、聊天状态和图片缓存。应用目录中已有的 `.env` 会在重新打包时保留，避免覆盖 GUI 中已保存的回复指令。不要直接把自己构建的 `dist/` 发给他人。让其他使用者从源码自行配置和构建，或在分发前彻底检查并移除个人数据。

打包版日志位于 `dist\微信自动回复\logs\wechat_autoreply.log`；源码运行时日志位于项目根目录的 `logs\wechat_autoreply.log`。日志最大 2 MB，并保留 3 份轮转备份。

## 配置项说明

源码运行时配置写在项目根目录的 `.env` 中；打包运行时配置写在 EXE 旁边的 `.env` 中。

### 模型配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `LLM_PROVIDER` | `ccswitch` | `ccswitch`、`codex_cli`、`lmstudio` 或 `openai_compatible` |
| `CCSWITCH_BASE_URL` | `http://127.0.0.1:15721/v1` | CC Switch 的 Codex 本地路由地址 |
| `CCSWITCH_MODEL` | 空 | 留空时每次请求自动读取 CC Switch 当前模型；填写后固定模型 |
| `CODEX_COMMAND` | `codex` | Codex CLI 命令名或可执行文件绝对路径 |
| `CODEX_MODEL` | 空 | Codex 模型；留空使用 Codex 当前默认模型 |
| `LMSTUDIO_BASE_URL` | `http://127.0.0.1:1234/v1` | LM Studio 本地服务器地址 |
| `LMSTUDIO_MODEL` | 空 | LM Studio 模型 ID；留空自动选择 `/v1/models` 返回的第一个模型 |
| `LMSTUDIO_API_KEY` | `lm-studio` | LM Studio 通常不校验，仅用于满足 OpenAI SDK 的客户端参数要求 |
| `CODEX_TIMEOUT_SECONDS` | `120` | 单次 Codex 调用超时，最小 10 秒 |
| `LLM_BASE_URL` | 空 | OpenAI 兼容接口根地址，仅兼容接口模式必填 |
| `LLM_API_KEY` | 空 | 兼容接口密钥，仅兼容接口模式必填 |
| `LLM_MODEL` | 空 | 兼容接口模型名，仅兼容接口模式必填 |

### 联系人和发送配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `WECHAT_TARGETS` | 空 | 最多 3 个目标联系人的备注名或名称，用逗号、分号或换行分隔；必填 |
| `WECHAT_TARGET` | 空 | 旧版单联系人配置，`WECHAT_TARGETS` 为空时使用 |
| `AUTO_SEND` | `false` | 下次启动默认是否自动发送；建议保持关闭 |
| `WECHAT_BACKGROUND_MODE` | `true` | 使用 UIAutomation 后台发送，不移动鼠标 |
| `WECHAT_ALLOW_MOUSE_FALLBACK` | `false` | UIA 失败时是否允许回退到坐标/OCR 发送；回退可能移动鼠标 |

### 回复策略和本地数据

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `REPLY_COOLDOWN_SECONDS` | `0` | 两次程序回复的最短间隔；`0` 表示关闭冷却 |
| `MAX_HISTORY_MESSAGES` | `100` | 每次生成前读取的最近真实双方消息数；`0` 表示不读取历史 |
| `MAX_REPLY_CHARS` | `500` | 回复最大字符数，超出部分会截断 |
| `MAX_INPUT_CHARS` | `4000` | 输入最大字符数，过长消息会跳过 |
| `SYSTEM_PROMPT` | 见 `.env.example` | 回复语气、边界和行为指令 |
| `PERSONA_PATH` | 空 | 可选的 UTF-8 语气画像文件路径，可用相对路径或绝对路径 |
| `LOCAL_MEMORY_ENABLED` | `false` | 仅 LM Studio 启用本地聊天记忆；其他模型提供方不会读取或发送该数据 |
| `LOCAL_MEMORY_PATH` | `data/raw/my_wechat_messages.jsonl` | 本人历史发言的 JSONL 文件路径，由本地提取工具生成 |
| `LOCAL_MEMORY_MAX_RESULTS` | `6` | 每次回复最多检索的相关历史片段数 |
| `LOCAL_MEMORY_MAX_CHARS` | `800` | 注入本地模型上下文的历史片段总字数上限 |
| `DATABASE_PATH` | `data/wechat_autoreply.sqlite3` | 本地去重和回复状态数据库 |
| `LOG_LEVEL` | `INFO` | `DEBUG`、`INFO`、`WARNING` 或 `ERROR` |

### 图片识别和缓存

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `IMAGE_RECOGNITION_ENABLED` | `false` | 是否解密普通图片并交给视觉模型理解 |
| `MEDIA_RETENTION_DAYS` | `7` | 图片缓存保留天数；`0` 表示清理时删除现有缓存 |
| `MEDIA_CACHE_MAX_MB` | `512` | 图片缓存容量上限；`0` 表示不按容量限制 |
| `MEDIA_CLEANUP_INTERVAL_SECONDS` | `3600` | 运行期间清理间隔，程序内部最小为 60 秒 |

图片临时保存在 `data/media/`。自动清理只处理该缓存目录，不会删除微信原始数据库、聊天 JSON 或语气画像。普通图片（微信类型码 3）支持识别；动画表情（类型码 47）目前会跳过。

## 数据与隐私

- `.env` 可能包含 API Key、联系人和回复指令。
- `data/wechat_autoreply.sqlite3` 保存消息去重和回复时间等本地状态。
- `data/media/` 可能保存已解密的聊天图片缓存。
- `logs/` 可能包含联系人、消息内容、生成结果和错误信息。
- 正常监听生成回复前，项目会读取微信最近的真实双方聊天记录并传给所选模型服务；不读取历史的 `--test-reply` 除外。

上述路径默认不会被 Git 跟踪，但仍应限制本机文件访问权限，并在备份、打包或提交代码前检查是否包含个人信息。

## 常见问题

### CC Switch 调用失败

- 确认 CC Switch 正在运行，并在“设置 → 路由 → 本地路由”中同时开启总开关和 Codex 路由。
- 确认 `CCSWITCH_BASE_URL` 与 CC Switch 显示的本地地址一致，默认是 `http://127.0.0.1:15721/v1`。
- 必须在 CC Switch 的 **Codex** 页切换供应商；Claude Code 或 Gemini 页的选择不会影响本项目。
- 若当前供应商是 `OpenAI Official`，请重新完成 Codex 登录；第三方供应商的 Key 则在 CC Switch 中检查。
- 若不同供应商使用不同模型名，保持 `CCSWITCH_MODEL` 为空，让程序跟随 CC Switch 写入的当前模型。

### LM Studio 调用失败

- 确认已在 LM Studio 的 Developer 页面点击 **Start Server**，并且至少加载了一个模型。
- 在浏览器或 PowerShell 中检查 `http://127.0.0.1:1234/v1/models`；如果修改过端口，同步修改 `LMSTUDIO_BASE_URL`。
- 如果自动选择的模型不对，请把 `LMSTUDIO_MODEL` 填成 `/v1/models` 返回的准确 `id`。

### 提示“找不到 Codex CLI”

先运行 `codex --version`。如果 PowerShell 也找不到命令，请重新安装 Codex CLI 或将其加入 PATH；也可以在 `.env` 中把 `CODEX_COMMAND` 设置为 `codex.exe` 的绝对路径。

### 提示“未安装微信 4.x 适配器”

确认已经创建 `.venv`，然后重新运行：

```powershell
.\install_wechat_adapter.ps1
```

安装日志中如果出现 GitHub 下载错误，请检查网络、Git 和代理配置。

### 提示“找不到联系人”或“联系人匹配不唯一”

将 `WECHAT_TARGETS` 改成微信中显示的精确且唯一的备注名，多个联系人用逗号分隔。存在同名联系人时，先在微信里设置不同备注，再重启程序。

### 能生成回复，但没有发送

- 确认当前不是“仅生成预览”模式。
- 确认微信已登录、主窗口可以正常操作且电脑未锁屏。
- 查看日志中的 UIA 错误。
- 默认不会在 UIA 失败时抢占鼠标。如确实需要兼容旧发送方式，可设置 `WECHAT_ALLOW_MOUSE_FALLBACK=true`，但该模式可能移动鼠标并受窗口位置影响。

### 收不到新消息

- 确认目标联系人备注名正确，且消息确实由对方发来。
- 程序会忽略本机发出的消息、程序已经处理过的消息、不支持的消息类型和处于冷却期的消息。
- 微信 4.x 可能把同一会话分布到多个 `message_N.db` 分片；项目会合并读取这些分片，但升级代码或适配器后必须重启旧进程。
- 将 `LOG_LEVEL=DEBUG` 后重启，并查看 `logs/wechat_autoreply.log`。

### OpenAI 兼容接口调用失败

检查 `LLM_BASE_URL` 是否包含正确的 `/v1` 路径、Key 和模型名是否有效，以及服务是否支持 Chat Completions。部分只支持 Responses API 的服务不能直接用于本项目。

## 测试与开发

测试工具未包含在运行依赖中。首次测试前先安装 `pytest`，再运行不依赖真实微信连接的单元测试：

```powershell
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest -q
```

主要目录和文件：

```text
main.py                               程序入口和回复服务
gui.py                                Windows 桌面控制台
config.py                             .env 配置读取与校验
llm.py                                CC Switch、Codex CLI 和兼容接口调用
policy.py                             白名单、消息类型、长度和冷却策略
storage.py                            SQLite 去重和回复状态
image_recognition.py                  图片识别相关逻辑
media_cleanup.py                      图片缓存清理
wechat_autoreply/wechat_adapter.py    微信数据库、监听和发送适配层
wechat_autoreply/background_sender.py 无鼠标 UIAutomation 发送器
install_wechat_adapter.ps1            第三方微信适配器安装脚本
build_app.ps1                         PyInstaller 打包脚本
tests/                                单元测试
```

遇到问题时，请在仓库的 [Issues](https://github.com/Mubai153/wechat-ai-autoreply/issues) 中提供 Windows 版本、微信版本、Python 版本、运行模式和脱敏后的错误日志。请勿上传 `.env`、API Key、微信数据库、联系人信息或原始聊天内容。
