"""Narrow structural compatibility fix for the pinned jianpu-ly 1.889.

The canonical upstream source is verified against a code-pinned release hash,
not just the mutable local runtime manifest. Do not alter the installed runtime.
The AST patch keeps transpose blocks inside repeat/alternative blocks and
migrates the pinned Mac font fallback to LilyPond 2.26 syntax. It does not
change notes, pitch coordinates or durations.
"""
from __future__ import annotations

import ast
import hashlib

JIANPU_LY_1889_SOURCE_SHA256 = "0890ed37035cf61a218bcf259153079ff12add7a6be8cde6a3dc493ecdab807c"


def compiler_ast(source: str, filename: str) -> ast.Module:
    if hashlib.sha256(source.encode("utf-8")).hexdigest() != JIANPU_LY_1889_SOURCE_SHA256:
        raise RuntimeError("Pinned jianpu-ly release hash not recognized; refusing to apply compatibility changes")
    tree = ast.parse(source, filename=filename)
    # The upstream Mac-only fallback uses set-global-fonts, removed in 2.26.
    # Change the verified compiler literal, not arbitrary user score text.
    legacy_fonts = '''  #(define fonts
    (set-global-fonts
     #:roman "Source Serif Pro,Source Han Serif SC,Times New Roman,Arial Unicode MS"
     #:factor (/ staff-height pt 20)
    ))'''
    font_literals = [node for node in ast.walk(tree)
                     if isinstance(node, ast.Constant) and isinstance(node.value, str)
                     and legacy_fonts in node.value]
    if len(font_literals) != 1:
        raise RuntimeError("Pinned jianpu-ly Mac font fallback is not recognized")
    font_literals[0].value = font_literals[0].value.replace(
        legacy_fonts,
        '  property-defaults.fonts.serif = "Source Serif Pro,Source Han Serif SC,Times New Roman,Arial Unicode MS"',
    )
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "getLY"]
    if len(functions) != 1:
        raise RuntimeError("Pinned jianpu-ly getLY structure is not recognized")
    function = functions[0]
    # SeparateTimesig is a numbered-staff presentation option. Upstream also
    # emits its Score-level mark while doubling the Western staff, causing
    # duplicate simultaneous marks (and a discarded-event warning). The Western
    # staff already emits a real \\time; retain that, and the Jianpu mark, intact.
    timesig_marks = [node for node in ast.walk(function)
                    if isinstance(node, ast.If)
                    and ast.unparse(node.test) == "notehead_markup.separateTimesig and (not midi)"]
    if len(timesig_marks) != 1:
        raise RuntimeError("Pinned jianpu-ly separate time-signature structure is not recognized")
    timesig_marks[0].test = ast.BoolOp(op=ast.And(), values=[
        timesig_marks[0].test,
        ast.UnaryOp(op=ast.Not(), operand=ast.Name(id="western", ctx=ast.Load())),
    ])
    appends = [node for node in ast.walk(function)
               if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
               and isinstance(node.value.func, ast.Attribute)
               and isinstance(node.value.func.value, ast.Name)
               and node.value.func.value.id == "out" and node.value.func.attr == "append"
               and len(node.value.args) == 1
               and any(isinstance(child, ast.Constant) and child.value == "\\transpose "
                       for child in ast.walk(node.value.args[0]))]
    loops = [node for node in ast.walk(function)
             if isinstance(node, ast.For) and isinstance(node.target, ast.Name) and node.target.id == "word"]
    if len(appends) != 1 or len(loops) != 1:
        raise RuntimeError("Pinned jianpu-ly transpose/word structure is not recognized")
    target = appends[0]

    class CaptureTranspose(ast.NodeTransformer):
        def visit_Expr(self, node: ast.Expr):
            if node is not target:
                return node
            assignment = ast.Assign(targets=[ast.Name(id="_elren_transpose", ctx=ast.Store())], value=node.value.args[0])
            node.value.args[0] = ast.Name(id="_elren_transpose", ctx=ast.Load())
            return [ast.copy_location(assignment, node), node]

    CaptureTranspose().visit(function)
    function.body.insert(0, ast.parse("_elren_transpose = None").body[0])
    loop = loops[0]
    dispatches = [index for index, node in enumerate(loop.body)
                  if isinstance(node, ast.If) and isinstance(node.test, ast.Call)
                  and isinstance(node.test.func, ast.Attribute)
                  and isinstance(node.test.func.value, ast.Name) and node.test.func.value.id == "word"
                  and node.test.func.attr == "startswith" and len(node.test.args) == 1
                  and isinstance(node.test.args[0], ast.Constant) and node.test.args[0].value == "%"]
    if len(dispatches) != 1:
        raise RuntimeError("Pinned jianpu-ly dispatch structure is not recognized")
    prelude = ast.parse(r'''
if midi or western:
    if word in {"R{", "A{", "}"} or re.fullmatch(r"R[1-9][0-9]*\{", word) or (word == "|" and repeatStack and repeatStack[-1][0] == 2):
        if inTranspose:
            out.append("}")
            inTranspose = 0
    elif not inTranspose and _elren_transpose and (re.fullmatch(note_regex, word) or word.startswith("g[") or word.startswith("R*")):
        out.append(_elren_transpose)
        inTranspose = 1
''').body
    loop.body[dispatches[0]:dispatches[0]] = prelude
    return ast.fix_missing_locations(tree)
