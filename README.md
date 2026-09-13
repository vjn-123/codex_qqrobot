# Codex QQ Robot

> 当前只测试过 Windows 端可以运行。Linux 启动脚本已经提供，但暂未实际测试，Linux 用户请自行验证。

QQ Gateway 本地桥接器。通过 QQ 机器人向本机 Codex CLI 发送消息，用于远程写代码、查看项目和执行本机命令。

## 基础功能

- 接收 QQ 私聊消息和群聊中 @机器人的消息
- 调用本机 Codex CLI 生成回答或执行编码任务
- 支持 Codex 原生会话续接
- 支持切换 Codex 工作目录
- 支持查看、恢复和新建 Codex 会话
- 支持设置模型、思考强度和权限模式
- 支持任务排队、任务状态、取消任务和运行提醒
- 支持通过 `/cmd` 执行 Windows CMD 命令
- 支持通过 `/ps` 执行 Windows PowerShell 命令
- 支持接收 QQ 图片和文件附件
- 支持将本地图片发送回 QQ
- 支持限制允许使用机器人的 QQ 用户

## 工作方式

```text
QQ 消息
  -> QQ Gateway
  -> client/qq_gateway_client.py
  -> Codex CLI
  -> QQ Bot API
  -> QQ 消息回复
```

项目在本机运行，不需要公网回调地址或额外的中转服务器。

## 环境要求

Windows：

- Windows 10 或更高版本
- Python 3.10 或更高版本
- Node.js 和 npm
- 已安装并登录 Codex CLI
- QQ 机器人 AppID 和 AppSecret

Linux：

- Python 3.10 或更高版本
- 已安装并登录 Codex CLI
- QQ 机器人 AppID 和 AppSecret

Python 依赖只有 `websocket-client`，会安装到项目自己的 `.venv` 虚拟环境中。

## Windows 部署

### 1. 获取项目

```powershell
git clone https://github.com/vjn-123/codex_qqrobot.git
cd .\codex_qqrobot
```

### 2. 创建虚拟环境

```powershell
powershell -ExecutionPolicy Bypass -File .\setup-venv.ps1
```

脚本会完成以下操作：

- 在项目根目录创建 `.venv`
- 安装 `requirements.txt` 中的 Python 依赖
- 从 `client/.env.example` 创建 `client/.env`

### 3. 配置 QQ 和 Codex

编辑：

```text
client/.env
```

至少填写：

```env
QQ_APP_ID=你的QQ机器人AppID
QQ_APP_SECRET=你的QQ机器人AppSecret
CODEX_COMMAND=C:\Users\你的用户名\AppData\Roaming\npm\codex.cmd
CODEX_WORKDIR=C:\你的代码目录
```

`CODEX_COMMAND` 必须填写当前电脑实际可运行的 Codex CLI。也可以填写：

```env
CODEX_COMMAND=codex
```

但如果系统找不到 `codex`，建议改成 `codex.cmd` 的绝对路径。

QQ 机器人申请和配置入口：

```text
https://q.qq.com/#
```

### 4. 启动机器人

双击：

```text
start-qqrobot.cmd
```

或者在 PowerShell 中运行：

```powershell
.\start-qqrobot.cmd
```

启动窗口会保留运行日志。按 `Ctrl+C` 可以停止机器人。

也可以使用部署脚本：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

仅检查环境和配置：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 -CheckOnly
```

## Linux 部署

> Linux 端暂未测试，仅提供基础启动脚本。不同发行版可能需要额外安装 Python、虚拟环境或 Codex CLI 依赖。

```bash
git clone https://github.com/vjn-123/codex_qqrobot.git
cd codex_qqrobot
chmod +x setup-venv.sh start-linux.sh
./setup-venv.sh
```

编辑 `client/.env`：

```env
QQ_APP_ID=你的QQ机器人AppID
QQ_APP_SECRET=你的QQ机器人AppSecret
CODEX_COMMAND=codex
CODEX_WORKDIR=/home/你的用户名/projects
```

启动：

```bash
./start-linux.sh
```

Linux 端不使用 Windows 托盘程序，也不支持 Windows 专用的 `/cmd` 和 `/ps` 命令。

## 常用命令

以 `/` 开头的消息由机器人本地处理，不会发送给 Codex。

```text
/start
显示入口面板和快捷按钮

/help
显示所有命令

/status
显示机器人、Codex、工作目录和权限状态

/whoami
显示当前 QQ 用户的 openid

/workdir
显示当前工作目录和该目录下的历史会话

/workdir C:\path\to\project
切换 Codex 工作目录

/new [标题]
新建 Codex 会话

/resume
查看可恢复的 Codex 会话

/resume <会话ID>
恢复指定 Codex 会话

/model
查看当前模型和思考强度

/model gpt-5.5 high
设置模型和思考强度

/permission
查看当前权限模式

/permission read only
只读模式

/permission ask
执行高风险操作前请求批准

/permission auto
自动审批，允许 Codex 在无沙箱环境中执行

/permission full
完全访问权限，风险最高

/cmd dir
通过 Windows CMD 执行命令

/ps Get-ChildItem
通过 Windows PowerShell 执行命令

/tasks
查看运行中和排队中的任务

/cancel
取消当前任务

/restart
重启 QQ Gateway 客户端
```

普通消息会发送给 Codex。例如：

```text
查看当前项目有哪些文件，并告诉我项目如何启动
```

## 用户权限限制

启动机器人后发送：

```text
/whoami
```

复制机器人返回的 `user_openid`，写入 `client/.env`：

```env
QQ_ALLOWED_USER_OPENIDS=openid1,openid2
```

多个用户使用英文逗号分隔。

如果留空，所有能够向机器人发送消息的用户都可能使用机器人。由于机器人支持执行本机命令，建议配置允许用户列表。

## 重要配置

```env
# Codex CLI 命令
CODEX_COMMAND=codex

# 默认工作目录
CODEX_WORKDIR=C:\path\to\your\workspace

# native 表示使用 Codex 原生会话
CODEX_CONTEXT_MODE=native

# 默认模型和思考强度
CODEX_MODEL=gpt-5.5
CODEX_REASONING_EFFORT=xhigh

# read-only、ask、auto 或 full
CODEX_PERMISSION=read-only

# QQ Gateway 事件
QQ_GATEWAY_INTENTS=100663296
QQ_ALLOWED_EVENTS=C2C_MESSAGE_CREATE,GROUP_AT_MESSAGE_CREATE
```

如果需要使用按钮卡片，将 `INTERACTION_CREATE` 加入事件列表：

```env
QQ_ALLOWED_EVENTS=C2C_MESSAGE_CREATE,GROUP_AT_MESSAGE_CREATE,INTERACTION_CREATE
```

## 本地文件

```text
.venv/                  项目 Python 虚拟环境
client/.env             本机配置和密钥
client/data/            日志、附件、运行状态和会话索引
requirements.txt        Python 依赖清单
```

`.venv/`、`client/.env` 和 `client/data/` 已加入 `.gitignore`，不会提交到 GitHub。

## 安全提示

- 不要把 `client/.env` 提交到 GitHub
- 不要公开 `QQ_APP_SECRET`
- 不要公开用户 `openid`、日志和本地会话数据
- `/cmd`、`/ps` 以及 `CODEX_PERMISSION=auto/full` 可以操作本机文件和程序
- 如果 Secret 曾经泄露，请立即在 QQ 机器人控制台重新生成

## 项目结构

```text
client/
  codex_bridge_client.py       Codex 调用和会话管理
  qq_gateway_client.py         QQ Gateway 客户端
  qq_gateway_background.py     Windows 后台运行入口
  .env.example                 配置示例

requirements.txt               Python 依赖
setup-venv.ps1                 Windows 环境初始化
setup-venv.sh                  Linux 环境初始化
start-qqrobot.cmd              Windows 双击启动入口
start-linux.sh                 Linux 启动入口
start.ps1                      Windows 部署和启动脚本
```
