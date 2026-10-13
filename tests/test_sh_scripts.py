"""`#!/bin/sh` 的脚本里不能有 bash 的扩展。

★ 为什么值得单独写一个测试。

   `scripts/assemble.sh` 的 shebang 是 `#!/bin/sh`，CI 上是 dash。我往里写了
   两个 bash 才有的东西，都在真跑的时候才炸：

       LABEL_ARGS=(...)           → Syntax error: "(" unexpected
       ${REVISION:0:12}           → Bad substitution

   **`sh -n` 抓不到第二个**——语法是合法的，它只在【执行到那一行】时才报。
   而 `bash -n` 两个都抓不到（bash 本来就支持）。

   ★ 所以"验的语言不是跑的语言"这一条，光靠 `-n` 是补不上的。

★ 这个测试查【已知的 bash 专有写法】。

   不追求完备（新 bashism 会漏），但覆盖实际会写出来的那些。查的是
   `#!/bin/sh` 的脚本；`#!/bin/bash` 的不查——那些本来就允许用。
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# (正则, 名字, 说明)
BASHISMS = [
    (re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:\s*[0-9]"), "${VAR:0:12}", "子串扩展，POSIX 里没有"),
    (re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*/\S"), "${VAR/a/b}", "替换扩展，POSIX 里没有"),
    (re.compile(r"(?<!\$)\(\([^)]*\)\)"), "(( ))", "算术命令（$(( )) 才是 POSIX）"),
    (re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*=\("), "name=(...)", "数组赋值"),
    (re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\[@\]\}"), "${a[@]}", "数组展开"),
    (re.compile(r"^\s*\[\["), "[[ ]]", "bash 的条件表达式"),
    (re.compile(r"\bfunction\s+\w+\s*\("), "function", "bash 的函数写法"),
    (re.compile(r"\bsource\s+\S"), "source", "POSIX 是 ."),
    (re.compile(r"\becho\s+-e\b"), "echo -e", "POSIX 的 echo 没有 -e"),
    (re.compile(r"\$\'"), "$'...'", "ANSI-C 引用"),
]


def _sh_scripts():
    for d in ("scripts", "pantsu"):
        for f in (ROOT / d).rglob("*"):
            if not f.is_file() or f.suffix not in ("", ".sh"):
                continue
            try:
                first = f.read_text(encoding="utf-8").split("\n", 1)[0]
            except UnicodeDecodeError:
                continue
            if first.strip() == "#!/bin/sh":
                yield f


class TestShScripts(unittest.TestCase):
    def test_no_bashisms(self):
        found = []
        for f in _sh_scripts():
            for i, line in enumerate(f.read_text(encoding="utf-8").split("\n"), 1):
                if line.lstrip().startswith("#"):
                    continue
                for pat, name, why in BASHISMS:
                    if pat.search(line):
                        found.append(f"{f.relative_to(ROOT)}:{i}  {name} —— {why}")
        self.assertEqual(
            found, [],
            "#!/bin/sh 的脚本里有 bash 专有写法（dash 下会报 Bad substitution / Syntax error）：\n  "
            + "\n  ".join(found),
        )


if __name__ == "__main__":
    unittest.main()
