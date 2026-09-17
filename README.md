# Coder Agent

本地终端 AI 编程助手。基于 [llama.cpp](https://github.com/ggerganov/llama.cpp) 的 `llama-server`，在本机跑 GGUF 模型，通过 OpenAI 兼容的 tool calling 直接改文件、执行命令、搜索网页。

只依赖两个 Python 库（`rich` 渲染、`prompt_toolkit` 输入），不依赖 LangChain 等框架。

## 特性

- **本地推理**：模型与对话都在本机，不经过云端 API；启动时选 GGUF，`/model` 会话内随时切换
- **真干活**：不只给步骤，会读改文件、跑命令、（配了 Key 时）联网查报错
- **改动可审查**：写文件 / 改文件前显示彩色 diff，方向键确认；拒绝时可写一句原因告诉模型
- **上下文可见可控**：状态栏实时显示 ctx 用量；快满时自动折叠旧工具结果 / 总结旧对话，`/compact` 手动压缩
- **为小模型优化**：工具集精简到 14 个（≈1.5k token），≤5B 模型自动用更短的提示词，工具参数 JSON 容错修复，`/think off` 关闭思考提速
- **会话不丢**：每轮自动保存到 `sessions/`，`/resume` 恢复；输入历史 ↑↓ 可翻，支持多行输入
- **常驻服务友好**：`npm run dev` 等会自动后台启动并返回访问地址

## 目录结构

```
coder-agent/
├── start.bat              # Windows 一键启动（装依赖 → 运行 chat.py）
├── chat.py                # 启动入口（实现在 agent/）
├── requirements.txt       # rich, prompt_toolkit
├── config.example.json    # 配置模板（复制为 config.json）
├── config.json            # 运行时配置（含 Key，不上传 Git）
├── agent/
│   ├── config.py          # 读取 config.json、斜杠命令、安全开关、运行时状态
│   ├── server.py          # 选模型、启动/切换 llama-server、读 /props
│   ├── prompts.py         # 系统提示词（full / compact 两档）
│   ├── project_context.py # 扫描 cwd 注入项目上下文
│   ├── tools_schema.py    # 给模型看的工具说明书
│   ├── tools.py           # tool_xxx 实现 + 调度
│   ├── model.py           # 与模型服务通信、流式解析、统计
│   ├── context.py         # 上下文用量估算与压缩
│   ├── render.py          # rich 渲染：markdown / diff / 工具结果 / 统计行
│   ├── terminal.py        # 按键读取、确认菜单、prompt_toolkit 输入
│   ├── session.py         # 会话保存 / 恢复
│   ├── paths.py           # Windows 路径解析
│   ├── loop.py            # 多轮工具循环
│   └── main.py            # 主程序入口
├── bin/                   # 本地自备：llama-server 及 DLL（不上传 Git）
├── models/                # 本地自备：*.gguf 模型（不上传 Git）
├── sessions/              # 自动保存的会话（不上传 Git）
└── README.md
```

**不要把 `bin/`、`models/` 提交到 Git。** 二者体积大且与本机硬件相关，已在 `.gitignore` 中忽略。

## 环境要求

| 项目 | 说明 |
|------|------|
| 系统 | Windows（`start.bat`）；代码本身兼容 POSIX，可直接 `python chat.py` |
| Python | 3.10+，已加入 PATH |
| 依赖 | `pip install -r requirements.txt`（`start.bat` 会自动装） |
| GPU（可选） | NVIDIA 驱动；CUDA 包用于 GPU 加速 |
| 模型 | 至少一个支持 tool calling 的 GGUF |

## 快速开始

### 1. 准备 `bin/`（llama-server）

来源：[llama.cpp Releases](https://github.com/ggml-org/llama.cpp/releases)。按机器选择并解压到 `bin/`：

| 场景 | 下载（名称随版本变化） |
|------|------------------------|
| 仅 CPU | `llama-b*-bin-win-cpu-x64.zip` |
| NVIDIA + CUDA 12 | `llama-b*-bin-win-cuda-12.*-x64.zip`，另下 `cudart-llama-bin-win-cuda-12.*-x64.zip` |
| NVIDIA + CUDA 13 | `llama-b*-bin-win-cuda-13.*-x64.zip`，另下 `cudart-llama-bin-win-cuda-13.*-x64.zip` |

最终至少要有 `bin/llama-server.exe` 和同包（及 cudart 包）中的全部 DLL。

### 2. 准备 `models/`（GGUF）

来源：[Hugging Face](https://huggingface.co/) 上的 GGUF 仓库。推荐（支持 tool calling）：

| 模型 | 下载页 |
|------|--------|
| Qwen3.5 4B Super Coder | [jica98/qwen3.5-4B-super-coder](https://huggingface.co/jica98/qwen3.5-4B-super-coder) |
| Qwen3.5 4B / 9B 官方量化 | [unsloth/Qwen3.5-4B-GGUF](https://huggingface.co/unsloth/Qwen3.5-4B-GGUF) 等 |

```bat
huggingface-cli download jica98/qwen3.5-4B-super-coder --local-dir models --include "*.gguf"
```

#### 看图（可选）

多模态模型的视觉部分是单独的 projector 文件（`mmproj-*.gguf`）。命名为「模型名 + `.mmproj.gguf`」会自动挂载：

```
models/Qwen3.5-9B-Q4_K_M.gguf
models/Qwen3.5-9B-Q4_K_M.mmproj.gguf
```

名字不匹配时启动会问你要不要启用（默认不启用；projector 只能配它自己那个模型）。

### 3. 启动

```bat
start.bat
```

流程：

1. 检查 Python 与依赖（缺 `rich` / `prompt_toolkit` 时自动 `pip install`）
2. 列出 `models/` 下的 GGUF（大小、参数量、是否带视觉），回车 = 上次用的模型；配了云端时也会列出
3. 后台拉起 `llama-server`，加载进度实时显示；失败时直接打印日志尾部与原因（显存不足 / 端口占用 / 文件损坏）
4. 进入对话；退出时自动关闭服务

也可以 `python chat.py D:\your-project` 直接指定工作目录。

### 4. 配置文件 `config.json`

```bat
copy config.example.json config.json
```

```json
{
  "provider": "local",
  "host": "127.0.0.1",
  "port": 8080,
  "base_url": "",
  "model": "",
  "api_key": "",
  "tavily_api_key": "",
  "server":   { "fit_margin": 384, "fit_ctx": 16384, "ngl": null, "ctx": null, "extra_args": [] },
  "sampling": { "temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "repeat_penalty": null },
  "prompt_tier": "auto"
}
```

| 字段 | 说明 |
|------|------|
| `provider` | `local` / `cloud`；启动菜单里可临时选另一个。环境变量 `CODER_AGENT_PROVIDER` 可强制并跳过菜单 |
| `host` / `port` | 本地 llama-server 监听地址 |
| `base_url` / `model` / `api_key` | 云端 OpenAI 兼容接口（`base_url` 不带 `/v1`）；填了才会在菜单里出现「云端」 |
| `tavily_api_key` | [Tavily](https://app.tavily.com) 搜索 Key（或环境变量 `TAVILY_API_KEY`）。**没配时不会把 `web_search` / `fetch_url` 暴露给模型**，省 token 也免报错 |
| `server.fit_margin` | 自适应显存时预留给桌面的 MiB（`-fitt`）。留太多会把层挤到 CPU 变慢 |
| `server.fit_ctx` | 自适应允许的最小上下文（`-fitc`）。系统提示 + 工具声明约 2k token，别低于 8192 |
| `server.ngl` / `server.ctx` | 手动写死 `-ngl` / `-c`（写了就不再自适应）；环境变量 `CODER_AGENT_NGL` / `CODER_AGENT_CTX` 优先 |
| `server.extra_args` | 追加给 llama-server 的其它参数 |
| `sampling.*` | 采样参数，非空字段才发给模型。示例值是 Qwen3 系列推荐 |
| `prompt_tier` | `auto`（≤5B 用 compact 提示词）/ `full` / `compact` |

6GB RTX 2060 + 9B Q4_K_M 实测 `fit_margin`：`1024` → 4.3 GB / 9 tok/s；`384` → 5.0 GB / 14 tok/s；`128` → 5.3 GB / 17 tok/s。

## 使用说明

启动后直接用自然语言下任务：

```
帮我在 C:\Users\...\Desktop\demo 创建一个最小 Vue3 + Vite 项目
把 src/App.vue 里的标题改成 Hello
查一下这个 TypeScript 报错怎么修
```

### 输入

- ↑↓ 翻历史输入；输入 `/` 弹出命令补全
- 多行：以 ``` 开头进入多行模式，再输一行 ``` 结束；或 Alt+Enter 换行
- 直接输入 / 拖入一个路径回车 = 切换工作目录
- 底部状态栏：`模型 │ ctx 用量 │ auto/manual · think │ cwd`

### 斜杠命令

| 命令 | 作用 |
|------|------|
| `/model` | 换本地 GGUF（或切到云端），对话历史保留 |
| `/ctx` | 查看上下文用量条与消息统计 |
| `/compact` | 折叠旧工具结果 + 让模型总结旧对话，腾出上下文 |
| `/think on\|off` | 开关模型思考（Qwen3 系列通过 `enable_thinking`）；简单任务关掉更快 |
| `/verbose` | 工具结果 / 思考 / diff 全量显示（默认折叠） |
| `/resume [N]` | 恢复最近的会话（会切回当时的工作目录） |
| `/sessions` | 列出已保存的会话 |
| `/new` `/clear_cache` `/reset` | 新会话 |
| `/cd <路径>` `/pwd` | 切换 / 查看工作目录 |
| `/auto` `/manual` | 全程自动执行 / 恢复每次确认 |
| `/help` | 命令说明 |
| `/exit` `/quit` `/q` | 退出 |

### 确认与中断

- 写文件 / 改文件前显示 diff（新文件显示前 20 行），`run_command` 显示命令；↑↓ 选「执行」或「本轮自动」，Esc 拒绝并可填一句原因
- 只读命令（`dir` / `git status` 等）与 `process list/read` 自动放行
- `Ctrl+C`：取消当前任务并断开生成；在提示符下再按一次退出
- 每轮结束打印统计：`⏱ 12.4s · 38 tok/s · 提示 6.8k / 生成 1.2k · 3 tools · ctx 42%`

### 上下文管理

- 用量以服务端返回的真实 `prompt_tokens` 为锚点估算，状态栏与 `/ctx` 可见
- 超过 75%：自动把两轮之前的长工具结果折叠成一行摘要
- 超过 90%：自动让模型把旧对话总结成一条备忘，只保留最近几条
- 撞上限报 HTTP 400 时会提示用 `/compact` 或 `/new`

### 内置工具

| 工具 | 能力 |
|------|------|
| `read_file` | 读文件（带行号，可指定行范围） |
| `write_file` | 新建 / 覆盖；`files` 批量 |
| `edit_file` | 精确文本替换；`edits` 一次改多处 / 多文件 |
| `edit_lines` | 按行号 `replace` / `insert` / `delete` |
| `delete_path` / `move_file` | 删除文件或空目录（可批量）/ 移动重命名 |
| `list_dir` / `glob_search` / `grep_search` | 目录、按路径模式、正则搜索 |
| `run_command` | 执行 shell；常驻服务自动后台 |
| `process` | 后台进程 `list` / `read` 日志 / `kill` |
| `check_syntax` | `.py` / `.json` / `.js` 快速语法检查 |
| `todo_write` | 多步骤任务计划清单（返回当前清单） |
| `web_search` / `fetch_url` | 联网搜索（Tavily）与抓取正文；仅配了 Key 时提供 |

启动、`/cd`、`/model` 时会把当前目录的项目上下文注入 system 提示（`AGENTS.md` / `.cursorrules`、`package.json` scripts、git status、README 节选），注入上限随模型上下文长度缩放。

## 云端模型（可选）

在 `config.json` 填好 `base_url` / `model` / `api_key` 后，启动菜单会多一项「云端」。常见场景：

| 场景 | `base_url` | `model` | `api_key` |
|------|-----------|---------|-----------|
| 本机 Ollama 转发云端模型 | `http://127.0.0.1:11434` | `gpt-oss:120b-cloud` | 留空 |
| 直连 Ollama 云端 | `https://ollama.com` | `gpt-oss:120b-cloud` | [ollama.com/settings/keys](https://ollama.com/settings/keys) |
| OpenAI | `https://api.openai.com` | `gpt-4o` 等 | OpenAI API Key |

要跳过菜单直接进云端：`set CODER_AGENT_PROVIDER=cloud` 后 `python chat.py`。

## 工作原理（简要）

```
用户输入
  → agent 发给模型服务（/v1/chat/completions，流式）
  → 模型返回 tool_calls 或最终回复（markdown 渲染）
  → agent 执行工具（写操作先显示 diff 等你确认），把结果写回 messages
  → 上下文快满时自动压缩
  → 循环直到模型给出最终总结，打印本轮统计，会话落盘
```

## 安全提示

- 本工具可读写本地文件并执行任意命令，请只在可信环境使用
- 默认对写操作与 `run_command` 做确认；生产或共享机器上慎用 `/auto`
- 仅监听 `127.0.0.1`，不要随意改成公网暴露
- `sessions/` 里保存了完整对话（含文件内容），注意不要上传

## License

按需自行补充许可证。未声明前，请勿默认可用于商业分发。
