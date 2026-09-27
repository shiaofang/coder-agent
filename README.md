# Coder Agent → Cursor 本地模型桥接

本机用 [llama.cpp](https://github.com/ggerganov/llama.cpp) 加载 GGUF，经 Cloudflare 隧道接到 Cursor。  
读改文件、跑命令由 Cursor Agent 完成。

```
bin/ 放 llama-server  →  models/ 放 GGUF  →  start.bat  →  另开终端跑隧道
```

---

## 环境

| 项目 | 要求 |
|------|------|
| 系统 | Windows（或直接 `python chat.py`） |
| Python | 3.10+，`pip install -r requirements.txt` |
| 模型 | 支持 tool calling 的 GGUF（推荐 Qwen3.5） |
| GPU | 可选，NVIDIA + CUDA |
| 隧道 | [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/)，或根目录放 `cloudflared-windows-amd64.exe` |

---

## 快速开始

### 1. 准备 `bin/`

从 [llama.cpp Releases](https://github.com/ggml-org/llama.cpp/releases) 解压到 `bin/`。  
CUDA 版需同时解压同版本 `cudart-*`。

```bat
bin\llama-server.exe --version
```

### 2. 准备 `models/`

放入 GGUF。视觉 projector 命名为 `模型名.mmproj.gguf` 会自动挂载。

### 3. 配置（可选）

```bat
copy config.example.json config.json
```

| 字段 | 说明 |
|------|------|
| `host` / `port` | 默认 `127.0.0.1:8080` |
| `server.fit_margin` | 自适应显存预留（MiB） |
| `server.fit_ctx` | `--fit` 最小上下文；`null` = 不传 |
| `server.ngl` / `ctx` | 手动指定；`null` = 交给 `--fit` |
| `server.extra_args` | 追加启动参数 |

可用环境变量覆盖：`CODER_AGENT_NGL`、`CODER_AGENT_CTX`、`CURSOR_PROXY_TOKEN`（默认 API Key 为 `cursor-local`）。

### 4. 启动

```bat
start.bat
```

清端口 → 选模型 → 启动 `llama-server` → 打印 Cursor / 隧道说明。  
**Ctrl+C** 或关闭窗口会停止服务。

### 5. 开隧道

另开一个终端：

```bat
cloudflared tunnel --url http://127.0.0.1:8080 --protocol http2
```

记下输出的 `https://xxxx.trycloudflare.com`（每次重启会变）。

### 6. 填到 Cursor

Settings → Models：

| 项 | 值 |
|----|-----|
| OpenAI API Key | `cursor-local` |
| Override OpenAI Base URL | `https://xxxx.trycloudflare.com/v1` |
| 模型名 | 启动日志中的名称 |

Base URL 末尾必须带 `/v1`。聊天选择 **Agent**。

```
Cursor  →  隧道 /v1  →  cloudflared  →  127.0.0.1:8080
```

---

## 常见问题

| 现象 | 处理 |
|------|------|
| 窗口一闪而过 | 手动运行 `python chat.py` 查看报错 |
| 缺 DLL / 闪退 | 检查 `bin/` 是否拷全；CUDA 版需 `cudart-*` |
| 端口清理失败 | 改 `config.json` 的 `port`，或结束占用进程 |
| Cursor 连不上 | 先测 `curl http://127.0.0.1:8080/health`，再确认 Base URL 含 `/v1` |
| 显存不足 | 缩小 `server.ctx`，或增大 `fit_margin` |

---

## 安全

隧道会把本机推理接口暴露到公网临时域名。用完请关闭隧道与本窗口。  
勿将含密钥的 `config.json` 提交到公开仓库。
