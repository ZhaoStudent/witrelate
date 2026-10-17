"""远端收件箱核对助手（#125 跨机部署后的 A′ 第三跳）。

背景：evidence-audio 常驻在第二台机器后，`run/e2e_audio_live.py` 的判据 A′
（附件逐跳落盘）第三本收件箱不在本机——探针查本地旧目录只会看到迁移前的
历史文件（「字节在，但落盘时间没变」的**已知假红**）。

本助手在**远端机器**上跑（两台代码同源、仓库同布局），用与探针完全同一份
`inbox.landed_at()`（只看盘、验 sha256、答 staged_at）核对远端收件箱：

    cd ~/witrelate && ./.venv/bin/python -m run.remote_inbox_probe <role> <handle>

打印一行 staged_at（空串 = 不在）。「不在」是合法结果 ⇒ 退出码恒 0；
ssh 层或远端脚本的失败会让退出码非 0，由调用方如实当「查不到」处理。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from legal_acps.evidence.inbox import inbox_root, landed_at  # noqa: E402


def main() -> int:
    if len(sys.argv) != 3:
        print("用法: python -m run.remote_inbox_probe <role> <handle>", file=sys.stderr)
        return 2
    role, handle = sys.argv[1], sys.argv[2]
    # 与探针的 `_box_of()` 同一口径：inbox_root(.wit/<role>)
    print(landed_at(handle, root=inbox_root(ROOT / ".wit" / role)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
