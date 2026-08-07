from __future__ import annotations

import ast
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path


MAX_SOURCE_LENGTH = 50_000
FORBIDDEN_NAMES = {
    "open", "exec", "eval", "compile", "__import__", "input", "help", "breakpoint",
    "globals", "locals", "vars", "getattr", "setattr", "delattr",
}
FORBIDDEN_NODES = (
    ast.Import, ast.ImportFrom, ast.ClassDef, ast.AsyncFunctionDef, ast.Global, ast.Nonlocal,
)


def validate_strategy_source(source: str) -> dict:
    errors: list[dict] = []
    if not source.strip():
        errors.append({"code": "empty_source", "message": "策略代码不能为空"})
        return _result(source, errors)
    if len(source) > MAX_SOURCE_LENGTH:
        errors.append({"code": "source_too_large", "message": "策略代码超过 50000 字符"})
        return _result(source, errors)
    try:
        tree = ast.parse(source, filename="strategy.py")
    except SyntaxError as exc:
        errors.append({"code": "syntax_error", "message": f"第 {exc.lineno} 行：{exc.msg}"})
        return _result(source, errors)

    entrypoints = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "generate_signals"
    ]
    if len(entrypoints) != 1:
        errors.append({"code": "entrypoint", "message": "必须且只能定义一个 generate_signals(context)"})
    elif len(entrypoints[0].args.args) != 1 or entrypoints[0].args.vararg or entrypoints[0].args.kwarg:
        errors.append({"code": "signature", "message": "generate_signals 必须只接收一个 context 参数"})

    for node in ast.walk(tree):
        if isinstance(node, FORBIDDEN_NODES):
            errors.append({"code": "forbidden_syntax", "message": f"禁止使用 {type(node).__name__}"})
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            errors.append({"code": "forbidden_name", "message": f"禁止使用 {node.id}"})
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            errors.append({"code": "dunder_access", "message": "禁止访问双下划线属性"})
    unique = list({(item["code"], item["message"]): item for item in errors}.values())
    return _result(source, unique)


def _result(source: str, errors: list[dict]) -> dict:
    return {
        "status": "valid" if not errors else "invalid",
        "errors": errors,
        "checks": ["syntax", "entrypoint", "restricted_ast"],
        "source_hash": hashlib.sha256(source.encode("utf-8")).hexdigest(),
    }


class CodeStrategyRuntime:
    def __init__(self, timeout_seconds: float = 3.0):
        self.timeout_seconds = timeout_seconds

    def execute(self, source: str, context: dict) -> list[dict]:
        validation = validate_strategy_source(source)
        if validation["status"] != "valid":
            raise ValueError("策略代码未通过校验：" + "；".join(
                item["message"] for item in validation["errors"]
            ))
        worker = Path(__file__).with_name("_code_worker.py")
        try:
            result = subprocess.run(
                [sys.executable, "-I", "-X", "utf8", str(worker)],
                input=json.dumps({"source": source, "context": context}, ensure_ascii=False),
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"策略执行超过 {self.timeout_seconds:g} 秒") from exc
        if result.returncode != 0:
            stderr = result.stderr or ""
            message = stderr.strip().splitlines()[-1] if stderr.strip() else "未知错误"
            raise RuntimeError(f"策略执行失败：{message}")
        try:
            rows = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("策略返回值不是合法 JSON") from exc
        return self._validate_output(rows, context)

    @staticmethod
    def _validate_output(rows, context: dict) -> list[dict]:
        if not isinstance(rows, list):
            raise ValueError("generate_signals 必须返回列表")
        allowed = {str(item["security_code"]) for item in context.get("universe", [])}
        seen: set[str] = set()
        normalized = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("策略结果的每一项必须是对象")
            code = str(row.get("security_code") or "").upper()
            if code not in allowed:
                raise ValueError(f"策略返回了股池外证券：{code}")
            if code in seen:
                raise ValueError(f"策略返回重复证券：{code}")
            score = float(row.get("score"))
            if not math.isfinite(score):
                raise ValueError(f"策略分数不是有限数字：{code}")
            seen.add(code)
            normalized.append({
                "security_code": code,
                "score": score,
                "reason": str(row.get("reason") or "代码策略评分"),
            })
        return normalized
