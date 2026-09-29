#!/usr/bin/env python
# Copyright (C) 2026 jkjkiiiii
# SPDX-License-Identifier: GPL-3.0-or-later
# 本程序为自由软件：可依 GPL-3.0 或更高版本条款再发布或修改，详见 LICENSE。
"""PDF -> Markdown 批量转换（MinerU HTTP API，远程服务固定映射到本地 8008 端口）。

调用链（复用已验证流程）：
    POST /v1/uploads                    创建上传任务
    PUT  /v1/uploads/{id}/content       上传 PDF 二进制
    POST /v1/uploads/{id}/complete      完成上传，取得 file_id
    POST /v1/parse/jobs                 创建解析任务
    GET  /v1/parse/jobs/{job_id}        轮询直到 completed / partial
    GET  /v1/files/{file_id}/content    下载 Markdown 并落盘

用法示例：
    python scripts/pdf2md.py a.pdf
    python scripts/pdf2md.py a.pdf b.pdf -d ./out --concurrency 4
    python scripts/pdf2md.py ./pdf_dir --recursive -d ./out --json
    python scripts/pdf2md.py --set-remote 10.0.0.7 mineru --ssh-port 22
"""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import httpx
except ModuleNotFoundError:  # pragma: no cover
    sys.exit(
        "缺少依赖 httpx。请先创建 skill 专属虚拟环境：\n"
        "  uv venv --python 3.12.14 ~/.workbuddy/skills/pdf2md/.venv\n"
        "  uv pip install --python ~/.workbuddy/skills/pdf2md/.venv/Scripts/python.exe "
        "-r ~/.workbuddy/skills/pdf2md/requirements.txt"
    )

import service as svc  # noqa: E402

EXIT_OK = 0
EXIT_CONVERT_FAILED = 1
EXIT_USAGE = 2
EXIT_SERVICE_UNAVAILABLE = 3

TERMINAL_OK = {"completed", "partial"}
TERMINAL_BAD = {"failed", "canceled", "cancelled"}


# --------------------------------------------------------------------------- #
# 输入 / 输出路径
# --------------------------------------------------------------------------- #
def resolve_inputs(raw_items: list[str], recursive: bool) -> list[Path]:
    """展开命令行参数：支持显式文件、通配符、目录。"""
    found: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        key = path.resolve()
        if key not in seen:
            seen.add(key)
            found.append(path.resolve())

    for raw in raw_items:
        path = Path(raw)
        if any(ch in raw for ch in "*?["):
            for hit in sorted(Path(".").glob(raw)):
                if hit.is_file():
                    add(hit)
            continue
        if path.is_dir():
            pattern = "**/*.pdf" if recursive else "*.pdf"
            for hit in sorted(path.glob(pattern)):
                if hit.is_file():
                    add(hit)
            continue
        if path.is_file():
            add(path)
            continue
        print(f"[warn] 忽略不存在的路径：{raw}", file=sys.stderr)
    return found


def resolve_output(pdf: Path, cfg: dict, output_dir: str | None, explicit: str | None) -> Path:
    """默认与源 PDF 同目录；-d 指定输出目录；-o 指定完整文件名（仅单文件）。"""
    suffix = cfg["output"].get("suffix") or ".md"
    if explicit:
        return Path(explicit).resolve()
    stem = pdf.with_suffix("").name
    if output_dir:
        return (Path(output_dir).resolve() / f"{stem}{suffix}")
    default_dir = cfg["output"].get("default_output_dir")
    if default_dir:
        return (Path(default_dir).resolve() / f"{stem}{suffix}")
    return pdf.with_suffix(suffix)


def dedupe_outputs(pairs: list[tuple[Path, Path]]) -> list[tuple[Path, Path]]:
    """同一批次内目标文件重名时，用父目录名消歧，避免静默覆盖。"""
    used: dict[Path, int] = {}
    result: list[tuple[Path, Path]] = []
    for pdf, out in pairs:
        if out not in used:
            used[out] = 1
            result.append((pdf, out))
            continue
        merged = out.with_name(f"{out.stem}__{pdf.parent.name or 'dup'}{out.suffix}")
        used[merged] = 1
        result.append((pdf, merged))
    return result


# --------------------------------------------------------------------------- #
# 单文件转换
# --------------------------------------------------------------------------- #
def _brief(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        body = exc.response.text[:300].replace("\n", " ")
        return f"HTTP {exc.response.status_code} @ {exc.request.url.path}：{body}"
    if isinstance(exc, httpx.TimeoutException):
        return f"请求超时：{exc}"
    if isinstance(exc, httpx.HTTPError):
        return f"网络错误：{exc!r}"
    if isinstance(exc, OSError):
        return f"文件读写错误：{exc}"
    return f"{exc.__class__.__name__}：{exc}"


async def convert_one(
    client: httpx.AsyncClient,
    pdf: Path,
    out: Path,
    cfg: dict,
    *,
    idx: int,
    total: int,
    verbose: bool,
    lock: asyncio.Lock,
    quiet: bool = False,
) -> dict[str, Any]:
    parse = cfg["parse"]
    tag = f"[{idx}/{total}] {pdf.name}"
    started = time.time()

    async def log(msg: str, force: bool = False) -> None:
        """quiet 模式下只保留失败信息（force=True 的消息）。"""
        if quiet and not force:
            return
        async with lock:
            print(f"{tag} {msg}", flush=True)

    result: dict[str, Any] = {
        "file": str(pdf),
        "output": str(out),
        "ok": False,
        "error": None,
        "elapsed_seconds": None,
    }
    try:
        payload_bytes = await asyncio.to_thread(pdf.read_bytes)
        mime = mimetypes.guess_type(pdf.name)[0] or "application/pdf"

        # 1. 创建上传任务
        resp = await client.post(
            "/v1/uploads",
            json={
                "filename": pdf.name,
                "bytes": len(payload_bytes),
                "mime_type": mime,
                "purpose": "parse",
            },
            timeout=30,
        )
        resp.raise_for_status()
        upload_id = resp.json()["id"]
        result["upload_id"] = upload_id
        if verbose:
            await log(f"upload 已创建 {upload_id}")

        # 2. 上传二进制内容
        resp = await client.put(
            f"/v1/uploads/{upload_id}/content",
            content=payload_bytes,
            headers={"Content-Type": "application/octet-stream"},
            timeout=600,
        )
        resp.raise_for_status()
        if verbose:
            await log(f"{len(payload_bytes) / 1024:.0f} KB 已上传")

        # 3. 完成上传 -> file_id
        resp = await client.post(f"/v1/uploads/{upload_id}/complete", json={}, timeout=30)
        resp.raise_for_status()
        file_obj = resp.json().get("file") or {}
        file_id = file_obj.get("id")
        if not file_id:
            raise RuntimeError(f"上传完成但未返回 file 对象：{resp.text[:300]}")
        result["file_id"] = file_id

        # 4. 创建解析任务
        job_payload: dict[str, Any] = {
            "files": [
                {
                    "source": {"type": "file_id", "file_id": file_id},
                    "page_range": parse.get("page_range", "all"),
                }
            ],
            "ocr_mode": parse.get("ocr_mode", "auto"),
            "output_formats": parse.get("output_formats", ["markdown"]),
        }
        if parse.get("tier"):
            job_payload["tier"] = parse["tier"]
        resp = await client.post("/v1/parse/jobs", json=job_payload, timeout=30)
        resp.raise_for_status()
        job_id = resp.json()["job_id"]
        result["job_id"] = job_id
        await log(f"解析任务 {job_id} 已创建")

        # 5. 轮询任务状态
        deadline = time.time() + float(parse.get("job_timeout_seconds", 1800))
        poll = float(parse.get("poll_interval_seconds", 2))
        last_status = None
        job_result: dict[str, Any] = {}
        while True:
            resp = await client.get(f"/v1/parse/jobs/{job_id}", timeout=30)
            resp.raise_for_status()
            job_result = resp.json()
            status = str(job_result.get("status", "")).lower()
            if status != last_status:
                await log(f"status={status}")
                last_status = status
            if status in TERMINAL_OK:
                break
            if status in TERMINAL_BAD:
                raise RuntimeError(f"解析任务失败（status={status}）：{json.dumps(job_result, ensure_ascii=False)[:500]}")
            if time.time() > deadline:
                raise TimeoutError(f"解析任务超时（>{parse.get('job_timeout_seconds')}s），最后状态={status}")
            await asyncio.sleep(poll)

        # 6. 下载 Markdown
        files = job_result.get("files") or []
        if not files:
            raise RuntimeError("任务完成但未返回任何文件结果。")
        file_result = files[0]
        if file_result.get("status") != "completed":
            raise RuntimeError(f"文件解析失败：{json.dumps(file_result, ensure_ascii=False)[:500]}")
        output_files = file_result.get("output_files") or {}
        markdown = output_files.get("markdown")
        if not markdown:
            raise RuntimeError(f"未返回 Markdown 输出：{json.dumps(output_files, ensure_ascii=False)[:500]}")
        resp = await client.get(f"/v1/files/{markdown['file_id']}/content", timeout=300)
        resp.raise_for_status()

        await asyncio.to_thread(_write_output, out, resp.content)
        result["ok"] = True
        await log(f"完成 -> {out}（{len(resp.content) / 1024:.0f} KB）")
    except Exception as exc:  # noqa: BLE001 - 逐文件捕获，保证批量任务互不中断
        result["error"] = _brief(exc)
        await log(f"失败：{result['error']}", force=True)

    result["elapsed_seconds"] = round(time.time() - started, 2)
    return result


def _write_output(out: Path, content: bytes) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(content)


# --------------------------------------------------------------------------- #
# 批量并发
# --------------------------------------------------------------------------- #
async def run_batch(
    pairs: list[tuple[Path, Path]], cfg: dict, verbose: bool, quiet: bool = False
) -> list[dict[str, Any]]:
    concurrency = max(1, int(cfg.get("concurrency", 4)))
    timeout = httpx.Timeout(connect=10, read=600, write=600, pool=10)
    semaphore = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    total = len(pairs)

    # trust_env=False：本地映射端口直连，绕开 HTTP_PROXY，否则上传会被代理拦截。
    async with httpx.AsyncClient(base_url=cfg["_base_url"], timeout=timeout, trust_env=False) as client:

        async def worker(item: tuple[int, tuple[Path, Path]]) -> dict[str, Any]:
            idx, (pdf, out) = item
            async with semaphore:
                return await convert_one(
                    client, pdf, out, cfg, idx=idx, total=total,
                    verbose=verbose, lock=lock, quiet=quiet,
                )

        return await asyncio.gather(*(worker(x) for x in enumerate(pairs, start=1)))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf2md",
        description="PDF 转 Markdown：先做端口健康检查，不健康时自动建立 SSH 端口映射，再异步并发转换。",
    )
    parser.add_argument("inputs", nargs="*", help="PDF 文件 / 目录 / 通配符，可多个")
    parser.add_argument("-o", "--output", help="输出 .md 完整路径（仅在单个输入时可用）")
    parser.add_argument("-d", "--output-dir", help="输出目录（默认与源 PDF 同目录）")
    parser.add_argument("--recursive", action="store_true", help="输入为目录时递归查找 PDF")
    parser.add_argument("--tier", help="解析档位：flash / basic / standard / advanced")
    parser.add_argument("--ocr-mode", dest="ocr_mode", help="OCR 模式，默认 auto")
    parser.add_argument("--page-range", dest="page_range", help="页码范围，默认 all")
    parser.add_argument("--concurrency", type=int, help="并发上传数量，默认取配置文件 concurrency")
    parser.add_argument("--config", help=f"配置文件路径，默认 {svc.DEFAULT_CONFIG_PATH}")
    parser.add_argument("--no-tunnel", action="store_true", help="端口不健康时直接报错，不尝试 SSH 映射")
    parser.add_argument("--set-remote", nargs=2, metavar=("HOST", "USER"),
                        help="写入远程服务器 IP 与用户名到配置文件后退出")
    parser.add_argument("--ssh-port", type=int, help="远程 SSH 端口，默认 22")
    parser.add_argument("--remote-port", type=int, help="远程 MinerU 服务端口，默认 8008")
    parser.add_argument("--identity-file", dest="identity_file", help="SSH 私钥路径")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结构化结果")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出最终结果")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出每个文件的详细步骤")
    return parser


def apply_overrides(cfg: dict, args: argparse.Namespace) -> dict:
    if args.tier:
        cfg["parse"]["tier"] = args.tier
    if args.ocr_mode:
        cfg["parse"]["ocr_mode"] = args.ocr_mode
    if args.page_range:
        cfg["parse"]["page_range"] = args.page_range
    if args.concurrency:
        cfg["concurrency"] = args.concurrency
    if args.ssh_port:
        cfg["ssh"]["port"] = args.ssh_port
    if args.remote_port:
        cfg["ssh"]["remote_port"] = args.remote_port
    if args.identity_file:
        cfg["ssh"]["identity_file"] = args.identity_file
    return cfg


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log = (lambda *a, **k: None) if args.quiet else print

    # 仅写入远程配置
    if args.set_remote:
        host, user = args.set_remote
        path = svc.save_remote_config(
            host,
            user,
            port=args.ssh_port,
            remote_port=args.remote_port,
            identity_file=args.identity_file,
            config_path=args.config,
        )
        print(f"已写入远程服务器配置：host={host} user={user} -> {path}")
        return EXIT_OK

    if not args.inputs:
        print("错误：请提供至少一个 PDF 文件/目录；或使用 --set-remote 配置远程服务器。", file=sys.stderr)
        return EXIT_USAGE

    cfg = apply_overrides(svc.load_config(args.config), args)

    pdfs = resolve_inputs(args.inputs, args.recursive)
    if not pdfs:
        print("错误：没有找到任何可转换的文件。", file=sys.stderr)
        return EXIT_USAGE

    if args.output and len(pdfs) > 1:
        print("错误：-o/--output 只能用于单个文件，批量请用 -d/--output-dir。", file=sys.stderr)
        return EXIT_USAGE

    pairs = dedupe_outputs([(p, resolve_output(p, cfg, args.output_dir, args.output)) for p in pdfs])
    log(f"[info] 待转换 {len(pairs)} 个文件，并发上限 {cfg.get('concurrency')}，"
        f"档位 {cfg['parse'].get('tier')}")

    # 1) 健康检查 / 必要时 SSH 映射 -> 2) 收尾健康检查
    try:
        state = svc.ensure_service(cfg, allow_tunnel=not args.no_tunnel, logger=log)
    except svc.ConfigMissing as exc:
        print(f"\n[需要补充配置] {exc}", file=sys.stderr)
        return EXIT_USAGE
    except svc.ServiceUnavailable as exc:
        print(f"\n[服务不可用] {exc}", file=sys.stderr)
        return EXIT_SERVICE_UNAVAILABLE

    cfg["_base_url"] = state["base_url"]
    log(f"[info] 服务就绪：{state['base_url']}（{state['mode']}）")

    results = asyncio.run(run_batch(pairs, cfg, verbose=args.verbose, quiet=args.quiet))

    ok = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]

    if args.json:
        print(json.dumps({"service": state["mode"], "results": results}, ensure_ascii=False, indent=2))
    else:
        print("-" * 60)
        if failed:
            print("失败明细：")
            for r in failed:
                print(f"  x {r['file']}\n    原因：{r['error']}")
        print(f"成功 {len(ok)} / 共 {len(results)}")
        for r in ok:
            print(f"  v {r['output']}  ({r['elapsed_seconds']}s)")

    return EXIT_OK if not failed else EXIT_CONVERT_FAILED


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        sys.exit(130)
