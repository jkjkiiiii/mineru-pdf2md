"""MinerU 服务可达性保障：端口健康检查 + SSH 端口映射。

设计要点：
1. 每次调用先对 localhost:<port> 做健康检查；健康则直接复用，不重复建立映射。
2. 不健康时读取 config.json 中的 ssh.host / ssh.user，建立 SSH 本地端口映射后重试。
3. 配置缺失（host 或 user 为空）时抛出 ConfigMissing，由上层提示用户补充并写回配置。
4. 无论走哪条路径，最终都以一次端口健康检查收尾；仍失败则抛出 ServiceUnavailable。
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = SKILL_DIR / "config.json"

LOCAL = "local"
TUNNEL = "ssh-tunnel"

DEFAULT_CONFIG: dict[str, Any] = {
    "service": {
        "local_port": 8008,
        "host": "127.0.0.1",
        "health_path": "/v1/health",
        "health_timeout_seconds": 3.0,
        "tunnel_wait_seconds": 25,
        "connect_timeout_seconds": 10,
    },
    "ssh": {
        "host": "",
        "user": "",
        "port": 22,
        "remote_bind_host": "127.0.0.1",
        "remote_port": 8008,
        "identity_file": "",
        "extra_args": [],
        "batch_mode": True,
        "log_file": "",
        "connect_timeout_seconds": 10,
    },
    "parse": {
        "tier": "advanced",
        "ocr_mode": "auto",
        "output_formats": ["markdown"],
        "page_range": "all",
        "poll_interval_seconds": 2,
        "job_timeout_seconds": 1800,
    },
    "output": {"default_output_dir": "", "suffix": ".md"},
    "concurrency": 4,
}


class ConfigMissing(RuntimeError):
    """SSH 映射所需的服务器 IP / 用户名缺失。"""


class ServiceUnavailable(RuntimeError):
    """健康检查最终失败。"""


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """读取配置并与默认值合并，缺字段自动补齐。"""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    data: dict[str, Any] = {}
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"配置文件不是合法 JSON：{cfg_path}\n{exc}") from exc
    return _deep_merge(DEFAULT_CONFIG, data)


def save_remote_config(
    host: str,
    user: str,
    *,
    port: int | None = None,
    remote_port: int | None = None,
    identity_file: str | None = None,
    config_path: str | Path | None = None,
) -> Path:
    """把服务器 IP / 用户名等写入配置文件（保留其它字段）。"""
    cfg_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    cfg = load_config(cfg_path)
    ssh = cfg.setdefault("ssh", {})
    ssh["host"] = host.strip()
    ssh["user"] = user.strip()
    if port:
        ssh["port"] = int(port)
    if remote_port:
        ssh["remote_port"] = int(remote_port)
    if identity_file:
        ssh["identity_file"] = identity_file
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return cfg_path


# --------------------------------------------------------------------------- #
# 健康检查
# --------------------------------------------------------------------------- #
@dataclass
class Health:
    ok: bool
    detail: str

    def __bool__(self) -> bool:  # noqa: D105
        return self.ok


def base_url(cfg: dict) -> str:
    svc = cfg["service"]
    return f"http://{svc['host']}:{svc['local_port']}"


def check_health(cfg: dict, timeout: float | None = None) -> Health:
    """对映射后的本地端口做一次健康检查。端口不通 / 非 200 / status 非 ok 均视为不健康。"""
    svc = cfg["service"]
    url = base_url(cfg) + svc["health_path"]
    timeout = timeout if timeout is not None else float(svc["health_timeout_seconds"])
    try:
        # trust_env=False：本地映射端口必须直连，避免被 HTTP_PROXY 拦截而误报 502。
        resp = httpx.get(url, timeout=timeout, trust_env=False)
    except httpx.ConnectError as exc:
        return Health(False, f"端口 {svc['local_port']} 无法连接（{exc.__class__.__name__}）：{exc}")
    except httpx.HTTPError as exc:
        return Health(False, f"健康检查请求异常：{exc!r}")

    if resp.status_code != 200:
        return Health(False, f"健康检查返回 HTTP {resp.status_code}：{resp.text[:300]}")
    try:
        payload = resp.json()
    except ValueError:
        return Health(False, f"健康检查返回非 JSON：{resp.text[:300]}")
    status = str(payload.get("status", "")).lower()
    if status and status not in {"ok", "healthy", "ready"}:
        return Health(False, f"健康检查 status={status}：{payload}")
    return Health(True, f"HTTP 200 status={status or 'ok'} version={payload.get('version', '?')}")


def port_is_listening(host: str, port: int, timeout: float = 1.0) -> bool:
    """仅判断 TCP 端口是否有监听（不判断服务是否可用）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# SSH 端口映射
# --------------------------------------------------------------------------- #
def build_ssh_command(cfg: dict) -> list[str]:
    svc, ssh = cfg["service"], cfg["ssh"]
    local_port = int(svc["local_port"])
    remote_port = int(ssh["remote_port"])
    bind_host = ssh["remote_bind_host"] or "127.0.0.1"
    cmd: list[str] = [
        "ssh",
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={int(ssh['connect_timeout_seconds'])}",
        "-L",
        f"{local_port}:{bind_host}:{remote_port}",
        "-p",
        str(int(ssh["port"])),
    ]
    if ssh.get("batch_mode", True):
        cmd += ["-o", "BatchMode=yes"]
    if ssh.get("identity_file"):
        cmd += ["-i", str(ssh["identity_file"])]
    cmd += [str(x) for x in ssh.get("extra_args", [])]
    cmd.append(f"{ssh['user']}@{ssh['host']}")
    return cmd


def _spawn(cmd: list[str], log_file: Path) -> subprocess.Popen:
    """后台启动 ssh，日志写入文件；Windows 下脱离当前进程组，避免被中断信号连带杀掉。"""
    handle = log_file.open("a", encoding="utf-8", errors="replace")
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": handle,
        "stderr": subprocess.STDOUT,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
    handle.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(cmd)} =====\n")
    handle.flush()
    return subprocess.Popen(cmd, **kwargs)


def _tail(path: Path, limit: int = 1200) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]


def ssh_config_missing(cfg: dict) -> list[str]:
    ssh = cfg["ssh"]
    missing = []
    if not str(ssh.get("host", "")).strip():
        missing.append("ssh.host（远程服务器 IP / 域名）")
    if not str(ssh.get("user", "")).strip():
        missing.append("ssh.user（登录用户名）")
    return missing


def start_tunnel(cfg: dict, *, logger=print) -> tuple[subprocess.Popen, Path]:
    """建立 SSH 本地端口映射，并等待端口健康检查通过。"""
    svc, ssh = cfg["service"], cfg["ssh"]
    missing = ssh_config_missing(cfg)
    if missing:
        raise ConfigMissing(
            "缺少 SSH 端口映射所需配置：\n"
            + "\n".join(f"  - {m}" for m in missing)
            + "\n请提供远程服务器 IP 和用户名，例如：\n"
            + f"  python scripts/pdf2md.py --set-remote <服务器IP> <用户名> [--ssh-port 22] [--remote-port 8008]\n"
            + f"或直接编辑配置文件：{DEFAULT_CONFIG_PATH}"
        )

    log_file = Path(ssh["log_file"]) if ssh.get("log_file") else SKILL_DIR / "logs" / "ssh-tunnel.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    cmd = build_ssh_command(cfg)
    logger(f"[tunnel] 建立 SSH 端口映射：localhost:{svc['local_port']} -> "
           f"{ssh['host']}:{ssh['remote_port']}（via {ssh['user']}@{ssh['host']}:{ssh['port']}）")
    logger(f"[tunnel] 命令：{' '.join(cmd)}")
    try:
        proc = _spawn(cmd, log_file)
    except FileNotFoundError as exc:
        raise ServiceUnavailable(f"未找到 ssh 可执行文件，请确认 OpenSSH 已安装并加入 PATH：{exc}") from exc

    deadline = time.time() + float(svc["tunnel_wait_seconds"])
    while time.time() < deadline:
        if proc.poll() is not None:
            raise ServiceUnavailable(
                f"SSH 隧道进程已退出（返回码 {proc.returncode}）。日志：{log_file}\n{_tail(log_file)}"
            )
        health = check_health(cfg)
        if health:
            logger(f"[tunnel] 端口已健康：{health.detail}")
            return proc, log_file
        time.sleep(1.0)

    proc.terminate()
    raise ServiceUnavailable(
        f"SSH 隧道已启动但 {svc['tunnel_wait_seconds']} 秒内端口仍未通过健康检查。\n"
        f"隧道日志：{log_file}\n{_tail(log_file)}\n"
        f"请检查：远程主机上 MinerU 服务是否在 {ssh['remote_bind_host']}:{ssh['remote_port']} 监听；"
        f"本地 {svc['local_port']} 端口是否被其它进程占用。"
    )


# --------------------------------------------------------------------------- #
# 统一入口
# --------------------------------------------------------------------------- #
def ensure_service(cfg: dict, *, allow_tunnel: bool = True, logger=print) -> dict:
    """保证本地端口上的 MinerU 服务可用，返回 {mode, base_url, health, ssh_proc, log_file}。"""
    svc = cfg["service"]
    host, port = svc["host"], int(svc["local_port"])

    # 1) 先探测已有端口：已健康则不重复映射
    health = check_health(cfg)
    if health:
        logger(f"[health] localhost:{port} 已健康（{health.detail}），直接复用，无需建立 SSH 映射。")
        return {"mode": LOCAL, "base_url": base_url(cfg), "health": health, "ssh_proc": None}

    logger(f"[health] localhost:{port} 不健康：{health.detail}")
    if port_is_listening(host, port):
        logger(f"[warn] 端口 {port} 有监听但健康检查未通过，可能存在僵死进程占用端口。")

    # 2) 尝试 SSH 端口映射
    if not allow_tunnel:
        raise ServiceUnavailable(_final_failure_msg(cfg, health, tunnel_skipped=True))

    proc, log_file = start_tunnel(cfg, logger=logger)

    # 3) 收尾：再次健康检查
    health = check_health(cfg)
    if not health:
        raise ServiceUnavailable(_final_failure_msg(cfg, health, log_file=log_file))
    logger(f"[health] 最终健康检查通过：{health.detail}")
    return {
        "mode": TUNNEL,
        "base_url": base_url(cfg),
        "health": health,
        "ssh_proc": proc,
        "log_file": log_file,
    }


def _final_failure_msg(cfg: dict, health: Health, *, tunnel_skipped: bool = False, log_file: Path | None = None) -> str:
    svc, ssh = cfg["service"], cfg["ssh"]
    lines = [
        f"MinerU 服务不可用：localhost:{svc['local_port']}{svc['health_path']} 健康检查失败。",
        f"原因：{health.detail}",
    ]
    if tunnel_skipped:
        lines.append("已通过 --no-tunnel 跳过 SSH 端口映射。")
    lines += [
        "",
        "请按以下顺序排查：",
        f"1. 确认远程服务器 {ssh['host'] or '<未配置 IP>'} 上的 MinerU 服务正在运行，"
        f"并监听 {ssh['remote_bind_host']}:{ssh['remote_port']}；",
        f"2. 确认 SSH 可连通：ssh -p {ssh['port']} {ssh['user'] or '<未配置用户名>'}@{ssh['host'] or '<未配置 IP>'}"
        "（建议配置免密公钥登录，脚本默认以 BatchMode 运行，不会交互式等待密码）；",
        f"3. 确认本地 {svc['local_port']} 端口未被其它进程占用：netstat -ano | findstr :{svc['local_port']}；",
        f"4. 手动验证映射结果：curl http://127.0.0.1:{svc['local_port']}{svc['health_path']}；",
        f"5. 查看服务接口文档：http://localhost:{svc['local_port']}/docs#/ 。",
    ]
    if log_file:
        lines += ["", f"SSH 隧道日志：{log_file}", _tail(log_file)]
    return "\n".join(lines)
