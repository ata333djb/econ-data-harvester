#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""econ_core —— 经济数据采集与验证核心层。

当前模块：
    http_client  HTTP 采集客户端（纯标准库 / OpenSSL 后端 / 原始响应自动存档）

设计约束参见 http_client 模块文档：本机 Schannel 凭证库不可用，
所有 HTTPS 采集必须经由 Python 的 OpenSSL 栈完成。
"""

from . import http_client  # noqa: F401

__all__ = ["http_client"]
__version__ = "0.1.0"
