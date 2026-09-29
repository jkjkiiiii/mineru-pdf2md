---
name: pdf2md
description: 通过已映射到本地 8008 端口的 MinerU HTTP 服务，把 PDF 批量转换为 Markdown 文件。支持单文件与多文件批量、异步并发上传、逐文件返回结果与失败原因；每次调用先做端口健康检查，不健康时自动用 SSH 建立端口映射后再重试。当用户提到 PDF 转 Markdown、pdf2md、批量解析 PDF、提取 PDF 文本为 md、文档解析（MinerU）时使用本技能。
agent_created: true
---

# PDF 转 Markdown（MinerU HTTP API）

把 PDF 交给远端 MinerU 解析服务，输出 `.md` 文件。服务固定映射到 **本地端口 8008**，
接口文档：`http://localhost:8008/docs#/`。

## 运行环境（重要）

本技能使用 **uv 创建的专属固定虚拟环境**，不要使用系统 Python：

```
~/.workbuddy/skills/pdf2md/.venv/Scripts/python.exe     # Windows
```

已安装依赖：`httpx`（`requirements.txt`，Python 3.12.14）。若环境缺失，重建：

```bash
bash ~/.workbuddy/skills/pdf2md/scripts/setup_env.sh
```

## 标准调用

```bash
PY=~/.workbuddy/skills/pdf2md/.venv/Scripts/python.exe
SK=~/.workbuddy/skills/pdf2md

# 单文件：输出同目录同名 .md
"$PY" "$SK/scripts/pdf2md.py" "D:/docs/a.pdf"

# 批量 + 指定输出目录 + 并发 4
"$PY" "$SK/scripts/pdf2md.py" "D:/docs/a.pdf" "D:/docs/b.pdf" -d "D:/out" --concurrency 4

# 整个目录（可含通配符、--recursive 递归）
"$PY" "$SK/scripts/pdf2md.py" "D:/docs/pdf_dir" --recursive -d "D:/out"

# 机器可读输出（便于上层汇总）
"$PY" "$SK/scripts/pdf2md.py" "D:/docs/a.pdf" --json
```

## 服务可达性流程（每次调用都执行）

1. **健康检查**：GET `http://127.0.0.1:8008/v1/health`，200 且 `status=ok` 视为健康。
2. **已健康 → 直接复用**，不重复建立 SSH 映射。
3. **不健康 → 建立 SSH 端口映射**：读取 `config.json` 的 `ssh.host` / `ssh.user`，
   后台执行 `ssh -N -L 8008:127.0.0.1:<remote_port> -p <ssh_port> <user>@<host>`，轮询等待健康。
4. **配置缺失 → 提示用户补充**：打印缺失项并退出（退出码 2）。收到用户提供的 IP 和用户名后：

   ```bash
   "$PY" "$SK/scripts/pdf2md.py" --set-remote <服务器IP> <用户名> [--ssh-port 22] [--remote-port 8008] [--identity-file <私钥路径>]
   ```

   该命令会写回 `config.json`，然后重新执行转换即可。
5. **收尾健康检查**：无论走哪条路径，最终再检查一次端口。
   - 失败 → 打印明确错误与排查清单（远程服务是否监听、SSH 是否可免密登录、本地端口是否被占用、
     `curl http://127.0.0.1:8008/v1/health` 验证），退出码 3。
   - 成功 → 开始转换。

## 转换流程（已验证）

`POST /v1/uploads` → `PUT /v1/uploads/{id}/content` → `POST /v1/uploads/{id}/complete`
→ `POST /v1/parse/jobs` → 轮询 `GET /v1/parse/jobs/{job_id}` → `GET /v1/files/{file_id}/content` 下载 Markdown。

- 每个文件独立走完整流程并独立并发，单个文件失败不影响其它文件，失败原因逐条打印。
- 并发度由 `--concurrency` 或 `config.json` 的 `concurrency` 控制（默认 4）。

## 输出规则

- 默认：与源 PDF **同目录同名** `.md`。
- `-d/--output-dir`：统一输出到指定目录（文件名仍取源文件名主干）。
- `-o/--output`：指定完整输出路径，**仅单文件可用**。
- 同批次出现重名时自动用父目录名消歧（`xxx__子目录.md`），不会静默覆盖。

## 配置文件 `config.json`

| 字段 | 说明 |
| --- | --- |
| `service.local_port` | 本地映射端口，固定 8008 |
| `service.health_path` / `health_timeout_seconds` / `tunnel_wait_seconds` | 健康检查路径与超时 |
| `ssh.host` / `ssh.user` | **远程服务器 IP 与用户名（待用户提供）** |
| `ssh.port` / `remote_port` / `remote_bind_host` | SSH 端口 22；远端服务端口 8008 |
| `ssh.identity_file` / `extra_args` / `batch_mode` | 私钥路径、附加 ssh 参数、是否免交互（默认 true） |
| `parse.tier` | `flash` / `basic` / `standard` / `advanced`（默认 `advanced`，以 `/v1/tiers` 为准） |
| `parse.ocr_mode` / `output_formats` / `poll_interval_seconds` / `job_timeout_seconds` | 解析参数 |
| `output.default_output_dir` | 留空表示与源 PDF 同目录 |
| `concurrency` | 并发上传数量 |

## 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 全部成功 |
| 1 | 至少一个文件转换失败（明细已打印） |
| 2 | 用法错误或缺少 SSH 配置（需用户提供 IP / 用户名） |
| 3 | 端口健康检查最终失败（服务不可达） |

## 常用参数

| 参数 | 作用 |
| --- | --- |
| `-d, --output-dir` | 输出目录 |
| `-o, --output` | 单文件输出路径 |
| `--tier` / `--ocr-mode` / `--page-range` | 覆盖解析参数 |
| `--concurrency N` | 并发数 |
| `--no-tunnel` | 不健康时直接报错，不建立 SSH 映射 |
| `--set-remote HOST USER` | 写入远程服务器配置 |
| `--json` / `-q` / `-v` | 结构化输出 / 静默 / 详细步骤 |

## 排障提示

- SSH 默认 `BatchMode=yes`，**不会交互式等待密码**；请先配置免密公钥登录，或用 `--identity-file` 指定私钥。
- 端口被僵死进程占用时，`ssh -L` 会绑定失败；先用 `netstat -ano | findstr :8008` 查占用进程。
- 隧道日志：`~/.workbuddy/skills/pdf2md/logs/ssh-tunnel.log`。
- 想确认服务档位能力：`curl http://127.0.0.1:8008/v1/tiers`。
- 脚本已强制本地直连（`trust_env=False`），不受 `HTTP_PROXY` 影响；若手动用 curl 验证，
  记得加 `--noproxy '*'`，否则可能看到代理返回的 502 而误判服务不可用。
