"""Deterministic output validation for deliverables (no model involved).

1. Structure: Markdown tables must have one row per line with consistent columns and a separator row; code
   fences must be balanced; a deliverable must not be wrapped in a code fence. CSV rows need consistent columns.
   JSON must parse.
2. Numeric grounding: every number in a deliverable must appear in the evidence (the user's request, tool results
   such as `calculate`, files that were read) or be recomputable in one arithmetic step from the user's numbers
   (sum, difference, product, ratio, percent change). Rounding to the shown precision is accepted, and so is a ×100
   percentage form. Dates, clock times and list markers are ignored.
"""
import csv
import io
import json
import re
from typing import NamedTuple

_NUM = re.compile(r"(?<![\w.])[-−]?\d+(?:,\d{3})*(?:\.\d+)?(?![\w])")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?\b")
_TIME = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_LIST = re.compile(r"^(\s*)\d+[.)]\s", re.M)
_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+|\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b|\blocalhost:\d+\b")
_YEAR = re.compile(r"(?<![\d.,])\b(19|20)\d{2}\b(?![.,]\d)")
_HEADING_NUM = re.compile(r"^(\s*#+\s*)\d+(?:\.\d+)*[.)]?\s", re.M)
_SEP_CELL = re.compile(r"^\s*:?-{3,}:?\s*$")
_LABEL = re.compile(r"(?i)\b(step|part|section|phase|option|item|chapter|no\.)\s+\d+\b"
                    r"|(?:[×*]|\\times|\bx)\s*100\b")  # list labels; the ×100 of a percentage formula
_NON_ANSWER = re.compile(r"(?i)\b(please provide|could you (please )?(provide|clarify|specify|share)|"
                         r"i need (more|a|the|some) (information|details|data|plan|instruction|context)|"
                         r"as an ai\b|i (cannot|can't|am unable to) (help|assist|access|complete))")
_EMBEDDED_MD = re.compile(r"```\s*(markdown|md)\b", re.I)
_COMMENTARY = re.compile(r"(?im)^\W*(let me\b|let's\b|let us\b|i will now\b|i'll now\b|now,? let's\b|"
                         r"it seems (there was|that) an? error|here(?:'s| is) (?:the|your|a)\b.*:\s*$|"
                         r"i hope this|feel free to|let me know)")
_SCRIPTS = (("cjk", re.compile(r"[぀-ヿ㐀-鿿가-힯]")),
            ("arabic", re.compile(r"[؀-ۿ]")), ("cyrillic", re.compile(r"[Ѐ-ӿ]")))


def _script(text: str) -> str:
    """Dominant non-Latin script if it makes up >20% of letters, else 'latin'."""
    letters = sum(ch.isalpha() for ch in text) or 1
    for name, rx in _SCRIPTS:
        if len(rx.findall(text)) / letters > 0.2:
            return name
    return "latin"


def numbers(text: str, strip_layout: bool = True) -> list[tuple[str, float, int, bool]]:
    """(token, value, decimals, is_percent) for every number; layout numbers (dates, list markers…) optionally removed."""
    if strip_layout:
        text = _URL.sub(" ", text)  # URLs/hosts are references, not figures
        text = _DATE.sub(" ", text)
        text = _TIME.sub(" ", text)
        text = _YEAR.sub(" ", text)  # years are context, not computed figures (documented: year claims unchecked)
        text = _LIST.sub(r"\1", text)
        text = _HEADING_NUM.sub(r"\1", text)
        text = _LABEL.sub(" ", text)
    out = []
    for m in _NUM.finditer(text):
        tok = m.group(0).replace("−", "-").replace(",", "")
        dec = len(tok.split(".")[1]) if "." in tok else 0
        pct = bool(re.match(r"\s*(%|percent\b)", text[m.end():m.end() + 9], re.I))
        out.append((m.group(0), float(tok), dec, pct))
    return out


def evidence_values(texts: list[str]) -> set[float]:
    """Numbers usable as evidence. Dates and clock times are excluded: the current time in the system prompt would
    otherwise 'ground' any number from 0 to 59 (a real false acceptance seen in trials)."""
    vals: set[float] = set()
    for t in texts:
        t = _TIME.sub(" ", _DATE.sub(" ", t))
        for _, v, *_ in numbers(t, strip_layout=False):
            vals.add(v)
            vals.add(-v)
    return vals


def ordered_values(texts: list[str]) -> list[float]:
    """Numbers in order of appearance (user's figures first), deduplicated; used as the recomputation base."""
    seen: dict[float, None] = {}
    for t in texts:
        for _, v, *_ in numbers(_TIME.sub(" ", _DATE.sub(" ", t)), strip_layout=False):
            seen.setdefault(abs(v), None)
    return list(seen)


class Derived(NamedTuple):
    plain: set[float]    # sums, differences, totals, means
    percent: set[float]  # percent change and percent share: the only values a "N%" figure may be recomputed as


def derivations(base, limit: int = 30) -> Derived:
    """Values one step away from the base figures (the user's own first, then data returned by tools), using only the
    operations reports typically state: difference, sum, percent change (a-b)/b*100, percent share a/b*100, plus the
    total and mean of the first figures. Used to verify a derived number by recomputing it, not to trust the model.
    Deliberately narrow: products, plain ratios and calculate results produced real false acceptances
    (4 × 12.5 = "50%", a wrong formula's "-20%")."""
    nums = list(dict.fromkeys(abs(x) for x in base))[:limit]
    plain, pct = set(), set()
    for a in nums:
        for b in nums:
            plain.add(a + b)  # includes doubling (2 + 2 = 4)
            if a == b:
                continue  # identities (a/a = 100%, a-a = 0) would ground anything
            plain.add(a - b)
            if b:
                pct |= {a / b * 100, (a - b) / b * 100}
    if nums:
        plain |= {sum(nums), sum(nums) / len(nums)}
    return Derived(plain | {-x for x in plain}, pct | {-x for x in pct})


def ungrounded(text: str, evidence: set[float], derivable: Derived | None = None) -> list[str]:
    """Numbers in `text` that are neither evidence nor independently recomputable. A figure written as a percentage
    must recompute as a percentage (sales 120→135 is +12.5%, so "15%" fails even though 15 is the difference)."""
    derivable = derivable or Derived(set(), set())
    bad = []

    def hit(pool, v, dec, exact_int=False, times100=False):
        if exact_int and dec == 0:  # recomputed integers must match exactly; rounding only for shown decimals
            return any(abs(e - v) < 1e-9 or (times100 and abs(e * 100 - v) < 1e-9) for e in pool)
        return any(abs(round(e, dec) - v) < 1e-9 or (times100 and abs(round(e * 100, dec) - v) < 1e-9) for e in pool)
    for tok, v, dec, pct in numbers(text):
        recomputed = hit(derivable.percent, v, dec, exact_int=True) if pct else             hit(derivable.plain | derivable.percent, v, dec, exact_int=True)
        if not hit(evidence, v, dec, times100=pct) and not recomputed and tok not in bad:
            bad.append(tok)
    return bad


def content_issues(name: str, content: str, request: str) -> list[str]:
    """Deterministic signs that a deliverable does not do the job (no model judgement involved)."""
    issues = []
    if _NON_ANSWER.search(content):
        issues.append(f"{name}: asks for clarification or declines instead of delivering the requested content")
    given = {v for _, v, *_ in numbers(request)}
    if len(given) >= 2 and not ({v for _, v, *_ in numbers(content)} & given):
        issues.append(f"{name}: does not use any of the figures given in the request")
    if _COMMENTARY.search(content):
        issues.append(f"{name}: contains assistant commentary (e.g. 'let me…', 'it seems there was an error'); "
                      "the file must contain only the deliverable")
    if _script(request) != _script(content):
        issues.append(f"{name}: written in a different script/language than the request")
    if name.lower().endswith(".md") and _EMBEDDED_MD.search(content):
        issues.append(f"{name}: contains an embedded ```markdown block; write the Markdown directly, without commentary")
    return issues


def structure_issues(name: str, content: str) -> list[str]:
    issues = []
    low = name.lower()
    if low.endswith(".json"):
        try:
            json.loads(content)
        except json.JSONDecodeError as e:
            issues.append(f"{name}: invalid JSON ({e.msg})")
        return issues
    if low.endswith(".csv"):
        rows = [r for r in csv.reader(io.StringIO(content)) if r]
        if len(rows) < 2:
            issues.append(f"{name}: CSV needs a header and at least one row")
        elif len({len(r) for r in rows}) > 1:
            issues.append(f"{name}: CSV rows have different numbers of columns")
        return issues
    if not content.strip():
        return [f"{name}: empty"]
    if low.endswith(".md"):
        stripped = content.strip()
        if stripped.startswith("```") and stripped.endswith("```"):
            issues.append(f"{name}: the whole document is wrapped in a code fence; write plain Markdown")
        if content.count("```") % 2:
            issues.append(f"{name}: unbalanced code fence")
        table: list[str] = []
        for line in [*content.splitlines(), ""]:
            if line.lstrip().startswith("|"):
                table.append(line.strip())
                continue
            if table:
                issues += _table_issues(name, table)
                table = []
    return issues


def _cells(row: str) -> list[str]:
    return row.strip().strip("|").split("|")


def _table_issues(name: str, rows: list[str]) -> list[str]:
    issues = []
    for r in rows:
        cells = _cells(r)
        seps = sum(bool(_SEP_CELL.match(c)) for c in cells)
        if seps and seps != len(cells):
            return [f"{name}: malformed table: separator cells mixed with data on one line ({r[:60]}…)"]
    if len(rows) < 2 or not all(_SEP_CELL.match(c) for c in _cells(rows[1])):
        issues.append(f"{name}: table is missing its header separator row (| --- | --- |)")
    if len({len(_cells(r)) for r in rows}) > 1:
        issues.append(f"{name}: table rows have different numbers of columns")
    return issues


if __name__ == "__main__":
    bad = ("| Month | Sales | Returns | |---|---|---| | September | $120 | 0 | |---|---|---| "
           "| Percentage Change | 11.67% | 100% |")
    assert structure_issues("r.md", bad)
    good = "# Sales\n\n| Month | Sales |\n| --- | --- |\n| Sep | 120 |\n| Oct | 135 |\n\nGrowth: 12.5% (2026-10-18)\n"
    assert structure_issues("r.md", good) == []
    ev = evidence_values(["September sales 120, October sales 135", '{"result": 12.5}'])
    assert ungrounded(good, ev) == []
    assert ungrounded("Growth 11.67%, returns 0", ev) == ["11.67%".rstrip("%"), "0"]
    assert ungrounded("Growth 12.50%", ev) == [] and ungrounded("1. first\n2. second", ev) == []
    assert ungrounded("ratio 0.125", evidence_values(["12.5"])) == ["0.125"]
    req = evidence_values(["September sales 120, October sales 135"])
    assert ungrounded("Sales grew 12.5% (+15), total 255", req, derivations(req)) == []  # recomputed, correct
    assert ungrounded("Sales grew 11.67%", req, derivations(req)) == ["11.67"]  # the earlier wrong value
    assert content_issues("r.md", "Please provide a clear instruction.", "sales 120 and 135")
    assert content_issues("r.md", "# Report\nSales were fine.", "September sales 120, October sales 135")
    assert content_issues("r.md", "Here it is:\n```markdown\n# R\n120\n```", "sales 120, 135")
    assert content_issues("r.md", "# Report\nSales rose from 120 to 135.", "sales 120, 135") == []
    assert ungrounded("Step 1: compute. Step 2: report 12.5%", req, derivations(req)) == []
    assert ungrounded("(135 - 120) / 120 × 100 = 12.5%", req, derivations(req)) == []
    assert ungrounded("Sales doubled: 100% growth", req, derivations(req)) == ["100"]  # a claimed 100 still fails
    sales = "September sales 120, October sales 135; returns 4 and 9"
    assert ungrounded("Change: -20% (a decrease)", evidence_values([sales]), derivations(ordered_values([sales]))) == ["-20"]
    assert content_issues("r.md", "# Report\n\nIt seems there was an error. Sales 120 → 135.", sales)
    assert content_issues("r.md", "# 报告\n2023年9月销售额为120万元，10月为135万元。", sales)
    assert content_issues("r.md", "# Report\n\nSales rose from 120 to 135 (+12.5%).", sales) == []
    assert ungrounded("Sales rose 15% (+15 units, 15 more)", evidence_values([sales]),
                      derivations(ordered_values([sales]))) == ["15%".rstrip("%")]  # only the percentage is wrong
    four_nine = evidence_values(["4 and 9"])
    assert ungrounded("ratio 2", four_nine, derivations(four_nine)) == ["2"]  # 9/4 = 2.25 is not "2"
    print("validation ok")
