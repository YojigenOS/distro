#!/usr/bin/env python3
"""pantsu 的单元测试：共用的辅助，不放测试。

跑法:
    python3 -m unittest discover -s pantsu/tests

（和 packages/skirt/tests/support.py 是同一个位置、同一个用途。两边不长一样
是因为要验的东西不一样，不是因为要各写一套。）
"""

from __future__ import annotations

import contextlib
import io


@contextlib.contextmanager
def 安静():
    """把 stdout / stderr 收起来。

    进度、警告、错误信息都是**给人看的**——测试里要断言它们，
    但不该让它们混进 `unittest` 的输出里。
    """
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


class 会报错的混入:
    """给那些"该说人话然后退出"的函数用的（pantsulib 里全是 `die()`）。"""

    def 应当报错(self, fn, *args, **kwargs) -> str:
        """跑一个【该报错】的函数，返回它写到 stderr 的那段话。

        ★ 不 mock `sys.exit`：那样 mock 之后函数会**继续往下跑**，于是测试
          验的就不是真实行为了（真实情况下 SystemExit 一抛，后面那些行根本
          不会执行）。让它真抛出来，这个差别才会被验到。

        ★ 连退出码一起查：`die()` 固定退 1，CI 和 shell 那侧都靠它判断成败。
        """
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                fn(*args, **kwargs)
        self.assertEqual(cm.exception.code, 1,
                         f"{fn.__name__} 的退出码是 {cm.exception.code}，die() 应该是 1")
        return err.getvalue()
