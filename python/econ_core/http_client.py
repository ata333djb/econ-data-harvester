#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一 HTTP 采集客户端 —— 纯标准库实现，走 OpenSSL 后端。

为什么需要这个模块
------------------
本机（Windows 沙箱）的 Schannel 加密凭证库不可用，表现为::

    curl: (35) schannel: AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS (0x8009030e)

任何使用 Schannel 的客户端都会在 TLS 握手前失败：
    * curl.exe (C:\\Windows\\system32\\curl.exe, curl 8.21.0 Schannel)
    * .NET System.Net.Http.HttpClient  -> "安全包中没有可用的凭证"
    * PowerShell Invoke-WebRequest      -> "基础连接已经关闭"

Python 的 ssl 模块使用自带的 OpenSSL 3.5.8，不受影响。因此**本模块强制要求
用项目 venv 的 Python 运行**，并承担以下职责：

    1. 统一 User-Agent / 请求头，避免 WAF 因裸客户端特征拦截
    2. 支持 Referer / Cookie 透传（统计局 WAF 会校验来源）
    3. 自动重试，指数退避（默认 3 次重试）
    4. 原始响应**逐字节**存档到 data/raw/_http_cache/，供后续解析与审计
    5. 提供 get_text / get_json / get_bytes 三个便捷入口

分层原则
--------
本模块只负责「取回原文并留存」。不做清洗、不做字段映射、不写 parsed/，
以保证 raw 层数据永不被修改（raw -> parsed -> validated -> processed）。

存档约定
--------
    data/raw/_http_cache/<sha256(url)[:16]>.bin        响应体原始字节（含 gzip 压缩态）
    data/raw/_http_cache/<sha256(url)[:16]>.meta.json  请求/响应元数据（含完整响应头）

注意：.bin 保存的是**网线上收到的字节**。若服务端返回 gzip，存档里就是 gzip
压缩数据，模块内部仅在解码时解压，不改动存档。

命令行用法
----------
    .\\.venv\\Scripts\\python.exe python\\econ_core\\http_client.py <url>
    .\\.venv\\Scripts\\python.exe python\\econ_core\\http_client.py <url> --referer https://data.stats.gov.cn/
    .\\.venv\\Scripts\\python.exe python\\econ_core\\http_client.py <url> --json --no-save

库用法
------
    from econ_core.http_client import get_text, get_json, get_bytes

    status, content, headers = get_text("https://data.stats.gov.cn")   # 可解包
    r = get_json("https://example.com/api", referer="https://example.com/")
    print(r.status, r.content, r.cache_path)
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import os
import random
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterator, Mapping, MutableMapping, Optional, Sequence, Union

__all__ = [
    "Response",
    "request",
    "get_text",
    "get_json",
    "get_bytes",
    "cache_path_for",
    "PROJECT_ROOT",
    "CACHE_DIR",
    "DEFAULT_UA",
    "DEFAULT_HEADERS",
    "DEFAULT_TIMEOUT",
    "DEFAULT_RETRIES",
    "assert_venv",
]

# --------------------------------------------------------------------------- #
# 路径与常量
# --------------------------------------------------------------------------- #

#: 文件位于 <PROJECT_ROOT>/python/econ_core/http_client.py
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

#: 原始响应存档目录
CACHE_DIR: Path = PROJECT_ROOT / "data" / "raw" / "_http_cache"

#: 默认 UA —— 伪装成正常 Chrome，规避 WAF 对裸客户端的识别
DEFAULT_UA: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": DEFAULT_UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "application/json;q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "close",
}

#: 超时（秒）
DEFAULT_TIMEOUT: float = 30.0

#: 重试次数（首发之外的额外尝试次数）=> 最多 1 + 3 = 4 次尝试
DEFAULT_RETRIES: int = 3

#: 触发重试的 HTTP 状态码
RETRY_STATUS: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})

_BACKOFF_BASE: float = 1.0
_BACKOFF_CAP: float = 20.0

#: 绕过 venv 检查的开关（仅供特殊场景使用）
_VENV_OVERRIDE_ENV: str = "ECON_HTTP_ALLOW_NON_VENV"

logger = logging.getLogger("econ_core.http_client")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)

_venv_checked: bool = False


# --------------------------------------------------------------------------- #
# venv 强制检查
# --------------------------------------------------------------------------- #

def _venv_python() -> Optional[Path]:
    """返回项目 venv 的解释器路径（不存在则 None）。"""
    for cand in (
        PROJECT_ROOT / ".venv" / "Scripts" / "python.exe",  # Windows
        PROJECT_ROOT / ".venv" / "bin" / "python",          # POSIX
        PROJECT_ROOT / ".venv" / "bin" / "python3",
    ):
        if cand.exists():
            return cand
    return None


def assert_venv(*, strict: bool = True) -> bool:
    """强制要求以项目 venv 的 Python 运行。

    设置环境变量 ``ECON_HTTP_ALLOW_NON_VENV=1`` 可显式绕过。
    返回 True 表示检查通过。
    """
    global _venv_checked
    if _venv_checked:
        return True

    if os.environ.get(_VENV_OVERRIDE_ENV) == "1":
        logger.warning("%s=1，已跳过 venv 检查。", _VENV_OVERRIDE_ENV)
        _venv_checked = True
        return True

    expected = _venv_python()
    actual = Path(sys.executable).resolve()
    if expected is not None and actual == expected.resolve():
        _venv_checked = True
        return True

    msg = (
        "本模块必须使用项目 venv 的 Python 运行（本机 Schannel 凭证库不可用，"
        "非 OpenSSL 栈会 TLS 失败）。\n"
        f"  期望解释器: {expected}\n"
        f"  当前解释器: {actual}\n"
        f"  请改用: python\\..\\..\\.venv\\Scripts\\python.exe {Path(__file__).name}\n"
        f"  如确需绕过，设置环境变量 {_VENV_OVERRIDE_ENV}=1"
    )
    if strict:
        raise RuntimeError(msg)
    logger.warning(msg)
    _venv_checked = True
    return False


# --------------------------------------------------------------------------- #
# Response
# --------------------------------------------------------------------------- #

Headers = dict[str, str]


@dataclass
class Response:
    """一次 HTTP 交互的完整结果。

    content 的类型取决于取用入口：
        request()   -> bytes（网线上解压后的字节）
        get_bytes() -> bytes
        get_text()  -> str
        get_json()  -> 解析后的 Python 对象

    支持按 ``(status, content, headers)`` 三元组解包::

        status, content, headers = get_text(url)
    """

    status: int
    content: Any
    headers: Headers
    url: str
    final_url: str = ""
    method: str = "GET"
    encoding: Optional[str] = None
    elapsed: float = 0.0
    attempts: int = 1
    from_cache: bool = False
    cache_path: Optional[Path] = None
    meta_path: Optional[Path] = None
    history: list[dict[str, Any]] = field(default_factory=list)

    # -- 便捷判定 ---------------------------------------------------------- #
    @property
    def ok(self) -> bool:
        """2xx 视为成功。"""
        return 200 <= self.status < 300

    def __iter__(self) -> Iterator[Any]:
        """允许 (status, content, headers) = resp 解包。"""
        yield self.status
        yield self.content
        yield self.headers

    def __bool__(self) -> bool:
        return self.ok

    def __repr__(self) -> str:  # pragma: no cover
        n = len(self.content) if hasattr(self.content, "__len__") else "?"
        return (
            f"<Response {self.status} {self.method} {self.final_url or self.url} "
            f"content={type(self.content).__name__}({n}) attempts={self.attempts} "
            f"cache={self.cache_path.name if self.cache_path else None}>"
        )

    # -- 二次取用 ---------------------------------------------------------- #
    def json(self) -> Any:
        """把 content 当 JSON 解析（bytes/str 均可）。"""
        raw = self.content
        if isinstance(raw, (bytes, bytearray)):
            raw = _decode_bytes(bytes(raw), self.encoding, self.headers)
        if isinstance(raw, str):
            return json.loads(raw)
        return raw

    def text(self) -> str:
        raw = self.content
        if isinstance(raw, (bytes, bytearray)):
            return _decode_bytes(bytes(raw), self.encoding, self.headers)
        return str(raw)


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #

def _body_fingerprint(body: Optional[Union[bytes, str]]) -> str:
    """请求体的 sha256 十六进制摘要；GET（body 为 None）返回空字符串。"""
    if body is None:
        return ""
    raw = body.encode("utf-8") if isinstance(body, str) else bytes(body)
    return hashlib.sha256(raw).hexdigest()


def _cache_digest(url: str, method: str = "GET",
                  request_body: Optional[Union[bytes, str]] = None) -> str:
    """存档文件名主干 = 缓存键的 sha256 前 16 位。

    缓存键把请求方法（及请求体）纳入，避免不同 POST body 互相覆盖存档：

        GET  -> sha256("GET:" + url)[:16]
        POST -> sha256("POST:" + url + ":" + body)[:16]

    请求体按 latin-1 解码参与拼接：该映射在 0-255 上双射，不会把两个不同的
    字节串折叠成同一个键（raw 层可追溯性优先于可读性）。
    """
    m = method.upper()
    if m == "GET" or request_body is None:
        key = f"GET:{url}"
    else:
        raw = request_body.encode("utf-8") if isinstance(request_body, str) else bytes(request_body)
        key = f"POST:{url}:{raw.decode('latin-1')}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _cache_paths(url: str, method: str = "GET",
                 request_body: Optional[Union[bytes, str]] = None) -> tuple[Path, Path]:
    digest = _cache_digest(url, method, request_body)
    return CACHE_DIR / f"{digest}.bin", CACHE_DIR / f"{digest}.meta.json"


def cache_path_for(url: str, method: str = "GET",
                   body: Optional[Union[bytes, str]] = None) -> tuple[Path, Path]:
    """公开的缓存路径查询：给定 url/method/body，返回 (bin_path, meta_path)。

    供离线复跑脚本使用，保证与采集时使用同一套缓存键规则。
    """
    return _cache_paths(url, method, body)


def _decode_bytes(body: bytes, encoding: Optional[str], headers: Mapping[str, str]) -> str:
    """把字节解码成文本。优先级：显式 encoding > Content-Type charset > utf-8 > gb18030。"""
    candidates: list[str] = []
    if encoding:
        candidates.append(encoding)
    ctype = ""
    for k, v in headers.items():
        if k.lower() == "content-type":
            ctype = v
            break
    if "charset=" in ctype.lower():
        candidates.append(ctype.lower().split("charset=", 1)[1].split(";")[0].strip().strip('"'))
    candidates += ["utf-8", "gb18030"]

    for enc in candidates:
        if not enc:
            continue
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


def _decompress(body: bytes, content_encoding: Optional[str]) -> bytes:
    """按 Content-Encoding 解压（仅内存中处理，不影响存档的原始字节）。"""
    if not body or not content_encoding:
        return body
    ce = content_encoding.lower().strip()
    try:
        if ce == "gzip":
            return gzip.decompress(body)
        if ce == "deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except Exception as exc:  # noqa: BLE001 - 解压失败不应中断采集
        logger.warning("解压失败（Content-Encoding=%s）：%s；按原始字节返回。", ce, exc)
        return body
    logger.warning("未知 Content-Encoding=%s，按原始字节返回。", ce)
    return body


def _headers_to_dict(msg: Any) -> Headers:
    out: Headers = {}
    for k, v in msg.items():
        out[k] = v
    return out


def _normalize_cookies(cookies: Union[str, Mapping[str, str], Sequence[str], None]) -> Optional[str]:
    if not cookies:
        return None
    if isinstance(cookies, str):
        return cookies
    if isinstance(cookies, Mapping):
        return "; ".join(f"{k}={v}" for k, v in cookies.items())
    return "; ".join(str(c) for c in cookies)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """关闭自动重定向（redirect_request 返回 None，urllib 会把 3xx 作为 HTTPError 抛出）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D102
        return None


def _build_opener(verify: bool, allow_redirects: bool) -> urllib.request.OpenerDirector:
    ctx = ssl.create_default_context() if verify else ssl._create_unverified_context()
    handlers: list[Any] = [urllib.request.HTTPSHandler(context=ctx)]
    if not allow_redirects:
        handlers.append(_NoRedirect())
    opener = urllib.request.build_opener(*handlers)
    opener.addheaders = []  # 完全由我们控制请求头
    return opener


def _backoff_delay(attempt: int) -> float:
    return min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** attempt)) + random.uniform(0.0, 0.3)


def _save_raw(url: str, *, status: int, body: bytes, headers: Headers,
              method: str, elapsed: float, attempts: int, encoding: Optional[str],
              raw_header_block: str,
              request_body: Optional[Union[bytes, str]] = None) -> tuple[Path, Path]:
    """把原始响应逐字节存档 + 写 sidecar 元数据。

    存档路径由缓存键决定：GET 只看 url，POST 还看请求体（见 _cache_digest）。
    meta.json 记录 method 与 body_sha256，便于事后核对"哪次请求产生了这个存档"。
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    bin_path, meta_path = _cache_paths(url, method, request_body)

    bin_path.write_bytes(body)

    entry: dict[str, Any] = {
        "url": url,
        "method": method.upper(),
        "body_sha256": _body_fingerprint(request_body),
        "request_body_preview": (
            (request_body.encode("utf-8") if isinstance(request_body, str) else bytes(request_body))
            .decode("utf-8", errors="replace")[:500]
            if request_body is not None else ""
        ),
        "status": status,
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "fetched_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_sec": round(elapsed, 3),
        "attempts": attempts,
        "encoding_used": encoding,
        "headers": headers,
        "raw_header_block": raw_header_block,
    }

    meta: dict[str, Any] = {
        "cache_key": bin_path.stem, "url": url, "method": method.upper(),
        "body_sha256": _body_fingerprint(request_body),
        "fetch_count": 0, "fetches": [],
    }
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("元数据文件损坏，重建：%s", meta_path)
    meta.setdefault("fetches", []).append(entry)
    meta["fetches"] = meta["fetches"][-20:]  # 只保留最近 20 次
    meta["fetch_count"] = meta.get("fetch_count", 0) + 1
    meta["url"] = url
    meta["method"] = method.upper()
    meta["body_sha256"] = _body_fingerprint(request_body)
    meta["last_fetch"] = entry
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    return bin_path, meta_path


# --------------------------------------------------------------------------- #
# 核心请求
# --------------------------------------------------------------------------- #

def request(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Mapping[str, str]] = None,
    referer: Optional[str] = None,
    cookies: Union[str, Mapping[str, str], Sequence[str], None] = None,
    params: Optional[Mapping[str, Any]] = None,
    data: Optional[Union[bytes, str]] = None,
    json_body: Any = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    verify: bool = True,
    allow_redirects: bool = True,
    save: bool = True,
    encoding: Optional[str] = None,
) -> Response:
    """发起一次 HTTP 请求，返回 :class:`Response`（content 为原始 bytes）。

    失败重试策略：网络层异常 / 5xx / 429 / 408 最多重试 ``retries`` 次，
    间隔为指数退避（1s, 2s, 4s...，上限 20s，附 0~0.3s 抖动）。

    4xx（429 除外）**不重试**——这类响应是可诊断信号而非瞬时故障。
    HTTP 错误响应体同样返回（不抛异常），因为 WAF 拦截页往往含关键信息。
    """
    assert_venv()

    if params:
        sep = "&" if urllib.parse.urlparse(url).query else "?"
        url = f"{url}{sep}{urllib.parse.urlencode(params)}"

    hdrs: dict[str, str] = dict(DEFAULT_HEADERS)
    if headers:
        hdrs.update({str(k): str(v) for k, v in headers.items()})
    if referer:
        hdrs["Referer"] = referer
    cookie_str = _normalize_cookies(cookies)
    if cookie_str:
        hdrs["Cookie"] = cookie_str

    body: Optional[bytes] = None
    if json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json; charset=utf-8")
    elif data is not None:
        body = data.encode("utf-8") if isinstance(data, str) else data
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=utf-8")
    if body is not None:
        hdrs["Content-Length"] = str(len(body))

    method = method.upper()
    opener = _build_opener(verify, allow_redirects)

    history: list[dict[str, Any]] = []
    last_exc: Optional[BaseException] = None

    for attempt in range(retries + 1):
        started = time.monotonic()
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with opener.open(req, timeout=timeout) as resp:
                status = resp.status
                resp_headers = _headers_to_dict(resp.headers)
                raw_block = str(resp.headers)
                wire = resp.read()
                final_url = resp.geturl()
        except urllib.error.HTTPError as exc:
            status = exc.code
            resp_headers = _headers_to_dict(exc.headers) if exc.headers else {}
            raw_block = str(exc.headers) if exc.headers else ""
            try:
                wire = exc.read() or b""
            except Exception:  # noqa: BLE001
                wire = b""
            final_url = exc.geturl() if hasattr(exc, "geturl") else url
            last_exc = exc
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, ConnectionError, OSError) as exc:
            last_exc = exc
            elapsed = time.monotonic() - started
            history.append({"attempt": attempt + 1, "error": f"{type(exc).__name__}: {exc}",
                            "elapsed_sec": round(elapsed, 3)})
            if attempt < retries:
                delay = _backoff_delay(attempt)
                logger.warning(
                    "网络异常 %s: %s —— %.1fs 后重试（第 %d/%d 次）",
                    type(exc).__name__, exc, delay, attempt + 1, retries,
                )
                time.sleep(delay)
                continue
            logger.error("重试耗尽，放弃：%s", exc)
            raise

        elapsed = time.monotonic() - started
        history.append({
            "attempt": attempt + 1,
            "status": status,
            "elapsed_sec": round(elapsed, 3),
            "bytes_wire": len(wire),
        })

        if status in RETRY_STATUS and attempt < retries:
            delay = _backoff_delay(attempt)
            logger.warning(
                "HTTP %d —— %.1fs 后重试（第 %d/%d 次）", status, delay, attempt + 1, retries,
            )
            time.sleep(delay)
            continue

        decoding = _decompress(wire, resp_headers.get("Content-Encoding"))
        used_encoding = None
        if encoding:
            used_encoding = encoding
        elif "charset=" in resp_headers.get("Content-Type", "").lower():
            used_encoding = (
                resp_headers["Content-Type"].lower().split("charset=", 1)[1]
                .split(";")[0].strip().strip('"')
            )

        bin_path: Optional[Path] = None
        meta_path: Optional[Path] = None
        if save:
            bin_path, meta_path = _save_raw(
                url, status=status, body=wire, headers=resp_headers, method=method,
                elapsed=elapsed, attempts=attempt + 1, encoding=used_encoding,
                raw_header_block=raw_block, request_body=body,
            )
            logger.info(
                "存档 raw -> %s (%d bytes%s)",
                bin_path, len(wire),
                f", sha256[:12]={hashlib.sha256(wire).hexdigest()[:12]}" if wire else "",
            )

        return Response(
            status=status,
            content=decoding,
            headers=resp_headers,
            url=url,
            final_url=final_url,
            method=method,
            encoding=used_encoding,
            elapsed=round(elapsed, 3),
            attempts=attempt + 1,
            cache_path=bin_path,
            meta_path=meta_path,
            history=history,
        )

    raise RuntimeError(f"unreachable: {last_exc}")  # pragma: no cover


# --------------------------------------------------------------------------- #
# 便捷入口
# --------------------------------------------------------------------------- #

def get_bytes(url: str, **kwargs: Any) -> Response:
    """取原始字节（解压后）。返回 Response，content 为 bytes。"""
    return request(url, **kwargs)


def get_text(url: str, *, encoding: Optional[str] = None, **kwargs: Any) -> Response:
    """取文本。返回 Response，content 为 str。

    编码优先级：显式 encoding > Content-Type charset > utf-8 > gb18030。
    """
    resp = request(url, encoding=encoding, **kwargs)
    return replace(resp, content=_decode_bytes(resp.content, resp.encoding, resp.headers))


def get_json(url: str, *, encoding: Optional[str] = None, **kwargs: Any) -> Response:
    """取 JSON。返回 Response，content 为解析后的对象；解析失败会抛 json.JSONDecodeError。"""
    kwargs.setdefault("headers", {})
    hdrs = dict(kwargs["headers"])
    hdrs.setdefault("Accept", "application/json, text/plain, */*")
    kwargs["headers"] = hdrs
    resp = request(url, encoding=encoding, **kwargs)
    text = _decode_bytes(resp.content, resp.encoding, resp.headers)
    try:
        parsed = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        logger.error("JSON 解析失败，响应前 300 字符：%r", text[:300])
        raise
    return replace(resp, content=parsed)


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #

def _main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    p = argparse.ArgumentParser(
        prog="http_client.py",
        description="统一 HTTP 采集客户端（OpenSSL 后端 / 原始响应自动存档）",
    )
    p.add_argument("url", help="目标 URL")
    p.add_argument("-X", "--method", default="GET")
    p.add_argument("--referer", default=None, help="Referer 头")
    p.add_argument("--cookie", default=None, help="Cookie 头，形如 k=v; k2=v2")
    p.add_argument("--json", action="store_true", help="按 JSON 解析并美化打印")
    p.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p.add_argument("--no-save", action="store_true", help="不存档到 data/raw/_http_cache/")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    assert_venv(strict=True)

    try:
        if args.json:
            resp = get_json(
                args.url, method=args.method, referer=args.referer, cookies=args.cookie,
                retries=args.retries, timeout=args.timeout, save=not args.no_save,
            )
            body_text = json.dumps(resp.content, ensure_ascii=False, indent=2)
        else:
            resp = get_text(
                args.url, method=args.method, referer=args.referer, cookies=args.cookie,
                retries=args.retries, timeout=args.timeout, save=not args.no_save,
            )
            body_text = resp.content if isinstance(resp.content, str) else repr(resp.content)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print("=" * 72)
    print(f"URL      : {resp.url}")
    print(f"FINAL    : {resp.final_url}")
    print(f"METHOD   : {resp.method}")
    print(f"STATUS   : {resp.status}  (ok={resp.ok})")
    print(f"ELAPSED  : {resp.elapsed}s   ATTEMPTS: {resp.attempts}")
    print(f"ENCODING : {resp.encoding}")
    print(f"CACHE    : {resp.cache_path}")
    print(f"META     : {resp.meta_path}")
    print("-" * 72)
    print("HEADERS:")
    for k, v in resp.headers.items():
        print(f"  {k}: {v}")
    print("-" * 72)
    print("BODY (first 2000 chars):")
    print(body_text[:2000])
    if len(body_text) > 2000:
        print(f"\n... [truncated, total {len(body_text)} chars]")
    print("=" * 72)
    return 0 if resp.ok else 2


if __name__ == "__main__":
    raise SystemExit(_main())
