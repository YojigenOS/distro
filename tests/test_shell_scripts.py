#!/usr/bin/env python3
"""shell 变量名和中文之间必须有花括号。

    echo "风味: $FLAVOR（$COUNT 个包）"      # ✗ shell 把「FLAVOR）」当变量名
    echo "风味: ${FLAVOR}（${COUNT} 个包）"  # ✓

这个坑在 packages 仓库里踩过一次（run-vm.sh 因此从来没有启动过 QEMU），
修完在 packages 侧加了检查——**但那个检查只扫 packages 的脚本**。

2026-10-12：同一个坑在 distro 的 assemble.sh 里又发作了一次（那份脚本
2026-10-15 收进了 pantsulib/image.py，坑随之消失）。所以这条检查要跟着
【脚本】走，不是跟着仓库走。两个仓库各放一份。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestShellVariableBoundaries(unittest.TestCase):
    BAD = re.compile(rb"\$([A-Za-z_][A-Za-z0-9_]*)(?=[\x80-\xff])")

    def test_变量名和中文之间要有花括号(self):
        bad = []
        for pat in ("scripts/*.sh", "scripts/*.command"):
            for f in sorted(ROOT.glob(pat)):
                for i, line in enumerate(f.read_bytes().splitlines(), 1):
                    for m in self.BAD.finditer(line):
                        bad.append(f"{f.relative_to(ROOT)}:{i}  "
                                   f"${m.group(1).decode()} 后面紧跟非 ASCII")
        self.assertFalse(bad, "shell 变量名和中文之间要用花括号：\n         "
                         + "\n         ".join(bad))
