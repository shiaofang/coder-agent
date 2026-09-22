# Coder Agent

本地终端 AI 编程助手。基于 [llama.cpp](https://github.com/ggerganov/llama.cpp) 的 `llama-server`，在本机跑 GGUF 模型，通过 OpenAI 兼容的 tool calling 直接改文件、执行命令、搜索网页。

只依赖三个 Python 库（`rich` 渲染、`prompt_toolkit` 输入、`playwright` 无头浏览器控制），不依赖 LangChain 等框架。

> 三步跑起来：① 把 `llama-server` 放进 `bin/` ② 把 GGUF 模型放进 `models/` ③ 双击 `start.bat`。
> 细节见下方 [快速开始](#快速开始)。

## 目录

- [特性](#特性)
- [目录结构](#目录结构)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
  - [1. 准备 `bin/`（llama-server 运行时）](#1-准备-binllama-server-运行时)
  - [2. 准备 `models/`（GGUF 模型）](#2-准备-modelsgguf-模型)
  - [3. 启动](#3-启动)
  - [4. 配置文件 `config.json`](#4-配置文件-configjson)
- [使用说明](#使用说明)（输入 / 斜杠命令 / 确认 / 上下文 / 工具）
- [云端模型（可选）](#云端模型可选)
- [工作原理](#工作原理简要)
- [常见问题](#常见问题)
- [安全提示](#安全提示)

## 特性

- **本地推理**：模型与对话都在本机，不经过云端 API；启动时选 GGUF，`/model` 会话内随时切换
- **真干活**：不只给步骤，会读改文件、跑命令、（配了 Key 时）联网查报错
- **改动可审查**：写文件 / 改文件前显示彩色 diff，方向键确认；拒绝时可写一句原因告诉模型
- **网页运行检查**：用系统 Chrome / Edge 无头运行 HTML，捕获控制台、JS 与资源加载错误，不弹浏览器窗口
- **网页工具**：自带 Web UI 已启用 llama-server 全部内置工具，可读写项目文件、搜索内容和执行命令
- **上下文可见可控**：状态栏实时显示 ctx 用量；快满时自动折叠旧工具结果 / 总结旧对话，`/compact` 手动压缩
- **提示词只留契约**：系统提示写任务模式、路径和验收；工具声明写能力与独有语义；工具参数 JSON 容错修复，`/think off` 可关思考提速
- **会话不丢**：每轮自动保存到 `sessions/`，`/resume` 恢复；输入历史 ↑↓ 可翻，支持多行输入
- **常驻服务友好**：`npm run dev` 等会自动后台启动并返回访问地址

## 目录结构

```
coder-agent/
├── start.bat              # Windows 一键启动（装依赖 → 运行 chat.py）
├── chat.py                # 启动入口（实现在 agent/）
├── requirements.txt       # rich, prompt_toolkit, playwright
├── config.example.json    # 配置模板（复制为 config.json）
├── config.json            # 运行时配置（含 Key，不上传 Git）
├── agent/
│   ├── config.py          # 读取 config.json、斜杠命令、安全开关、运行时状态
│   ├── server.py          # 选模型、启动/切换 llama-server、读 /props
│   ├── prompts.py         # 系统提示词
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
├── bin-prism/             # 可选：PrismML 分支的 llama-server（三值量化模型用）
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
| 浏览器 | Chrome 或 Edge；供 `check_webpage` 无头运行页面 |
| 模型 | 至少一个支持 tool calling 的 GGUF |

## 快速开始

### 1. 准备 `bin/`（llama-server 运行时）

`bin/` 放的是 llama.cpp 官方预编译的 `llama-server` 可执行文件和它依赖的全部 DLL。本项目**不自带**这些文件（体积大、与显卡/CUDA 版本相关），需要你按自己的机器下载一次。

#### 1.1 下载哪个包

来源：[llama.cpp Releases](https://github.com/ggml-org/llama.cpp/releases)。每个 Release 底部的 Assets 里按你的机器选：

| 你的机器 | 下载的压缩包（`b*` 是版本号，随时间变化） |
|----------|------------------------------------------|
| 只有 CPU，没独显 | `llama-b****-bin-win-cpu-x64.zip` |
| NVIDIA 显卡 + CUDA 12 | `llama-b****-bin-win-cuda-12.*-x64.zip` **＋** `cudart-llama-bin-win-cuda-12.*-x64.zip` |
| NVIDIA 显卡 + CUDA 13 | `llama-b****-bin-win-cuda-13.*-x64.zip` **＋** `cudart-llama-bin-win-cuda-13.*-x64.zip` |

- 有 N 卡就选 CUDA 包，能把模型层放到 GPU 上、快很多；CUDA 包**必须**再额外下同版本的 `cudart-*` 包（里面是 NVIDIA 运行时 DLL），否则会报缺 `cudart64_12.dll` 之类的错。
- CUDA 12 还是 13：用 `nvidia-smi` 看右上角 "CUDA Version"，选不超过它的那个大版本即可（比如显示 12.4 就用 CUDA 12 包）。
- 不确定 / 没独显：先用 CPU 包能跑通，只是慢。
- 也可以自己从 [llama.cpp 源码](https://github.com/ggml-org/llama.cpp) 编译，把产物拷进 `bin/`。

#### 1.2 怎么放

把 `llama-*.zip` 和 `cudart-*.zip` 里的文件**全部解压到 `bin/` 根目录**（不要保留多一层子文件夹）。两个压缩包的内容直接合并放一起。

#### 1.3 放好后 `bin/` 里应该有什么

以本机一份可用的 CUDA 12 版本为例，`bin/` 里的文件及作用：

| 文件 | 来自哪个包 | 作用 |
|------|-----------|------|
| `llama-server.exe` | llama 包 | **主程序**，启动后提供 `127.0.0.1:8080` 的 HTTP 接口（本项目就是连它） |
| `llama-server-impl.dll` | llama 包 | server 的实际实现（`.exe` 只是瘦启动器） |
| `llama.dll` / `llama-common.dll` | llama 包 | llama.cpp 推理核心 |
| `ggml.dll` / `ggml-base.dll` | llama 包 | ggml 张量计算框架（所有后端的基础） |
| `ggml-cpu-haswell.dll` | llama 包 | **CPU 计算后端**（Haswell 及以上 CPU；GPU 放不下的层落到这里算） |
| `ggml-cuda.dll` | CUDA 包 | **GPU 计算后端**（约 500MB，只有 CUDA 版才有） |
| `mtmd.dll` | llama 包 | 多模态支持（挂 `mmproj` 看图时用到） |
| `libomp.dll` | llama 包 | OpenMP 并行运行时 |
| `cudart64_12.dll` | **cudart 包** | NVIDIA CUDA 运行时 |
| `cublas64_12.dll` / `cublasLt64_12.dll` | **cudart 包** | NVIDIA 矩阵运算库（GPU 加速的关键） |
| `LICENSE-LLVM-OpenMP` | llama 包 | 许可证文件，放着不用管 |

> 关键点：只有 `llama-server.exe` 一个 `.exe`，其余全是它依赖的 `.dll`，**缺一个都可能起不来或报错**。所以务必把压缩包里的 DLL 全部拷进来，不要只挑 `.exe`。CPU 版没有 `ggml-cuda.dll` 和三个 CUDA 运行时 DLL，这是正常的。

#### 1.4 验证是否可用

在仓库根目录执行，能打印版本号就说明 `bin/` 齐全：

```bat
bin\llama-server.exe --version
```

正常输出类似：

```
version: 0.4.1-dev (build 11010, commit 4bc272fd7)
built with Clang 20.1.8 for Windows x86_64
```

如果报「找不到 xxx.dll」或直接闪退，多半是 DLL 没拷全（尤其是 CUDA 版忘了下 `cudart-*` 包）。

### 2. 准备 `models/`（GGUF 模型）

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

#### 三值量化模型（Bonsai，可选）

[prism-ml/Ternary-Bonsai-2-27B-gguf](https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf)
这类权重（文件名带 `PTQ1_0` / `PQ2_0`）用的是自定义三值量化，**官方 llama.cpp 加载会直接报
`invalid ggml type 143` 退出**，必须用 PrismML 分支编出的 `llama-server`。

本项目支持两套运行时并存：把 PrismML 分支的 `llama-server` 及其 DLL 放进 `bin-prism/`，
选到这类模型时自动切过去，其余模型继续用 `bin/`。目录放别处时在 `config.json` 里写
`server.prism_bin_dir`。缺这份构建时，模型列表会标注「缺 Prism 运行时」并在启动前直接报错，
不会白等一次加载。

拿二进制的两种方式（[releases](https://github.com/PrismML-Eng/llama.cpp/releases/latest)）：

- **下预编译包**：按 `bin/` 同样的规则挑，N 卡选 `llama-prism-*-bin-win-cuda-12.4-x64.zip`
  并额外下同版本 `cudart-llama-bin-win-cuda-12.4-x64.zip`，两个包的内容一起解压进 `bin-prism/` 根目录
- **自己编**：`git clone -b prism https://github.com/PrismML-Eng/llama.cpp` 后
  `cmake -B build -DGGML_CUDA=ON && cmake --build build -j`，把 `build/bin/` 的产物拷进 `bin-prism/`

分支的基线 commit 和官方版不一定一致，启动前会先读一次 `llama-server --help`，
只传它认识的参数（`--jinja` / `--tools` / `-fitt`），缺哪个就自动退回 `-ngl 99 -c <fit_ctx>`。

选到这类模型时会自动套一套 6 GB 卡实测参数，**`config.json` 里显式写过的键不会被覆盖**：

| 预设 | 为什么 |
|------|--------|
| `-ngl 99` | 权重 5.95 GB 必须整体在 GPU 上。交给 `--fit` 自适应会把层挤回 CPU，实测从 3.9 tok/s 掉到 0.4 以下 |
| `-nkvo` + `-ctk q4_0` `-ctv q4_0` | 显存已被权重占满，KV cache 放内存并压到 q4_0，否则上下文一大就 OOM |
| `-fa on`、`--context-shift` | Flash Attention 省 KV 显存；上下文满了滚动而不是报错 |
| `temp 1.0` / `top_p 0.95` / `top_k 20` / `repeat_penalty 1.1` | Bonsai（Qwen3 系）官方推荐采样值 |
| `ctx 65536` | 只在 `config.json` 没写 `server.ctx` 时生效 |

RTX 2060 6GB 上的实测：加载约 10 秒，生成约 4 tok/s。能用但慢，适合让它慢慢改一个文件，
不适合长对话来回。

#### 思考深度

选模型的表格里有「思考深度」一列。程序会读 GGUF 里的 chat template 判断该模型是否支持
`reasoning_effort`：支持就列出可选档位（如 Bonsai 的 低/中/极高），选完模型再问一次深度，
按 `--reasoning-effort` 传给服务；只有思考开关（`enable_thinking`）没有分档的模型显示 `-`，
不会追问。档位白名单直接从模板里解析，不会传模型不认的值。回车 = 用模型模板自带的默认档。

### 3. 启动

```bat
start.bat
```

流程：

1. 检查 Python 与依赖（缺 `rich` / `prompt_toolkit` / `playwright` 时自动 `pip install`）
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
  "sampling": { "temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "repeat_penalty": null }
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
| `server.prism_bin_dir` | 三值量化（`PTQ1_0`/`PQ2_0`）模型用的 PrismML `llama-server` 目录，留空 = `bin-prism/` |
| `sampling.*` | 采样参数，非空字段才发给模型。示例值是 Qwen3 系列推荐 |

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

- 写文件 / 改文件前显示 diff（新文件显示前 20 行），`run_command` 显示命令；↑↓ 可选「执行」「本轮自动」或「暂不处理」，Esc 拒绝并可填一句原因
- 只读命令（`dir` / `git status` 等）与 `process list/read` 自动放行
- `Ctrl+C`：取消当前任务并断开生成；在提示符下再按一次退出
- 每轮结束打印统计：`⏱ 12.4s · 38 tok/s · 提示 6.8k / 生成 1.2k · 3 tools · ctx 42%`

`http://127.0.0.1:8080` 的网页也可调用 `read_file`、`write_file`、`edit_file`、
文件搜索和 shell 命令等 llama-server 内置工具。网页工具没有终端 Agent 的逐次确认，
只应在本机可信环境使用；相对路径从项目根目录解析。

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
| `run_command` | 执行 shell；常驻服务自动后台；运行 npm script 前检查 `package.json` 与脚本是否存在 |
| `process` | 后台进程 `list` / `read` 日志 / `kill` |
| `check_syntax` | `.html` / `.py` / `.json` / `.js` 文件级静态检查；HTML 会检查重复 id/属性、本地资源与内联 JS |
| `check_webpage` | 无头浏览器运行本地 HTML / URL，捕获控制台、JS、请求与 HTTP 错误 |
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

## 常见问题

| 现象 | 原因 / 处理 |
|------|-------------|
| 双击 `start.bat` 窗口一闪而过，什么都没有 | 多半是 Python 没装或没加进 PATH。在命令行 `python --version` 确认；也可以在窗口里手动 `python chat.py` 看报错 |
| 提示找不到 `xxx.dll` / `llama-server` 闪退 | `bin/` 的 DLL 没拷全。重看 [1.3](#13-放好后-bin-里应该有什么)，CUDA 版记得连 `cudart-*` 包一起解压 |
| 模型加载失败，日志里有 `invalid ggml type` | 该量化档这份 `llama-server` 不认识。三值量化模型要用 `bin-prism/` 里的 PrismML 构建；其它情况多半是 `bin/` 版本过旧，重下新版 |
| 模型加载失败，日志里有 `out of memory` | 显存不够。调小 `config.json` 的 `server.fit_ctx`（如 `8192`），或调大 `server.fit_margin` |
| 起来了但很慢（个位数 tok/s） | GPU 放不下、层落到了 CPU。换更小的量化档（Q4）、调小上下文，或用参数量更小的模型 |
| `HTTP 401 Unauthorized`（云端） | `config.json` 的 `api_key` 无效或过期，去对应平台重新生成 |
| 模型不调用工具 / 只会聊天 | 该 GGUF 不支持 tool calling，换支持的模型（如 Qwen3.5 系列） |
| `web_search` 用不了 | 没配 `tavily_api_key`；不配的话这个工具根本不会出现，属正常 |

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
- `check_webpage` 会自动执行页面 JavaScript 并可能发起网络请求，只检查可信的本地页面或 URL
- 默认对写操作与 `run_command` 做确认；生产或共享机器上慎用 `/auto`
- 仅监听 `127.0.0.1`，不要随意改成公网暴露
- `sessions/` 里保存了完整对话（含文件内容），注意不要上传

## License

按需自行补充许可证。未声明前，请勿默认可用于商业分发。
