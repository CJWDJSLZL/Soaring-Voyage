"""A tiny AST-to-SymPy parser; deliberately avoids eval and arbitrary symbols."""

from __future__ import annotations

import ast

import sympy

_MAX_LENGTH = 128
_MAX_NODES = 64
_MAX_EXPONENT = 12


def _convert(node: ast.AST) -> sympy.Expr:
    if isinstance(node, ast.Expression):
        return _convert(node.body)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return sympy.Rational(str(node.value))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _convert(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
        left, right = _convert(node.left), _convert(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            if right == 0:
                raise ValueError("division by zero")
            return left / right
        if not right.is_Integer or abs(int(right)) > _MAX_EXPONENT:
            raise ValueError("unsafe exponent")
        return left**right
    raise ValueError(f"unsupported expression node: {type(node).__name__}")


def safe_parse_math(expression: str) -> sympy.Expr | None:
    text = expression.strip().replace("×", "*").replace("÷", "/").replace("^", "**").replace("−", "-")
    if not text or len(text) > _MAX_LENGTH:
        return None
    try:
        tree = ast.parse(text, mode="eval")
        if sum(1 for _ in ast.walk(tree)) > _MAX_NODES:
            return None
        result = _convert(tree)
        if result.has(sympy.zoo, sympy.nan, sympy.oo, -sympy.oo) or not result.is_number:
            return None
        return result
    except (SyntaxError, TypeError, ValueError, ZeroDivisionError, OverflowError, MemoryError):
        return None


def safe_math_equal(left: str, right: str) -> bool | None:
    """Return exact equality, or None when either expression is not safely parseable."""
    left_expr, right_expr = safe_parse_math(left), safe_parse_math(right)
    if left_expr is None or right_expr is None:
        return None
    try:
        return bool(sympy.simplify(left_expr - right_expr) == 0)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
