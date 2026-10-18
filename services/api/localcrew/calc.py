"""Safe arithmetic for the `calculate` tool: an AST whitelist, no eval, bounded size and magnitude."""
import ast
import math
import operator

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {"round": round, "abs": abs, "min": min, "max": max, "sum": lambda *a: sum(a), "sqrt": math.sqrt,
          "avg": lambda *a: sum(a) / len(a)}
MAX_NODES, MAX_ABS = 200, 1e100


class CalcError(ValueError):
    pass


def evaluate(expr: str) -> float | int:
    try:
        tree = ast.parse(expr.replace("^", "**"), mode="eval")  # no thousands separators: write 1234, not 1,234
    except SyntaxError as e:
        raise CalcError(f"not an arithmetic expression: {e.msg}") from e
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
        raise CalcError("expression too long")

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and type(n.value) in (int, float):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and (abs(b) > 100 or abs(a) > 1e6):
                raise CalcError("exponent too large")
            try:
                r = _BIN[type(n.op)](a, b)
            except ZeroDivisionError as e:
                raise CalcError("division by zero") from e
        elif isinstance(n, ast.UnaryOp) and type(n.op) in _UNARY:
            r = _UNARY[type(n.op)](ev(n.operand))
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords:
            r = _FUNCS[n.func.id](*[ev(x) for x in n.args])
        else:
            raise CalcError(f"unsupported element: {type(n).__name__}")
        if isinstance(r, complex) or abs(r) > MAX_ABS:
            raise CalcError("result out of range")
        return r

    r = ev(tree)
    return int(r) if isinstance(r, float) and r.is_integer() else (round(r, 10) if isinstance(r, float) else r)


if __name__ == "__main__":
    assert evaluate("(135-120)/120*100") == 12.5
    assert evaluate("round((135-120)/120*100, 2)") == 12.5
    assert evaluate("2^10") == 1024 and evaluate("avg(120, 135)") == 127.5
    for bad in ("__import__('os')", "(1).__class__", "9**9**9", "1/0", "a+1", "[1,2]"):
        try:
            evaluate(bad)
            raise AssertionError(bad)
        except CalcError:
            pass
    print("calc ok")
