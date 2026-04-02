---
name: proxy_rotator
description: WHEN web_fetch or outbound HTTP fails with rate limits / blocks — rotate user-supplied proxies or run VPN_SWITCH_CMD; optional Windows netsh only if META_NETSH_CMD is explicitly set by the operator.
metadata: {"openclaw":{"always":false}}
---

## 安全边界

- 不爬公共「免费代理池」；只使用 `PROXY_LIST_FILE` 中**你本人维护**的条目。
- 不自动登录机场网站；`VPN_SWITCH_CMD` 由你填写本机已安装的 CLI（如官方 VPN 客户端）。

## 步骤

1. `execute_terminal`: `py skills\\proxy_rotator\\rotate.py`（工作区为仓库根）。
2. 若需 Windows 网络栈：仅在老板已设置 `META_NETSH_CMD` 时执行该**单一**命令（例如切换已知配置文件）；否则跳过。
3. 重试失败的 `web_fetch`；仍失败则 `append_typed_memory` 记录并停。

## 与 meta-driving 的关系

后台 `autonomous_tick()` 在检测到 `rate_like` 且配置了 `PROXY_LIST_FILE` 时会自动调用同一套 `rotate_proxy_index` 逻辑。
