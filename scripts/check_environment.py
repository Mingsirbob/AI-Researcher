from __future__ import annotations

import importlib.metadata
import sys
from pathlib import Path


EXPECTED = {
    "fastapi": "0.139.0",
    "httpx": "0.28.1",
    "iFinDAPI": "0.0.8",
    "numpy": "2.4.6",
    "pandas": "2.3.3",
    "pydantic": "2.13.4",
    "pypdf": "6.14.2",
    "pyqlib": "0.9.7",
    "PyYAML": "6.0.3",
    "pytest": "9.1.1",
    "uvicorn": "0.51.0",
}


def main() -> int:
    env_name = Path(sys.prefix).name.lower()
    problems = []
    if env_name != "quant":
        problems.append(f"当前环境是 {env_name!r}，预期为 'quant'")
    if sys.version_info[:2] != (3, 11):
        problems.append(f"当前 Python 是 {sys.version.split()[0]}，预期为 3.11.x")
    for package, expected in EXPECTED.items():
        try:
            actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            problems.append(f"缺少 {package}=={expected}")
            continue
        if actual != expected:
            problems.append(f"{package}=={actual}，预期 {expected}")
    if problems:
        print("quant 环境检查失败：")
        for problem in problems:
            print(f"- {problem}")
        return 1
    print(f"quant 环境检查通过：{sys.executable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
