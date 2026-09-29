# mineru-pdf2md

把 PDF 批量转成 Markdown 的命令行工具，基于自建的 **MinerU HTTP 解析服务**（OpenAPI：`http://localhost:8008/docs#/`）。

服务通常跑在远程 GPU 机器上、通过 SSH 映射到本地 8008 端口。本工具把「健康检查 → 需要时自动建 SSH 隧道 → 异步并发上传解析 → 逐文件落盘」整条链路串起来，一条命令即可批量转换，单个文件失败不影响其它文件。

## 核心特性

- **单文件 / 多文件 / 整个目录**批量转换，支持通配符与 `--recursive`
- **异步并发上传**（`asyncio` + `httpx` + 信号量），并发度可配置
- **逐文件返回结果**：成功路径 + 耗时，失败给出具体原因（HTTP 状态码 / 任务状态 / 超时）
- **自动打通服务可达性**：先健康检查，端口不通才建 SSH 隧道；已健康则不重复映射
- **配置外置**：服务器 IP、用户名、解析档位、并发数都放 `config.json`
- **输出可控**：默认与源 PDF 同目录同名 `.md`，也可统一输出到指定目录；同名自动消歧
- 零框架依赖，仅依赖 `httpx`；环境用 `uv` 管理

> 许可协议：**GPL-3.0**（强 copyleft，衍生作品需同样开源）。详见 [License](#license)。

## 技术栈

| 项        | 选择                                                             |
| -------- | -------------------------------------------------------------- |
| 语言       | Python 3.12（3.9+ 亦可）                                           |
| HTTP 客户端 | `httpx`（异步 `AsyncClient`；同步场景可用 `requests` 替代）                 |
| 并发       | `asyncio` + `asyncio.Semaphore`                                |
| 环境/依赖管理  | `uv`（`uv venv` + `uv pip install`）                             |
| 远程通道     | OpenSSH 本地端口映射（`ssh -N -L`）                                    |
| 解析引擎     | MinerU HTTP API v1（`/v1/uploads`、`/v1/parse/jobs`、`/v1/files`） |

## 目录结构

```
.
├── README.md
├── LICENSE
├── requirements.txt
├── config.json          # 运行配置（ssh 段需填自己的服务器信息）
├── SKILL.md             # WorkBuddy 技能清单（可选，用于作为技能安装）
└── scripts/
    ├── pdf2md.py        # CLI 入口：参数解析、批量并发编排、结果输出
    ├── service.py       # 端口健康检查 + SSH 端口映射 + 配置读写
    └── setup_env.sh     # 用 uv 重建虚拟环境
```

## 安装

```bash
# 1. 克隆
git clone https://github.com/jkjkiiiii/mineru-pdf2md.git
cd mineru-pdf2md

# 2. 用 uv 创建专属虚拟环境（固定 Python 3.12）
uv venv --python 3.12.14 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt

# Windows PowerShell 下把解释器路径换成 .venv\Scripts\python.exe
# 也可直接执行：bash scripts/setup_env.sh
```

> 没有 `uv` 时：`pip install -r requirements.txt` 同样可用，只是少了版本隔离。

## 配置

编辑 `config.json`：

```jsonc
{
  "service": { "local_port": 8008, "host": "127.0.0.1" },   // 本地映射端口
  "ssh": {
    "host": "",        // 远程服务器 IP / 域名
    "user": "",        // SSH 登录用户名
    "port": 22,
    "remote_port": 8008,      // 远程机器上 MinerU 服务监听的端口
    "remote_bind_host": "127.0.0.1",
    "identity_file": "",      // 私钥路径，留空用默认密钥
    "batch_mode": true        // 免交互，不等待密码（建议配免密登录）
  },
  "parse": {
    "tier": "advanced",       // flash / basic / standard / advanced
    "ocr_mode": "auto",
    "output_formats": ["markdown"],
    "page_range": "all"
  },
  "output": { "default_output_dir": "" },   // 留空 = 与源 PDF 同目录
  "concurrency": 4
}
```

也可以用命令行写入，不必手改文件：

```bash
.venv/Scripts/python.exe scripts/pdf2md.py --set-remote 172.16.113.1 gpu --ssh-port 22 --remote-port 8008
```

`ssh` 段留空且端口不通时，脚本会提示缺失项并退出（退出码 2），不会盲连。

## 用法

```bash
PY=.venv/Scripts/python.exe      # Windows；macOS/Linux 用 .venv/bin/python

# 单文件：输出同目录同名 .md
"$PY" scripts/pdf2md.py paper.pdf

# 多文件 + 指定输出目录 + 并发 4（用最高档位模型）
"$PY" scripts/pdf2md.py a.pdf b.pdf -d ./md --concurrency 4 --tier advanced

# 整个目录（递归可选）
"$PY" scripts/pdf2md.py ./papers --recursive -d ./md

# 机器可读输出，便于上层汇总
"$PY" scripts/pdf2md.py ./papers -d ./md --json

# 静默 / 详细
"$PY" scripts/pdf2md.py paper.pdf -q          # 只输出最终结果
"$PY" scripts/pdf2md.py paper.pdf -v          # 打印每个文件的分步进度
```

常用参数：`-d/--output-dir`、`-o/--output`（仅单文件）、`--tier`、`--ocr-mode`、  
`--page-range`、`--concurrency`、`--config`、`--no-tunnel`、`--set-remote`、`--json`、`-q`、`-v`。

## 服务可达性流程

1. 请求 `http://127.0.0.1:8008/v1/health`，200 且 `status=ok` 视为健康；
2. 已健康 → 直接复用，**不重复建立 SSH 映射**；
3. 不健康 → 读 `config.json` 的 `ssh.host`/`ssh.user`，后台执行  
   `ssh -N -o ExitOnForwardFailure=yes -L 8008:127.0.0.1:<remote_port> -p <ssh_port> <user>@<host>`，  
   轮询至健康检查通过；配置缺失则提示补充后退出；
4. 无论哪条路径，最后再检查一次端口；仍失败则打印排查清单并以退出码 3 退出。

排查清单包括：远程服务是否在监听、SSH 是否可免密登录、本地端口是否被占用、  
`curl --noproxy '*' http://127.0.0.1:8008/v1/health` 手动验证。

## 输出规则

- 默认：与源 PDF 同目录同名 `.md`
- `-d`：统一输出到指定目录（保留源文件名主干）
- `-o`：指定完整输出路径，仅单文件可用
- 同批次重名自动用父目录名消歧（`paper__sub.md`），不静默覆盖

## 退出码

| 码 | 含义                |
| - | ----------------- |
| 0 | 全部成功              |
| 1 | 至少一个文件转换失败（明细已打印） |
| 2 | 用法错误或缺少 SSH 配置    |
| 3 | 端口健康检查最终失败（服务不可达） |

## 常见问题

- **提示 Permission denied (publickey)**：脚本默认 `BatchMode=yes` 不等待密码，请先配置免密公钥登录或指定 `--identity-file`。
- **端口被占用导致 `ssh -L` 绑定失败**：`netstat -ano | findstr :8008` 查占用进程。
- **代理环境误报服务不可用**：脚本已用 `trust_env=False` 强制本地直连；手动 curl 请加 `--noproxy '*'`。
- **想确认服务端档位能力**：`curl --noproxy '*' http://127.0.0.1:8008/v1/tiers`。

## License

本项目采用 **GPL-3.0**（[GNU General Public License v3.0](LICENSE)）© 2026 jkjkiiiii。
