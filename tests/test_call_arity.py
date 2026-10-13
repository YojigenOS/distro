"""调用点和函数签名对不上——少传参数。

★ 为什么值得单独写一个测试。

   `_accel_name()` 少传了一个参数，签名要 guest_arch：

       TypeError: _accel_name() missing 1 required positional argument: 'guest_arch'

   而这个调用点【只在真启动 QEMU 的时候】才走到：

     · 骨架的单元测试不覆盖它
     · CI 前几轮从没跑到那里（盘都没建出来过）

   于是它一路活到了「第一次真的建出磁盘镜像」那一天——一跑就现形。

★ 这个测试不跑代码，只做静态检查：把所有【本地定义的函数】的必填参数个数
   算出来，再看每一处调用给够了没有。

   它抓不到那类（比如传错了类型），但对"少传一个参数"这种整类问题有效——
   而这类错误的特点是：**写的时候看不出来，跑的时候才炸，而且只在某条分支上**。
"""
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGES = ["pantsu"]


def _required_params(fn: ast.FunctionDef) -> int:
    """这个函数至少要给几个位置参数。"""
    args = [a for a in fn.args.args if a.arg not in ("self", "cls")]
    has_star = fn.args.vararg is not None or fn.args.kwarg is not None
    need = len(args) - len(fn.args.defaults)
    return 0 if has_star else max(0, need)


def _scan(path: pathlib.Path) -> list[tuple[int, str, int, int]]:
    """返回 [(行号, 函数名, 要几个, 给了几个)]，只列不够的。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    sigs: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            sigs[node.name] = _required_params(node)

    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # 只查直接按名字调的（f()）。属性调用（obj.f()）不查——那要类型推断，
        # 而这里的目标是"同一文件里自己写错了自己"，不是全程序分析。
        if not isinstance(node.func, ast.Name):
            continue
        need = sigs.get(node.func.id)
        if not need:
            continue
        given = len(node.args) + len(node.keywords)
        if given < need:
            problems.append((node.lineno, node.func.id, need, given))
    return problems


class TestCallArity(unittest.TestCase):
    def test_no_missing_arguments(self):
        found = []
        for top in PACKAGES:
            for f in (ROOT / top).rglob("*.py"):
                if "__pycache__" in f.parts:
                    continue
                for lineno, name, need, given in _scan(f):
                    found.append(f"{f.relative_to(ROOT)}:{lineno}  {name}() 要 {need} 个参数，给了 {given} 个")
        self.assertEqual(
            found, [],
            "有调用点少传参数——这类错误平时不报，只在跑到那条分支时才 TypeError：\n  "
            + "\n  ".join(found),
        )


if __name__ == "__main__":
    unittest.main()
