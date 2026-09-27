# Coder Agent → Cursor 本地模型桥接

在本机用 [llama.cpp](https://github.com/ggerganov/llama.cpp) 的 `llama-server` 加载 GGUF，暴露 OpenAI 兼容接口，经 Cloudflare 隧道接到 Cursor 自定义模型。

本仓库**不再提供终端写代码 Agent**。读改文件、跑命令由 Cursor Agent 自己完成。

> 三步：① `bin/` 放好 `llama-server` ② `models/` 放好 GGUF ③ 双击 `start.bat`，另开终端跑隧道。

## 目录结构

```
coder-agent/
├── start.bat              # 装依赖 → 运行 chat.py
├── chat.py                # 入口
├── requirements.txt       # rich
├── config.example.json    # 配置模板 → 复制为 config.json
├── config.json            # 运行时配置（不上传 Git）
├── agent/
│   ├── config.py          # 读取 config.json
│   ├── server.py          # 选模型、清端口、启动/停止 llama-server
│   ├── process_guard.py   # 关窗 / 强杀时带走子进程
│   ├── render.py          # 终端输出
│   └── main.py            # 打印 Cursor/隧道说明并保活
├── bin/                   # llama-server 及 DLL（不上传）
├── models/                # *.gguf（不上传）
└── README.md
```

可选：把 `cloudflared-windows-amd64.exe` 放在仓库根目录，启动提示会打印用它的命令（该文件已被 `.gitignore` 忽略）。

## 环境要求

| 项目 | 说明 |
|------|------|
| 系统 | Windows（`start.bat`）；也可 `python chat.py` |
| Python | 3.10+ |
| 依赖 | `pip install -r requirements.txt`（仅 `rich`） |
| GPU（可选） | NVIDIA + 对应 CUDA 包 |
| 模型 | 至少一个支持 tool calling 的 GGUF（推荐 Qwen3.5） |
| 隧道 | [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/) 或本目录自带 exe |

## 快速开始

### 1. 准备 `bin/`

从 [llama.cpp Releases](https://github.com/ggml-org/llama.cpp/releases) 下载对应平台压缩包，全部解压到 `bin/`（CUDA 版需同时解压同版本 `cudart-*`）。

验证：

```bat
bin\llama-server.exe --version
```

### 2. 准备 `models/`

把 GGUF 放进 `models/`。例如 `Qwen3.5-4B-Q5_K_M.gguf`。  
视觉 projector 命名为 `模型名.mmproj.gguf` 会自动挂载。

### 3. 配置（可选）

```bat
copy config.example.json config.json
```

| 字段 | 说明 |
|------|------|
| `host` / `port` | llama-server 监听地址（默认 `127.0.0.1:8080`） |
| `server.fit_margin` | 自适应显存预留 MiB（`-fitt`） |
| `server.fit_ctx` | `--fit` 最小上下文；`null` = 不传 |
| `server.ngl` / `server.ctx` | 手动写死；`null` = 交给 `--fit` / 模型上限 |
| `server.extra_args` | 追加参数 |

环境变量 `CODER_AGENT_NGL` / `CODER_AGENT_CTX` 可覆盖 ngl/ctx。  
`CURSOR_PROXY_TOKEN` 可改启动时打印的 API Key（默认 `cursor-local`；llama-server 默认不校验，Cursor 里填同一把即可）。

### 4. 启动

```bat
start.bat
```

流程：

1. 若 `config` 端口已被占用 → 结束占用进程并确认释放；失败则退出
2. 列出 `models/` 下 GGUF，回车 = 上次模型
3. 后台拉起 `llama-server`，就绪后打印 Cursor 填写说明与隧道命令
4. 本窗口保持打开；**Ctrl+C 或关闭终端会结束 llama-server**

### 5. 开隧道（另开终端）

```bat
cloudflared tunnel --url http://127.0.0.1:8080 --protocol http2
```

或本目录有 exe 时：

```bat
cloudflared-windows-amd64.exe tunnel --url http://127.0.0.1:8080 --protocol http2
```

`--protocol http2` 建议带上（部分网络下 QUIC 易断）。  
等它打印 `https://xxxx.trycloudflare.com`。快速隧道每次重启地址会变。

### 6. 填到 Cursor

Settings → Models：

| 设置 | 填什么 |
|------|--------|
| OpenAI API Key | `cursor-local`（或你设的 `CURSOR_PROXY_TOKEN`） |
| Override OpenAI Base URL | `https://xxxx.trycloudflare.com/v1`（末尾 `/v1` 必填） |
| 添加的模型名 | 启动日志里的模型名（或 `/v1/models` 返回的 `id`） |

聊天选 **Agent**，选该模型即可。工具与项目上下文由 Cursor 提供。

## 请求路径

```text
Cursor
  → https://<隧道>/v1/chat/completions
  → cloudflared
  → 本机 127.0.0.1:8080（llama-server + GGUF）
```

## 退出与端口

- **Ctrl+C**、正常退出、**点控制台窗口 X**：都会调用 `server.stop()`，并清理残留 `llama-server`
- Windows 下另用 Job Object：进程被强杀时尽量带走子进程
- **每次启动**前检测端口占用；有占用则 `taskkill` 对应 PID，释放失败则不启动

## 常见问题

| 现象 | 处理 |
|------|------|
| 窗口一闪而过 | PATH 里没有 `python`；命令行手动 `python chat.py` 看报错 |
| 缺 DLL / llama-server 闪退 | `bin/` 没拷全；CUDA 版记得解压 `cudart-*` |
| 端口清理失败 | 改 `config.json` 的 `port`，或手动结束占用进程 |
| Cursor 连不上 | 确认本机 `curl http://127.0.0.1:8080/health`，再查隧道与 Base URL 是否带 `/v1` |
| 显存不足 | 写小 `server.ctx`（如 `8192`），或调大 `fit_margin` |

## 安全提示

- 隧道会把本机推理接口暴露到公网临时域名，用完关掉隧道与本窗口
- 仅建议在可信环境使用；不要把含 Key 的 `config.json` 提交到公开仓库

## License

按需自行补充许可证。未声明前，请勿默认可用于商业分发。
