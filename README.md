# 微信自动回复

Windows 微信 4.x 指定联系人 AI 自动回复项目。

项目将本地微信适配器、回复策略、上下文存储和 LLM 接口分开，默认只监听一个联系人。默认模型层调用本机已登录的 Codex CLI，不需要 OpenAI API Key 或 Ollama。

## 重要提醒

这是个人微信桌面端自动化，不是微信官方个人号接口。微信必须在本机登录；第三方适配器可能读取本地聊天数据库并通过 UI/OCR 发送消息。请先审查源码、使用小号测试，并自行承担账号和隐私风险。

## 安装

在 PowerShell 中执行：

```powershell
cd "C:\Users\25754\Desktop\工作集\微信自动回复"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

然后安装微信 4.x 适配器。该脚本会从 GitHub 克隆第三方项目并安装，请先阅读其源码：

```powershell
.\install_wechat_adapter.ps1
```

## 配置

复制配置模板：

```powershell
Copy-Item .env.example .env
notepad .env
```

先确认 Codex CLI 使用 ChatGPT 登录态：

```powershell
codex login status
```

应显示 `Logged in using ChatGPT`。然后至少填写：

```dotenv
LLM_PROVIDER=codex_cli
CODEX_COMMAND=codex
WECHAT_TARGET=对方在微信中的唯一备注名
```

Codex CLI 会使用当前 Codex 配置中的默认模型；如需指定模型，可填写 `CODEX_MODEL`。每条消息都会启动一次临时、只读的 `codex exec` 会话，使用你的 ChatGPT/Codex 套餐额度，调用速度和可用次数受账户限制。

### 无鼠标发送

发送默认使用 UIAutomation 后台路径：通过 UIA 的 `ValuePattern` 和
`InvokePattern` 写入/发送，不调用坐标点击，也不会移动鼠标；发送器会在多条回复
之间复用同一个 UIA 会话。若微信版本暂时没有暴露输入框，日志会明确提示失败，
不会悄悄抢鼠标。只有在 `.env` 中显式设置
`WECHAT_ALLOW_MOUSE_FALLBACK=true` 才允许回退到旧的坐标/OCR 兼容路径。

### 可选：兼容接口

如果以后改用其他兼容服务，可配置：

```dotenv
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://你的服务商地址/v1
LLM_API_KEY=你的服务商Key
LLM_MODEL=服务商提供的模型名
```

第一次运行保持：

```dotenv
AUTO_SEND=false
```

此时只生成回复并打印日志，不会发送。确认监听对象和生成内容都正确后，改为 `AUTO_SEND=true`，或者使用一次性覆盖参数：

```powershell
python main.py --send
```

## 运行

先启动并登录微信桌面端，再运行：

```powershell
python main.py --dry-run
```

确认无误后自动发送：

```powershell
python main.py --send
```

## 打包为可点击应用

在项目目录运行一次：

```powershell
.\build_app.ps1
```

之后双击项目目录中的 `启动微信自动回复.lnk`，或双击 `dist\微信自动回复\微信自动回复.exe`，即可启动整套服务。应用会打开一个日志窗口；请保持微信桌面端和 Codex CLI 登录状态。`.env` 会被复制到应用目录，修改配置后需要重新复制配置或重新打包。

打包版同时把关键日志写入 `dist\微信自动回复\logs\wechat_autoreply.log`，日志文件最大 2 MB，并保留 3 份轮转备份。即使日志窗口意外关闭，也可以从这里排查最近一次启动和回复结果。

配置中的 `REPLY_COOLDOWN_SECONDS` 可限制连续回复频率，设为 `0` 表示关闭冷却、每条新消息都允许回复；SQLite 数据保存在 `data/wechat_autoreply.sqlite3`。不要把 `.env`、数据库、聊天日志提交到 Git。

## 检查和测试

不连接微信检查配置：

```powershell
python main.py --check
```

打包后可用以下参数分别诊断“微信发送”和“Codex 生成 + 微信发送”。这两条命令都会向 `.env` 中的目标联系人实际发送消息，请只在明确需要端到端测试时使用：

```powershell
.\dist\微信自动回复\微信自动回复.exe --test-send "发送链路测试"
.\dist\微信自动回复\微信自动回复.exe --test-reply "请简短回复：测试通过"
```

运行单元测试：

```powershell
python -m pytest -q
```

如果修改过适配器代码，必须先在正在运行监听器的窗口按 `Ctrl+C`，再重新执行启动命令；已经运行的 Python 进程不会自动加载新代码。正常收到新消息时，终端应依次出现“收到消息”“已加入回复队列”“开始调用 Codex 生成回复”和“已自动回复”。

微信 4.x 可能把同一会话分布在多个 `message_N.db` 分片中。本项目已在自己的适配层合并这些分片；如果仍在使用旧进程，监听器会继续只读旧分片，因此重启是必要的。

监听器只处理 `origin_source=2` 的对方来信，并忽略 `origin_source=1` 的本机发送消息，防止自动回复把自己刚发出的 `AI：` 消息再次当作新消息而形成循环。

## 目录

```text
main.py                         服务入口
config.py                       环境变量配置
llm.py                          Codex CLI 或兼容接口
storage.py                      SQLite 去重和上下文
policy.py                       联系人白名单、消息过滤、冷却
wechat_autoreply/wechat_adapter.py  微信 4.x 适配器
wechat_autoreply/background_sender.py  不移动鼠标的 UIAutomation 发送器
install_wechat_adapter.ps1     安装第三方微信适配器
tests/                          不依赖微信的单元测试
data/                           本地数据库目录
```
