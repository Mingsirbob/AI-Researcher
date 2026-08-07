from __future__ import annotations

import json
import math
import statistics
import sys
import builtins


SAFE_BUILTINS = {
    name: getattr(builtins, name)
    for name in (
        "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int",
        "len", "list", "map", "max", "min", "range", "reversed", "round", "set",
        "sorted", "str", "sum", "tuple", "zip",
    )
}


def main() -> None:
    payload = json.load(sys.stdin)
    namespace = {"__builtins__": SAFE_BUILTINS, "math": math, "statistics": statistics}
    exec(compile(payload["source"], "strategy.py", "exec"), namespace, namespace)
    result = namespace["generate_signals"](payload["context"])
    sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
