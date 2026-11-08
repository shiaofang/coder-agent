# 本地 GGUF 模型 API（llama-server + 隧道）

在本机用 [llama.cpp](https://github.com/ggml-org/llama.cpp) 的 `llama-server` 加载 GGUF，
提供 OpenAI 兼容接口；再用 Cloudflare 隧道（或 ngrok 等）把本机端口暴露成公网地址，
任何 SDK / curl / 客户端都能调用。**不绑定任何特定 IDE。**

```
客户端  →  https://xxxx.trycloudflare.com/v1  →  隧道  →  127.0.0.1:8080  →  llama-server  →  GGUF
```

---

## 这是什么

- 模型跑在本机（CPU 或 NVIDIA GPU），推理数据不出本机（除非经隧道调用）。
- 对外同时提供 OpenAI 兼容协议（`/v1/chat/completions`）和 Anthropic Messages 协议
  （`/v1/messages`），因此既能给任意 OpenAI 客户端用，也能直接接到 Claude Code。
- 隧道只做「本机端口 → 公网 https 地址」的映射，地址每次重启都会变。
- 默认**没有鉴权**，默认**关闭 llama-server 内置工具**——隧道开着时请务必看
  [安全](#安全)一节。

---

## 环境

| 项目 | 要求 |
|------|------|
| 系统 | Windows 10/11（Linux/macOS 用对应的 `llama-server` 二进制） |
| Python | 3.10+，`pip install -r requirements.txt`（只依赖 rich） |
| 引擎 | [llama.cpp Releases](https://github.com/ggml-org/llama.cpp/releases) 的解压内容放 `bin/` |
| 模型 | `models/` 下的 `.gguf`（视觉模型另需 mmproj） |
| GPU | 可选，NVIDIA + CUDA 版 llama-server |
| 隧道 | 自备 [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/) 或 [ngrok](https://ngrok.com/) |

### 目录结构

| 路径 | 说明 |
|------|------|
| `start.bat` / `chat.py` | 启动入口 |
| `agent/` | 选模型、启停 llama-server、终端提示 |
| `bin/` | llama-server 及其 DLL（不提交） |
| `models/` | GGUF 模型（不提交） |
| `config.json` | 本地运行配置，从 `config.example.json` 复制（不提交） |
| `.last_model` | 记住上次选择的模型 |

---

## 快速开始

### 1. 准备 `bin/`

把 llama.cpp Release 解压到 `bin/`。CUDA 版需同时放入同版本的 `cudart-*` 文件。

```bat
bin\llama-server.exe --version
```

### 2. 准备 `models/`

放入 `*.gguf`。带视觉的模型需要 mmproj 文件：

- 优先同名配对：`Qwen3.5-4B-Q4_K_M.gguf` → `Qwen3.5-4B-Q4_K_M.mmproj.gguf`
- 没有同名文件时，共用 `models/` 下任意 `mmproj*.gguf`（如 `mmproj-F16.gguf`）

`mmproj*.gguf` 不会出现在模型选择列表里。三值/二值量化（PTQ1_0 / PQ2_0）会被跳过。

### 3. 配置（可选）

```bat
copy config.example.json config.json
```

不建也能跑，全部用默认值。字段见 [config.json](#configjson)。

### 4. 启动

```bat
start.bat
```

或直接 `python chat.py`。流程：

1. 清理 `port` 上占用的进程
2. 选模型（回车 = 上次用过的）
3. 模型模板支持思考深度时，再问一次档位
4. 启动 llama-server（`-np 4` 多槽并发 + `--kv-unified` 共享上下文）并等待 `/health`
   （大模型加载最长等 10 分钟）
5. 打印本机 `/v1` 地址、Claude Code 的环境变量、隧道命令、安全提示

**Ctrl+C** 或关闭窗口会停止 llama-server。

### 5. 开隧道

另开一个终端：

```bat
cloudflared tunnel --url http://127.0.0.1:8080 --protocol http2
```

或者：

```bat
ngrok http 8080
```

记下输出的 `https://xxxx.trycloudflare.com`（每次重启都会变）。端口改过就同步替换 `8080`。

### 6. 调用

先确认本机服务：

```bat
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/v1/models
```

聊天（Windows cmd 语法）：

```bat
curl http://127.0.0.1:8080/v1/chat/completions ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"local\",\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}]}"
```

> cmd 里直接写中文有时会被按本地代码页编码，服务端会报
> `ill-formed UTF-8 byte`。中文提问建议用下面的 SDK，或把请求体存成 UTF-8 的
> `.json` 文件后 `--data-binary @body.json`。

换成隧道地址即可从外网调用：把 `http://127.0.0.1:8080` 替换成
`https://xxxx.trycloudflare.com`。

OpenAI Python SDK（`pip install openai`，不在 requirements 里）：

```python
from openai import OpenAI

# api_key 只是 SDK 的必填项，本服务不校验
client = OpenAI(base_url="https://xxxx.trycloudflare.com/v1", api_key="local")
resp = client.chat.completions.create(
    model="Qwen3.5-4B-Q4_K_M.gguf",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

本机测试时把 `base_url` 换成 `http://127.0.0.1:8080/v1` 即可。
支持 `stream=True` 流式输出。其他客户端同理：**Base URL 填 `https://…/v1`
（必须以 `/v1` 结尾），API Key 填任意非空值，模型名用 `/v1/models` 返回的 `id`。**

---

## 接口一览

| 路径 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 就绪状态：`{"status":"ok"}` |
| `/props` | GET | 实际 `n_ctx`、模型路径等 |
| `/v1/models` | GET | 模型 id（默认是文件名去掉 .gguf，与 `-a` 别名一致） |
| `/v1/chat/completions` | POST | OpenAI 兼容聊天，支持 stream |
| `/v1/messages` | POST | Anthropic Messages 兼容（Claude Code 用这个），支持 stream |
| `/v1/messages/count_tokens` | POST | Anthropic 口径的 token 计数 |
| `/tools` | GET | 内置工具列表，**默认关闭**（未开 `--tools` 时返回 403） |
| `/` | GET | llama-server 自带网页聊天界面 |

默认 `-np 4` + `--kv-unified`：4 个槽共享同一个 `-c` 上下文池，可并发处理多个请求。
改 `server.parallel` 可调；设为 `null` / `0` 则不传 `-np`，交给 llama-server 自动决定。

---

## config.json

| 字段 | 默认 | 说明 |
|------|------|------|
| `host` / `port` | `127.0.0.1` / `8080` | 监听地址与端口 |
| `server.ngl` | `null` | 放到 GPU 的层数；`null` = 交给 `--fit` |
| `server.ctx` | `null` | 上下文长度；`null` = 不传 `-c`，由模型标称值 / `--fit` 决定 |
| `server.fit_margin` | `384` | `--fit` 每卡预留显存（MiB） |
| `server.fit_ctx` | `null` | `--fit` 能把上下文缩到的下限（tokens）；`null` = 不传（llama 默认 4096） |
| `server.parallel` | `4` | 并发槽数（`-np`），同时还会传 `--kv-unified` 让各槽共享上下文；`null` / `0` = 不传 `-np`，交给 llama-server auto |
| `server.extra_args` | `[]` | 追加到启动命令末尾的参数 |

`ngl` / `ctx` 与 `--fit` 的关系：

- **两者都是 `null`**：启用自适应，传 `-fitt fit_margin`；`fit_ctx` 非空时再传 `-fitc`。
- **只有一个是 `null`**：显式填的那项照传，`null` 的那项交给 `--fit`。
- **两者都填了值**：不再传 `-fitt` / `-fitc`，此时 `fit_margin` / `fit_ctx` **完全无效**。

环境变量可覆盖：`CODER_AGENT_NGL`、`CODER_AGENT_CTX`。

---

## 内置工具（默认关闭）

`llama-server` 带一套内置工具，但**本项目默认不启用**——用 Claude Code 时它自带的工具已经够用，
再开一份会重复，还多一个 `exec_shell_command` 执行入口。

需要 llama-server 网页界面里的 Agent 时，在 `config.json` 的 `server.extra_args` 里打开：

```json
"server": { "extra_args": ["--tools", "all"] }
```

启用后可用：

| 工具 | 作用 |
|------|------|
| `read_file` / `write_file` / `edit_file` | 读写 / 修改文件 |
| `grep_search` / `file_glob_search` | 搜索文件内容与文件名 |
| `exec_shell_command` | 执行 shell 命令 |
| `get_info` | 环境信息 |

工具的工作目录是**项目根目录**。它们主要供 llama-server 自带网页界面的 Agent 使用；
任何能访问 `/tools` 的客户端也能调用。关闭时 `/tools` 返回 403。

---

## 安全

隧道会把接口暴露到公网临时域名，而本服务：

- **不校验 API Key**：拿到地址的人都能调用你的模型、占用你的 GPU。
- **默认不开内置工具**：没有 `exec_shell_command` 这类执行入口，风险面比之前小。
  自己用 `extra_args` 打开 `--tools` 后，这条风险会回来。

所以：

- 不用的时候立刻 `Ctrl+C` 关闭隧道，并关掉 `start.bat` 窗口。
- 不要把隧道地址发到公开地方。
- 不要在公网长时间挂着。

需要最基本的防护时，可在 `config.json` 里追加（参数会加在启动命令末尾）：

```json
"server": { "extra_args": ["--api-key", "<你的 key>"] }
```

之后调用需带 `Authorization: Bearer <你的 key>`。实测加了这个参数后，`/v1/*` 和 `/tools`
会返回 401，`/health` 仍然公开。

---

## 用 Claude Code 连接

`llama-server` 原生实现了 Anthropic Messages 协议（`/v1/messages`、
`/v1/messages/count_tokens`，含 SSE 流式、`tool_use`、`thinking`），所以 Claude Code
可以直接把它当模型后端，不需要适配层。

**Base URL 不带 `/v1`**（Claude Code 自己拼 `/v1/messages`），这点和 OpenAI 客户端相反。

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
export ANTHROPIC_AUTH_TOKEN=local          # 任意非空值，服务不校验
export ANTHROPIC_MODEL=claude-sonnet-4-5   # 见下方「模型名」
export ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-7
export ANTHROPIC_DEFAULT_SONNET_MODEL=claude-sonnet-4-5
export ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-haiku-4-5
export CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=34   # 34% × 200k ≈ 70k，压在本机 ctx 之下
export CLAUDE_CODE_MAX_OUTPUT_TOKENS=8192
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
```

Windows 可在启动器里 `set`，或换成 `ANTHROPIC_AUTH_TOKEN` → `ANTHROPIC_API_KEY`
（后者走 `x-api-key` 头，本服务两种头都接受）。

### 模型名

服务端认任何模型名（`/v1/models` 里的 `id` 也可，但 Claude Code 不认它），所以模型名由
**Claude Code 对 id 的识别**决定，而不是服务端：

- **可用**：Opus 4.8 之前的 id，如 `claude-sonnet-4-5`、`claude-opus-4-7`、
  `claude-sonnet-4-6`、`claude-haiku-4-5`。
- **不可用**：Opus 4.8 / Sonnet 5.5 / Haiku 5.5 / Fable 等新 id。这类 id 会让 Claude Code
  在对话中间插入 `role: "system"` 的提醒消息，而 Qwen3.5 的 chat template 明确禁止
  `System message must be at the beginning.`，服务端直接 500。实测换模型名无效，
  `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` / `CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING`
  也绕不过。

### 上下文

Claude Code 对不认识的模型 id 按 200k 估算上下文，而本机实际是 `config.json` 的
`server.ctx`（当前 70656）。用 `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` 把压缩点压到 200k×34%
≈ 70k 即可，不需要（也不建议）改 `CLAUDE_CODE_MAX_CONTEXT_TOKENS`。

### 已知限制

- **`/v1/messages/count_tokens` 可用**，`/usage`、`/context` 的 token 数是准的。
- **prompt caching 不影响正确性**：服务端接受 `cache_control` 但不回报缓存命中，
  Claude Code 每轮按未缓存计费（本地无成本，可忽略）。
- **工具并发**：4 个槽够主对话 + 标题 + 子代理并行；`-np 1` 会排队甚至超时。
- **速度**：RTX 2060 6GB + 4B Q4，Claude Code 一次 prefill 几万 token，首字几十秒属正常。

---

## 常见问题

| 现象 | 处理 |
|------|------|
| 窗口一闪而过 | 直接 `python chat.py` 看报错 |
| 缺 DLL / 闪退 | 检查 `bin/` 是否拷全；CUDA 版需 `cudart-*` |
| 显存不足 | `ngl` / `ctx` 都填了值时改小 `ctx`（如 8192），此时 `fit_margin` 无效；两者留 `null` 才是自适应模式 |
| 实际上下文比标称小 | `--fit` 按显存自动下调了，看 `/props` 的 `n_ctx` |
| 端口被占用 | 启动会结束占用 `port` 的进程；介意的话先改 `config.json` 的 `port` |
| 加载很慢 / 超时 | 大模型 + 慢盘常见，最长等 10 分钟；失败时会打印日志尾部 |
| 客户端 404 | OpenAI 客户端 Base URL 必须以 `/v1` 结尾；**Claude Code 反过来，必须不带 `/v1`** |
| 客户端连不上 | 先 `curl http://127.0.0.1:8080/health`，再确认隧道还在跑、端口一致；cloudflared 可加 `--protocol http2` |
| 隧道 502 | 本机服务没问题时，多半是隧道断了，重启隧道 |
| 返回 401 | 只有手动配了 `extra_args` 的 `--api-key` 才需要带 Key，且要一致 |
| 模型列表为空 | 只扫描 `models/*.gguf`；跳过 mmproj 与不支持的量化 |
| Claude Code 报 `System message must be at the beginning` | 模型名用了 Opus 4.8 之后的新 id，换成 `claude-sonnet-4-5` / `claude-opus-4-7` 这类旧 id |
| Claude Code 聊天到一半卡住 | 先看启动行是否 4 槽；`server.parallel` 为 `null`/`0` 时靠 auto，行为随二进制版本变 |
| Claude Code 老在压缩上下文 | 调 `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`（34 ≈ 70k）；别改 `CLAUDE_CODE_MAX_CONTEXT_TOKENS`，对不认识的 id 它按 200k 兜底更麻烦 |
