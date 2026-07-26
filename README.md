# Coder Agent

本地终端 AI 编程助手。基于 [llama.cpp](https://github.com/ggerganov/llama.cpp) 的 `llama-server`，在本机跑 GGUF 模型，通过 OpenAI 兼容的 tool calling 直接改文件、执行命令、搜索网页。

零第三方 Python 依赖：标准库即可运行，不依赖 LangChain / requests 等框架。

## 特性

- **本地推理**：模型与对话都在本机，不经过云端 API
- **真干活**：不只给步骤，会读改文件、跑命令、联网查报错
- **安全确认**：写文件 / 删文件 / 执行命令前可交互确认；支持 `/auto` 全程自动
- **精确编辑**：`edit_file` / 按行替换插入删除，避免整文件重写
- **常驻服务友好**：`npm run dev` 等会自动后台启动并返回访问地址

## 目录结构

```
coder-agent/
├── start.bat              # Windows 一键启动（选模型 → 起服务 → 进对话）
├── chat.py                # 启动入口（实现在 agent/）
├── config.example.json    # 配置模板（复制为 config.json）
├── config.json            # 运行时配置（含 Key，不上传 Git）
├── agent/                 # 终端客户端 + Agent 工具循环
│   ├── config.py          # 读取 config.json、斜杠命令、安全开关
│   ├── prompts.py         # 系统提示词
│   ├── project_context.py # 扫描 cwd 注入项目上下文
│   ├── tools_schema.py    # 给模型看的工具说明书
│   ├── terminal.py        # 颜色、横幅、确认菜单、输入
│   ├── paths.py           # Windows 路径解析
│   ├── tools.py           # tool_xxx 实现 + 调度
│   ├── model.py           # 与 llama-server 通信
│   ├── loop.py            # 多轮工具循环
│   └── main.py            # 主程序入口
├── bin/                   # 本地自备：llama-server 及 DLL（不上传 Git）
├── models/                # 本地自备：*.gguf 模型（不上传 Git）
├── .gitignore
└── README.md
```

**不要把 `bin/`、`models/` 提交到 Git。** 二者体积大且与本机硬件相关，已在 `.gitignore` 中忽略（含 `*.gguf` / `*.exe` / `*.dll`）。克隆仓库后需自行下载并放到对应目录，见下方说明。

## 环境要求

| 项目 | 说明 |
|------|------|
| 系统 | Windows（当前通过 `start.bat` 启动） |
| Python | 3.10+，已加入 PATH |
| GPU（可选） | NVIDIA 驱动；CUDA 包用于 `-ngl` 加速 |
| 模型 | 至少一个支持 tool calling 的 GGUF |

## 快速开始

克隆后本地自行创建目录（若不存在）：

```bat
mkdir bin
mkdir models
```

### 1. 准备 `bin/`（llama-server）

数据来源：[llama.cpp Releases](https://github.com/ggml-org/llama.cpp/releases)（官方预编译包）。

按机器选择资产并解压到 `bin/`：

| 场景 | 下载（名称随版本变化，以 Release 页为准） |
|------|------------------------------------------|
| 仅 CPU | `llama-b*-bin-win-cpu-x64.zip` |
| NVIDIA + CUDA 12 | `llama-b*-bin-win-cuda-12.*-x64.zip`，另下 `cudart-llama-bin-win-cuda-12.*-x64.zip` |
| NVIDIA + CUDA 13 | `llama-b*-bin-win-cuda-13.*-x64.zip`，另下 `cudart-llama-bin-win-cuda-13.*-x64.zip` |

把压缩包里的 `llama-server.exe` 以及同包（和 cudart 包）中的 DLL **全部放到 `bin/`**，最终至少要有：

```
bin/llama-server.exe
bin/*.dll          # ggml / llama / CUDA runtime 等
```

也可自行从 [llama.cpp](https://github.com/ggml-org/llama.cpp) 源码编译后，把产物拷进 `bin/`。

### 2. 准备 `models/`（GGUF）

数据来源：[Hugging Face](https://huggingface.co/) 上的 GGUF 仓库（搜索 `GGUF` + 模型名）。

推荐（本项目常用、支持 tool calling）：

| 模型 | 下载页 | 放入路径示例 |
|------|--------|--------------|
| Qwen3.5 4B Super Coder（Q4_0） | [jica98/qwen3.5-4B-super-coder](https://huggingface.co/jica98/qwen3.5-4B-super-coder) | `models/qwen3.5-4B-super-coder.Q4_0.gguf` |
| Qwen3.5 4B 官方量化系列 | [unsloth/Qwen3.5-4B-GGUF](https://huggingface.co/unsloth/Qwen3.5-4B-GGUF) | `models/Qwen3.5-4B-Q4_K_M.gguf` 等 |

下载 `.gguf` 文件后放到：

```
models/*.gguf
```

其它能跑 tool calling 的 GGUF 也可；体积与显存按量化档位自行选择。

可用 [Hugging Face CLI](https://huggingface.co/docs/huggingface_hub/guides/download) 下载，例如：

```bat
huggingface-cli download jica98/qwen3.5-4B-super-coder --local-dir models --include "*.gguf"
```

### 3. 启动

确认 `bin/llama-server.exe` 与至少一个 `models/*.gguf` 已就位后，在仓库根目录执行：

```bat
start.bat
```

流程：

1. 列出 `models/` 下的 GGUF，输入序号选择
2. 后台启动 `llama-server`（默认 `127.0.0.1:8080`）
3. 进入 `chat.py` 终端对话
4. 退出对话后自动关闭后台服务

### 手动启动（可选）

```bat
bin\llama-server.exe -m models\your-model.gguf --host 127.0.0.1 --port 8080 -ngl 99 -c 40000 --jinja
```

另开终端：

```bat
python chat.py
```

本地模式下 `config.json` 的 `host` / `port` 需与 `start.bat` 里启动的 llama-server 一致。

### 4. 统一配置文件 `config.json`

运行时配置（模型、搜索 Key 等）全部放在项目根目录的 **`config.json`**（已 gitignore）。复制模板：

```bat
copy config.example.json config.json
```

```json
{
  "provider": "cloud",
  "host": "127.0.0.1",
  "port": 8080,
  "base_url": "https://ollama.com",
  "model": "gpt-oss:120b-cloud",
  "api_key": "",
  "tavily_api_key": ""
}
```

| 字段 | 说明 |
|------|------|
| `provider` | `local`（本机 llama-server）或 `cloud`（OpenAI 兼容接口）；可被环境变量 `CODER_AGENT_PROVIDER` / `start.bat` 菜单覆盖 |
| `host` / `port` | 本地 llama-server 地址（`provider=local` 时使用） |
| `base_url` | 云端接口地址，**不带** `/v1`（代码会拼 `/v1/chat/completions`） |
| `model` | 请求体里的 `model`；云端通常必填 |
| `api_key` | 云端鉴权，作为 `Authorization: Bearer`；不需要则留空 |
| `tavily_api_key` | Tavily 搜索 Key；也可用环境变量 `TAVILY_API_KEY` |

常见云端场景：

| 场景 | `base_url` | `model` | `api_key` |
|------|-----------|---------|-----------|
| 本机 Ollama 转发云端模型（先 `ollama signin` + `ollama pull gpt-oss:120b-cloud`） | `http://127.0.0.1:11434` | `gpt-oss:120b-cloud` | 留空 |
| 直连 Ollama 云端 API | `https://ollama.com` | `gpt-oss:120b-cloud` | 在 [ollama.com/settings/keys](https://ollama.com/settings/keys) 生成 |
| OpenAI | `https://api.openai.com` | `gpt-4o` 等 | OpenAI API Key |

`start.bat` 菜单里选「Cloud model」会设 `CODER_AGENT_PROVIDER=cloud`（此时不启本地 llama-server）。也可手动：

```bat
set CODER_AGENT_PROVIDER=cloud
python chat.py
```

或直接在 `config.json` 写 `"provider": "cloud"` 后 `python chat.py`。云端模式不轮询本地 `/health`；配置缺失或缺字段时启动会提示原因并退出。

若仍有旧的 `cloud_config.json`，请重命名为 `config.json`。

## 使用说明

启动后直接用自然语言下任务，例如：

```
帮我在 C:\Users\...\Desktop\demo 创建一个最小 Vue3 + Vite 项目
把 src/App.vue 里的标题改成 Hello
查一下这个 TypeScript 报错怎么修
```

### 斜杠命令

| 命令 | 作用 |
|------|------|
| `/clear_cache` `/reset` `/new` | 清空对话上下文 |
| `/auto` | 全程自动执行写操作与命令（无需每次确认） |
| `/manual` | 恢复每次确认 |
| `/exit` `/quit` `/q` | 退出 |

其它：

- 写文件 / 命令前：方向键选择，Enter 确认
- `Ctrl+C`：取消当前任务；在提示符下再按一次退出

### 内置工具

| 工具 | 能力 |
|------|------|
| `read_file` / `write_file` / `edit_file` | 读写与精确文本替换 |
| `replace_lines` / `insert_lines` / `delete_lines` | 按行号编辑 |
| `delete_file` / `move_file` / `mkdir` / `list_dir` | 文件与目录操作 |
| `glob_search` / `grep_search` | 按路径模式 / 正则搜索 |
| `run_command` | 执行 shell；常驻服务自动后台 |
| `web_search` / `fetch_url` | 联网搜索（Tavily）与抓取正文 |
| `todo_write` / `todo_read` | 多步骤任务计划清单（会话内） |
| `get_datetime` | 当前本地时间 |

启动与 `/cd` 时会把当前目录的项目上下文注入 system 提示（`AGENTS.md` / `.cursorrules`、`package.json` scripts、git status、README 节选等），见 `agent/project_context.py`。

### 联网搜索（`web_search`）

`web_search` 使用 [Tavily](https://app.tavily.com) Search API（返回标题、链接与摘要）。

1. 申请 Key：https://app.tavily.com
2. 任选一种方式配置（环境变量优先于配置文件）：

```bat
set TAVILY_API_KEY=tvly-xxxxxxxx
```

或在 `config.json` 里填写 `tavily_api_key`。未配置时 `web_search` 会报错，可改用 `fetch_url`。

## 配置

**优先改 `config.json`**（见上方「统一配置文件」）。其余：

**`start.bat`**

| 变量 | 默认 | 含义 |
|------|------|------|
| `HOST` / `PORT` | `127.0.0.1` / `8080` | 本地 llama-server 监听地址（建议与 `config.json` 的 host/port 一致） |
| `NGL` | `99` | GPU 卸载层数（无 GPU 可改为 `0`） |
| `CTX` | `40000` | 上下文长度 |

**`agent/config.py` / `agent/prompts.py`（代码内常量）**

| 常量 | 含义 |
|------|------|
| `MAX_TOOL_ROUNDS` | 单轮任务最多工具调用次数 |
| `CONFIRM_TOOLS` | 需要用户确认的工具集合 |
| `SYSTEM_PROMPT` | 系统提示词（`prompts.py`） |

## 工作原理（简要）

```
用户输入
  → agent 发给本地 llama-server（/v1/chat/completions）
  → 模型返回 tool_calls 或最终回复
  → agent 执行工具，把结果写回 messages
  → 循环直到模型给出最终中文总结
```

工具声明与执行均为手写 OpenAI 兼容协议，无 Agent 框架。

## 安全提示

- 本工具可读写本地文件并执行任意命令，请只在可信环境使用
- 默认对写操作与 `run_command` 做确认；生产或共享机器上慎用 `/auto`
- 仅监听 `127.0.0.1`，不要随意改成公网暴露

## License

按需自行补充许可证。未声明前，请勿默认可用于商业分发。
