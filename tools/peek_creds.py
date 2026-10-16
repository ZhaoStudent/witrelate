"""排查用：确认 wit 到底从哪里读开发者凭据、读到了什么（不打印密码明文）。"""

from __future__ import annotations

import inspect
import os

from wit import RegistryPasswordCredentials

print("=== 环境里与 ACPS 相关的变量名 ===")
for key in sorted(os.environ):
    if key.startswith("ACPS"):
        value = os.environ[key]
        shown = "(空)" if not value else f"长度 {len(value)}"
        print(f"  {key:<26} {shown}")
        if "PASS" in key or "SECRET" in key.upper():
            print(f"     首尾是否有空白/引号: {value[:1]!r} … {value[-1:]!r}")

print()
print("=== from_env() 读取结果 ===")
try:
    creds = RegistryPasswordCredentials.from_env()
    print(f"  username = {creds.username!r}")
    print(f"  password 长度 = {len(creds.password)}")
except Exception as exc:  # noqa: BLE001
    print(f"  ❌ {type(exc).__name__}: {exc}")

print()
print("=== from_env 的读取规则 ===")
try:
    print(inspect.getsource(RegistryPasswordCredentials.from_env))
except Exception as exc:  # noqa: BLE001
    print(f"  (取不到源码: {exc})")

print()
print("=== 类里所有字段 ===")
try:
    print(f"  {list(RegistryPasswordCredentials.__dataclass_fields__)}")
except Exception as exc:  # noqa: BLE001
    print(f"  ({exc})")
