"""Translate OpenSCAD AST nodes into Python code using Piecad."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable

from openscad_parser.ast import getASTfromFile
from openscad_parser.ast.nodes import (
    Assignment,
    AssertOp,
    BlankLine,
    BooleanLiteral,
    CommentLine,
    CommentedExpr,
    CommentSpan,
    EchoOp,
    FunctionDeclaration,
    FunctionLiteral,
    Identifier,
    IncludeStatement,
    ListComprehension,
    ListCompCFor,
    ListCompEach,
    ListCompFor,
    ListCompIf,
    ListCompIfElse,
    ListCompLet,
    LetOp,
    ModularAssert,
    ModularCall,
    ModularEcho,
    ModularFor,
    ModularIf,
    ModularIfElse,
    ModularIntersectionFor,
    ModularLet,
    ModularModifierBackground,
    ModularModifierDisable,
    ModularModifierHighlight,
    ModularModifierShowOnly,
    ModuleInstantiation,
    ModuleDeclaration,
    NamedArgument,
    NumberLiteral,
    PositionalArgument,
    PrimaryCall,
    PrimaryIndex,
    PrimaryMember,
    RangeLiteral,
    RenderExpression,
    StringLiteral,
    UnaryMinusOp,
    UndefinedLiteral,
    UseStatement,
)

_RANGE_HELPER = """def _scadtopy_range(start, step, end):
    if step == 0:
        raise ValueError("OpenSCAD range step cannot be zero")
    distance = (end - start) / step
    if distance < 0:
        return []
    last_index = math.floor(math.nextafter(distance, math.inf))
    return [start + index * step for index in range(last_index + 1)]"""


class TranslationError(ValueError):
    """Raised for malformed input or invalid arguments to supported constructs."""


_BINARY_OPERATORS = {
    "AdditionOp": "+",
    "SubtractionOp": "-",
    "MultiplicationOp": "*",
    "DivisionOp": "/",
    "ModuloOp": "%",
    "ExponentOp": "**",
    "LogicalAndOp": "and",
    "LogicalOrOp": "or",
    "EqualityOp": "==",
    "InequalityOp": "!=",
    "GreaterThanOp": ">",
    "GreaterThanOrEqualOp": ">=",
    "LessThanOp": "<",
    "LessThanOrEqualOp": "<=",
    "BitwiseAndOp": "&",
    "BitwiseOrOp": "|",
    "BitwiseShiftLeftOp": "<<",
    "BitwiseShiftRightOp": ">>",
}

_PIECAD_NAMES = {
    "union": "union",
    "difference": "difference",
    "hull": "hull",
    "intersection": "intersect",
}

_TRANSFORMS = {
    "translate": "translate",
    "rotate": "rotate",
    "scale": "scale",
    "mirror": "mirror",
}


# Piecad's trig functions use degrees like OpenSCAD, so they pass through unchanged.
_MATH_FUNCTIONS = {
    "sqrt": "math.sqrt",
    "floor": "math.floor",
    "ceil": "math.ceil",
    "ln": "math.log",
    "log": "math.log10",
    "exp": "math.exp",
}

try:
    import piecad as _piecad

    _PIECAD_EXPORTS = frozenset(n for n in dir(_piecad) if not n.startswith("_"))
except ImportError:
    _PIECAD_EXPORTS = frozenset()


def _node_name(node: object) -> str:
    return type(node).__name__


def _uses_children(node: object) -> bool:
    if isinstance(node, ModularCall) and node.name.name == "children":
        return True
    if isinstance(node, Identifier) and node.name == "$children":
        return True
    if isinstance(node, (list, tuple)):
        return any(_uses_children(item) for item in node)
    if hasattr(node, "__dict__"):
        return any(
            _uses_children(value)
            for key, value in vars(node).items()
            if key != "position"
        )
    return False


def _all_module_names(nodes: Iterable[object]) -> set[str]:
    names: set[str] = set()
    for node in nodes:
        if isinstance(node, ModuleDeclaration):
            names.add(node.name.name)
            names |= _all_module_names(node.children)
    return names


def _children_modules(nodes: Iterable[object]) -> set[str]:
    names: set[str] = set()
    for node in nodes:
        if isinstance(node, ModuleDeclaration):
            if _uses_children(node.children):
                names.add(node.name.name)
            names |= _children_modules(node.children)
    return names


_TWO_D_CALLS = {"square", "circle", "polygon", "text", "offset", "projection"}
_THREE_D_CALLS = {
    "cube", "sphere", "cylinder", "polyhedron", "linear_extrude", "rotate_extrude",
    "surface", "import",
}


def _is_2d(node: object) -> bool:
    if isinstance(node, (list, tuple)):
        return any(_is_2d(item) for item in node)
    if isinstance(node, ModularCall):
        if node.name.name in _TWO_D_CALLS:
            return True
        if node.name.name in _THREE_D_CALLS:
            return False
    return _is_2d(getattr(node, "children", None) or [])


def _tidy(code: str) -> str:
    """Drop redundant parentheses by round-tripping through the Python parser."""
    try:
        return ast.unparse(ast.parse(code, mode="eval"))
    except SyntaxError:
        return code


def _first_two(code: str) -> str:
    try:
        tree = ast.parse(code, mode="eval").body
    except SyntaxError:
        return f"({code})[:2]"
    if isinstance(tree, ast.List) and len(tree.elts) >= 2:
        return ast.unparse(ast.List(elts=tree.elts[:2], ctx=ast.Load()))
    return f"({code})[:2]"


def _receiver(code: str) -> str:
    """Parenthesize generated code only when it is not already a call chain."""
    try:
        tree = ast.parse(code, mode="eval").body
    except SyntaxError:
        return f"({code})"
    if isinstance(tree, (ast.Call, ast.Name, ast.Attribute, ast.Subscript)):
        return code
    return f"({code})"


def _position(node: object) -> str:
    position = getattr(node, "position", None)
    line = getattr(position, "line", None)
    return f" at line {line}" if line is not None else ""


class _Translator:
    def __init__(self) -> None:
        self.output_stem = ""
        self.base_dir: Path | None = None
        self.function_names: set[str] = set()
        self.uses_vec2 = False
        self.module_names: set[str] = set()
        self.shadowing_names: set[str] = set()
        self.children_modules: set[str] = set()
        self.uses_children_helper = False
        self.uses_linear_extrude_helper = False
        self.uses_text_helper = False
        self.uses_ranges = False
        self.uses_math = False
        self.uses_assert_helper = False
        self.uses_echo_helper = False
        self.fixmes: list[str] = []

    def py_name(self, name: str) -> str:
        return f"{name}_" if name in self.shadowing_names else name

    def translate(self, nodes: Iterable[object]) -> str:
        nodes = list(nodes)
        self.shadowing_names = {
            node.name.name
            for node in nodes
            if isinstance(node, (FunctionDeclaration, ModuleDeclaration))
            and node.name.name in _PIECAD_EXPORTS
        }
        self.uses_ranges = False
        self.uses_math = False
        self.uses_assert_helper = False
        self.uses_echo_helper = False
        self.fixmes = []
        self.uses_children_helper = False
        self.uses_linear_extrude_helper = False
        self.uses_text_helper = False
        self.uses_vec2 = False
        self.function_names = {
            node.name.name for node in nodes if isinstance(node, FunctionDeclaration)
        }
        self.module_names = _all_module_names(nodes)
        self.children_modules = _children_modules(nodes)
        for node in nodes:
            if isinstance(node, UseStatement):
                self.load_used_names(node)
        statements: list[str] = ["_parts = []"]
        models: list[str] = []
        parts_2d: list[str] = []
        parts_3d: list[str] = []
        saw_model = False
        last_statement_is_model = False

        for node in nodes:
            if isinstance(node, Assignment):
                value = self.expression(node.expr)
                if node.name.name == "$fn":
                    statements.append(f"Config.set_default_segments(int({value}))")
                elif node.name.name.startswith("$"):
                    statements.append(
                        f"# UNSUPPORTED: {node.name.name} = {value}"
                    )
                else:
                    statements.append(f"{node.name.name} = {value}")
                last_statement_is_model = False
            elif isinstance(node, ModuleDeclaration):
                statements.extend(self.module_declaration(node))
                last_statement_is_model = False
            elif isinstance(node, FunctionDeclaration):
                statements.extend(self.function_declaration(node))
                last_statement_is_model = False
            elif isinstance(node, ModuleInstantiation):
                if isinstance(node, ModularModifierDisable):
                    self.module_call(node)
                    last_statement_is_model = False
                elif isinstance(node, ModularEcho) and not node.children:
                    statements.append(self.module_call(node))
                    last_statement_is_model = False
                elif isinstance(node, ModularAssert) and not node.children:
                    statements.append(self.module_call(node))
                    last_statement_is_model = False
                else:
                    saw_model = True
                    model = self.module_call(node)
                    if model != "None":
                        models.append(model)
                        (parts_2d if _is_2d(node) else parts_3d).append(model)
                        statements.append(f"_parts.append({model})")
                        last_statement_is_model = True
                    else:
                        last_statement_is_model = False
            elif isinstance(node, (CommentLine, CommentSpan)):
                if (
                    isinstance(node, CommentLine)
                    and node.same_line
                    and last_statement_is_model
                ):
                    suffix = (
                        f"#{node.text}"
                        if node.text.startswith((" ", "\t"))
                        else f"# {node.text}"
                    )
                    statements[-1] = f"{statements[-1].rstrip()}  {suffix}"
                else:
                    self.append_comment(statements, node)
                last_statement_is_model = False
            elif isinstance(node, BlankLine):
                statements.append("")
                last_statement_is_model = False
            elif isinstance(node, UseStatement):
                statements.append(self.use_statement(node))
                last_statement_is_model = False
            elif isinstance(node, IncludeStatement):
                statements.append(
                    f"pass  # FIXME: include {node.filepath.val!r} was not expanded"
                )
                last_statement_is_model = False
            else:
                statements.append(f"pass  # FIXME: {_node_name(node)} is not translated")
                last_statement_is_model = False

        if saw_model:
            statements.extend(
                [
                    "",
                    'if __name__ == "__main__":',
                    *self.save_lines(bool(parts_3d), bool(parts_2d)),
                    "    for _part in _parts:",
                    "        view(_part)",
                    "    view_all_now()",
                ]
            )

        statements.extend(self.fixmes)
        body = "\n".join(statements).rstrip()
        prefix = ""
        if self.uses_ranges:
            prefix = f"import math\n\n{_RANGE_HELPER}\n\n"
        elif self.uses_math:
            prefix = "import math\n\n"
        if self.uses_assert_helper:
            prefix += (
                "def _scadtopy_assert(condition, message, value):\n"
                "    if not condition:\n"
                "        raise AssertionError(message)\n"
                "    return value()\n\n"
            )
        if self.uses_echo_helper:
            prefix += (
                "import json\n\n"
                "def _scadtopy_echo_repr(value):\n"
                "    if value is True:\n"
                "        return 'true'\n"
                "    if value is False:\n"
                "        return 'false'\n"
                "    if value is None:\n"
                "        return 'undef'\n"
                "    if isinstance(value, str):\n"
                "        return json.dumps(value, ensure_ascii=False)\n"
                "    if isinstance(value, (list, tuple)):\n"
                "        return '[' + ', '.join(_scadtopy_echo_repr(item) for item in value) + ']'\n"
                "    if isinstance(value, float) and value.is_integer():\n"
                "        return str(int(value))\n"
                "    return str(value)\n\n"
                "def _scadtopy_echo(values, result):\n"
                "    rendered = []\n"
                "    for name, value in values:\n"
                "        text = _scadtopy_echo_repr(value)\n"
                "        rendered.append(f'{name} = {text}' if name is not None else text)\n"
                "    print('ECHO: ' + ', '.join(rendered))\n"
                "    return result()\n\n"
            )
        prefix += "from piecad import *\n"
        if self.uses_vec2:
            prefix += "from piecad.lin_math import Vec2\n"
        prefix += "\n"
        if self.uses_children_helper:
            prefix += (
                "def _scadtopy_children(children, selector=None):\n"
                "    import warnings\n"
                "    if selector is None:\n"
                "        picked = list(children)\n"
                "    else:\n"
                "        items = selector if isinstance(selector, (list, tuple, range)) "
                "else [selector]\n"
                "        picked = []\n"
                "        for item in items:\n"
                "            index = int(item)\n"
                "            if 0 <= index < len(children):\n"
                "                picked.append(children[index])\n"
                "            else:\n"
                "                warnings.warn(f'children({index}) is out of range')\n"
                "    picked = [child for child in picked if child is not None]\n"
                "    return union(*picked) if len(picked) > 1 "
                "else (picked[0] if picked else None)\n\n"
            )
        if self.uses_text_helper:
            prefix += (
                "def _scadtopy_text(content, size, halign, valign):\n"
                "    obj = text(size=size, text=content)\n"
                "    min_x, min_y, max_x, max_y = obj.bounding_box()\n"
                "    dx = {'left': 0, 'center': -(min_x + max_x) / 2, 'right': -max_x}[halign]\n"
                "    dy = {\n"
                "        'baseline': 0, 'top': -max_y,\n"
                "        'center': -(min_y + max_y) / 2, 'bottom': -min_y,\n"
                "    }[valign]\n"
                "    return obj.translate([dx, dy])\n\n"
            )
        if self.uses_linear_extrude_helper:
            prefix += (
                "def _scadtopy_linear_extrude(obj, height, center, twist, slices, scale):\n"
                "    if not isinstance(scale, (list, tuple)):\n"
                "        scale = (scale, scale)\n"
                "    divisions = int(slices) if slices else (\n"
                "        max(1, math.ceil(abs(twist) / 360 * Config.get_default_segments()))\n"
                "        if twist else 1\n"
                "    )\n"
                "    # Piecad twists counter-clockwise; OpenSCAD positive twist is clockwise.\n"
                "    flip = twist > 0\n"
                "    if flip:\n"
                "        obj = obj.mirror((True, False))\n"
                "    result = extrude_transforming(obj, height, divisions, abs(twist), tuple(scale))\n"
                "    if flip:\n"
                "        result = result.mirror((True, False, False))\n"
                "    return result.translate([0, 0, -height / 2]) if center else result\n\n"
            )
        return prefix + (body + "\n" if body else "")

    def cross(self, nodes: list[object], arguments: list[str]) -> str:
        values = [getattr(argument, "expr", None) for argument in nodes]
        if all(
            isinstance(value, ListComprehension) and len(value.elements) == 2
            for value in values
        ):
            self.uses_vec2 = True
            return f"Vec2.cross({', '.join(arguments)})[2]"
        return f"Vec3.cross({', '.join(arguments)})"

    def used_nodes(self, node: UseStatement) -> list[object]:
        if self.base_dir is None:
            return []
        path = self.base_dir / node.filepath.val
        if not path.is_file():
            return []
        return getASTfromFile(str(path)) or []

    def load_used_names(self, node: UseStatement) -> None:
        nodes = self.used_nodes(node)
        self.module_names |= _all_module_names(nodes)
        self.children_modules |= _children_modules(nodes)
        self.function_names |= {
            declaration.name.name
            for declaration in nodes
            if isinstance(declaration, FunctionDeclaration)
        }
        self.shadowing_names |= {
            declaration.name.name
            for declaration in nodes
            if isinstance(declaration, (FunctionDeclaration, ModuleDeclaration))
            and declaration.name.name in _PIECAD_EXPORTS
        }

    def use_statement(self, node: UseStatement) -> str:
        module = Path(node.filepath.val).stem
        if not module.isidentifier():
            return f"pass  # FIXME: cannot import {node.filepath.val!r} as a Python module"
        return f"from {module} import *"

    def save_lines(self, has_3d: bool, has_2d: bool) -> list[str]:
        if not self.output_stem or not (has_3d or has_2d):
            return []
        if has_3d:
            parts = (
                "[part for part in _parts if isinstance(part, Obj3d)]"
                if has_2d
                else "_parts"
            )
            return [f'    save("{self.output_stem}.3mf", union(*{parts}))']
        return [f'    save("{self.output_stem}.svg", union(*_parts))']

    def module_declaration(self, node: ModuleDeclaration) -> list[str]:
        params = self.parameters(node.parameters)
        if _uses_children(node.children):
            params = f"{params}, _children=()" if params else "_children=()"
        lines = [f"def {self.py_name(node.name.name)}({params}):"]
        assignments: list[str] = []
        geometry: list[str] = []
        for child in node.children:
            if isinstance(child, Assignment):
                value = self.expression(child.expr)
                if child.name.name.startswith("$"):
                    assignments.append(
                        f"    # UNSUPPORTED: {child.name.name} = {value}"
                    )
                else:
                    assignments.append(f"    {child.name.name} = {value}")
            elif isinstance(child, ModularEcho) and not child.children:
                assignments.append(f"    {self.module_call(child)}")
            elif isinstance(child, ModuleInstantiation):
                translated = self.module_call(child)
                if translated != "None":
                    geometry.append(translated)
            elif isinstance(child, FunctionDeclaration):
                lines.extend(f"    {line}" for line in self.function_declaration(child))
            elif isinstance(child, ModuleDeclaration):
                lines.extend(f"    {line}" for line in self.module_declaration(child))
            elif isinstance(child, (CommentLine, CommentSpan)):
                self.append_comment(assignments, child, indent="    ")
            elif isinstance(child, BlankLine):
                assignments.append("")
            else:
                assignments.append(f"    pass  # FIXME: {_node_name(child)} is not translated")

        if assignments:
            lines.extend(assignments)
        if geometry:
            result = geometry[0] if len(geometry) == 1 else f"union({', '.join(geometry)})"
            lines.append(f"    return {result}")
        else:
            lines.append("    return None")
        return lines

    def function_declaration(self, node: FunctionDeclaration) -> list[str]:
        params = self.parameters(node.parameters)
        return [f"def {self.py_name(node.name.name)}({params}):", f"    return {self.expression(node.expr)}"]

    def parameters(self, parameters: list[object]) -> str:
        return ", ".join(
            parameter.name.name
            + (
                f"={self.expression(parameter.default)}"
                if parameter.default is not None
                else ""
            )
            for parameter in parameters
        )

    def append_comment(
        self,
        lines: list[str],
        node: CommentLine | CommentSpan,
        indent: str = "",
    ) -> None:
        if isinstance(node, CommentLine):
            text = node.text
            if node.same_line and lines and lines[-1]:
                suffix = f"#{text}" if text.startswith((" ", "\t")) else f"# {text}"
                lines[-1] = f"{lines[-1].rstrip()}  {suffix}"
            else:
                lines.append(f"{indent}#{text}")
            return

        text_lines = node.text.splitlines() or [""]
        lines.extend(
            f"{indent}# {text}" if text else f"{indent}#"
            for text in text_lines
        )

    def assert_values(self, arguments: list[object], node: object) -> tuple[str, str]:
        condition = self.argument_expression(arguments, ("condition",), 0)
        message = self.argument_expression(arguments, ("message",), 1)
        condition_value = self.expression(condition) if condition is not None else "False"
        message_value = (
            self.expression(message)
            if message is not None
            else repr(f"OpenSCAD assert failed{_position(node)}")
        )
        return condition_value, message_value

    def echo_expression(self, arguments: list[object], result: str) -> str:
        self.uses_echo_helper = True
        values = []
        for argument in arguments:
            if isinstance(argument, PositionalArgument):
                values.append(f"(None, {self.expression(argument.expr)})")
            elif isinstance(argument, NamedArgument):
                values.append(
                    f"({argument.name.name!r}, {self.expression(argument.expr)})"
                )
            else:
                self.note_fixme(argument, "unknown echo argument is not translated")
        return f"_scadtopy_echo([{', '.join(values)}], lambda: {result})"

    def module_list(self, nodes: object) -> list[object]:
        if isinstance(nodes, list):
            return nodes
        return [nodes]

    def module_group(self, nodes: object) -> str:
        geometries: list[str] = []
        for node in self.module_list(nodes):
            if isinstance(node, (CommentLine, CommentSpan, BlankLine)):
                continue
            if isinstance(node, ModularModifierDisable):
                self.module_call(node)
                continue
            geometry = self.module_call(node)
            if geometry != "None":
                geometries.append(geometry)
        if not geometries:
            return "None"
        return self.combine(geometries)

    def module_loop(
        self, assignments: list[Assignment], body: object, operation: str = "union"
    ) -> str:
        value = self.module_group(body)
        clauses = " ".join(
            f"for {assignment.name.name} in {self.expression(assignment.expr)}"
            for assignment in assignments
        )
        return f"{operation}(*[{value} {clauses}])"

    def module_call(self, node: object) -> str:
        return _tidy(self._module_call(node))

    def _module_call(self, node: object) -> str:
        if isinstance(node, ModularFor):
            return self.module_loop(node.assignments, node.body)
        if isinstance(node, ModularIntersectionFor):
            return self.module_loop(node.assignments, node.body, "intersect")
        if isinstance(node, ModularLet):
            body = self.module_group(node.children)
            return self.let_expression(node.assignments, body)
        if isinstance(node, ModularIf):
            return f"({self.module_group(node.true_branch)} if {self.expression(node.condition)} else None)"
        if isinstance(node, ModularIfElse):
            return (
                f"({self.module_group(node.true_branch)} if "
                f"{self.expression(node.condition)} else "
                f"{self.module_group(node.false_branch)})"
            )
        if isinstance(node, ModularEcho):
            return self.echo_expression(node.arguments, self.module_group(node.children))
        if isinstance(node, ModularAssert):
            self.uses_assert_helper = True
            condition, message = self.assert_values(node.arguments, node)
            return (
                f"_scadtopy_assert({condition}, {message}, "
                f"lambda: {self.module_group(node.children)})"
            )
        if isinstance(
            node,
            (
                ModularModifierShowOnly,
                ModularModifierHighlight,
                ModularModifierBackground,
            ),
        ):
            self.note_fixme(node, "display modifier is not represented by Piecad")
            return self.module_call(node.child)
        if isinstance(node, ModularModifierDisable):
            self.note_fixme(node, "disabled geometry is not translated")
            return "None"
        if not isinstance(node, ModularCall):
            self.note_fixme(node, f"{_node_name(node)} is not translated")
            return "None"

        name = node.name.name
        arguments = self.call_arguments(node.arguments)
        source_children = [
            child
            for child in self.module_list(node.children)
            if not isinstance(child, (CommentLine, CommentSpan, BlankLine))
        ]
        children = [self.module_call(child) for child in source_children]
        if source_children and all(child == "None" for child in children):
            return "None"
        children = [child for child in children if child != "None"]

        if name in _TRANSFORMS:
            if not children:
                raise TranslationError(f"{name}() requires a child geometry{_position(node)}")
            base = self.combine(children)
            return self.transform_call(name, node, base)

        if name == "linear_extrude":
            if not children:
                raise TranslationError(f"linear_extrude() requires a child geometry{_position(node)}")
            self.reject_unexpected_named_arguments(
                node, {"height", "center", "twist", "slices", "scale", "convexity", "$fn", "$fa", "$fs"}
            )
            height = self.argument_value(node.arguments, ("height",), 0)
            if height is None:
                raise TranslationError(f"linear_extrude() requires height{_position(node)}")
            center = self.argument_value(node.arguments, ("center",), 1)
            twist = self.argument_value(node.arguments, ("twist",), -1)
            slices = self.argument_value(node.arguments, ("slices",), -1)
            scale = self.argument_value(node.arguments, ("scale",), -1)
            if twist is None and slices is None and scale is None:
                extruded = f"{_receiver(self.combine(children))}.extrude({height})"
                if center == "True":
                    extruded += f".translate([0, 0, -({height}) / 2])"
                elif center is not None and center != "False":
                    extruded += f".translate([0, 0, (-({height}) / 2 if {center} else 0)])"
                return _tidy(extruded)
            self.uses_linear_extrude_helper = True
            self.uses_math = True
            return (
                f"_scadtopy_linear_extrude({self.combine(children)}, {height}, "
                f"{center or 'False'}, {twist or '0'}, {slices or 'None'}, {scale or '1'})"
            )

        if name == "rotate_extrude":
            self.reject_unexpected_named_arguments(
                node, {"angle", "convexity", "$fn", "$fa", "$fs"}
            )
            if not children:
                return "None"
            angle = self.argument_value(node.arguments, ("angle",), 0)
            segments = self.argument_value(node.arguments, ("$fn",), -1)
            extra = f", int({segments})" if segments is not None else ""
            if extra and angle is None:
                angle = "360.0"
            if angle is None:
                return f"revolve({self.combine(children)})"
            return f"revolve({self.combine(children)}, {angle}{extra})"

        if name in _PIECAD_NAMES:
            if not children:
                raise TranslationError(f"{name}() requires child geometry{_position(node)}")
            if node.arguments:
                raise TranslationError(f"{name}() does not accept arguments{_position(node)}")
            return f"{_PIECAD_NAMES[name]}({', '.join(children)})"

        if name == "minkowski":
            if node.arguments:
                raise TranslationError(f"minkowski() does not accept arguments{_position(node)}")
            if len(children) < 2:
                raise TranslationError(
                    f"minkowski() requires at least two child geometries{_position(node)}"
                )
            result = children[0]
            for child in children[1:]:
                result = f"{_receiver(result)}.minkowski_sum({child})"
            return result

        if name == "offset":
            self.reject_unexpected_named_arguments(
                node, {"r", "delta", "chamfer", "$fn", "$fa", "$fs"}
            )
            if not children:
                return "None"
            radius = self.argument_value(node.arguments, ("r",), 0)
            delta = self.argument_value(node.arguments, ("delta",), -1)
            chamfer = self.argument_value(node.arguments, ("chamfer",), -1)
            segments = self.argument_value(node.arguments, ("$fn",), -1)
            geometry = self.combine(children)
            if delta is not None:
                join = f'"square" if {chamfer} else "miter"' if chamfer else '"miter"'
                call = f"{_receiver(geometry)}.offset({delta}, {join}, 1000.0"
            else:
                call = f'{_receiver(geometry)}.offset({radius or "1"}, "round"'
            if segments is not None and delta is None:
                call += f", segments=int({segments})"
            return call + ")"

        if name == "projection":
            if len(children) != 1:
                raise TranslationError(
                    f"projection() requires exactly one child geometry{_position(node)}"
                )
            self.reject_unexpected_named_arguments(node, {"cut"})
            if len(node.arguments) > 1:
                raise TranslationError(
                    f"projection() accepts only the cut argument{_position(node)}"
                )
            cut = self.argument_value(node.arguments, ("cut",), 0)
            cut = cut or "False"
            if cut in ("True", "False"):
                method = "slice(0)" if cut == "True" else "project()"
                return f"{_receiver(children[0])}.{method}"
            return (
                f"({_receiver(children[0])}.slice(0) "
                f"if {cut} else {_receiver(children[0])}.project())"
            )

        if name == "render":
            return self.combine(children) if children else "None"
        if name == "color":
            self.reject_unexpected_named_arguments(node, {"c", "alpha"})
            if len(children) != 1:
                self.note_fixme(node, "color() requires exactly one child")
                return "None"
            color = self.argument_expression(node.arguments, ("c",), 0)
            if color is None:
                self.note_fixme(node, "color() without a color argument")
                return children[0]
            if isinstance(color, ListComprehension):
                values = [self.expression(value) for value in color.elements]
                if len(values) not in {3, 4}:
                    self.note_fixme(node, "only RGB/RGBA color vectors are supported")
                    return children[0]
                rgb = ", ".join(f"round(({value}) * 255)" for value in values[:3])
                return f"{_receiver(children[0])}.color(({rgb}))"
            return f"{_receiver(children[0])}.color({self.expression(color)})"
        if name == "text":
            self.reject_children(node, children)
            text = self.argument_value(node.arguments, ("text",), 0)
            size = self.argument_value(node.arguments, ("size",), 1)
            halign = self.argument_value(node.arguments, ("halign",), -1)
            valign = self.argument_value(node.arguments, ("valign",), -1)
            unsupported_arguments = {
                argument.name.name
                for argument in node.arguments
                if isinstance(argument, NamedArgument)
            } - {"text", "size", "halign", "valign"}
            positional = [a for a in node.arguments if not isinstance(a, NamedArgument)]
            if text is not None and not unsupported_arguments and len(positional) <= 2:
                size = size or "10"
                if halign is None and valign is None:
                    return f"text(size={size}, text={text})"
                self.uses_text_helper = True
                return (
                    f"_scadtopy_text({text}, {size}, "
                    f"{halign or repr('left')}, {valign or repr('baseline')})"
                )
            self.note_fixme(
                node, "text() supports only text, size, halign and valign in Piecad"
            )
            return "None"
        if name == "polyhedron":
            self.reject_children(node, children)
            points = self.argument_value(node.arguments, ("points",), 0)
            faces = self.argument_value(node.arguments, ("faces",), 1)
            if points is not None and faces is not None and len(node.arguments) <= 2:
                triangles = (
                    f"[tuple(int(index) for index in "
                    f"(face[0], face[i + 1], face[i])) "
                    f"for face in {faces} for i in range(1, len(face) - 1)]"
                )
                return f"polyhedron({points}, {triangles}, check='repair')"
            self.note_fixme(node, "polyhedron() requires points and faces")
            return "None"

        if name == "cube":
            self.reject_children(node, children)
            return self.cube(node)
        if name == "sphere":
            self.reject_children(node, children)
            return self.primitive_call(
                "sphere",
                node,
                {"r": "radius", "d": "diameter", "$fn": "segments"},
            )
        if name == "cylinder":
            self.reject_children(node, children)
            return self.cylinder(node)
        if name == "square":
            self.reject_children(node, children)
            return self.square(node)
        if name == "circle":
            self.reject_children(node, children)
            return self.primitive_call(
                "circle",
                node,
                {"r": "radius", "d": "diameter", "$fn": "segments"},
            )
        if name == "polygon":
            self.reject_children(node, children)
            points = self.argument_value(node.arguments, ("points",), 0)
            paths = self.argument_value(node.arguments, ("paths",), 1)
            self.reject_unexpected_named_arguments(node, {"points", "paths"})
            if paths is not None:
                if points is None:
                    raise TranslationError(f"polygon() requires points{_position(node)}")
                return (
                    f"polygon([[{points}[int(index)] for index in reversed(path)] "
                    f"for path in {paths}])"
                )
            if points is not None:
                return f"polygon([[point for point in reversed({points})]])"
            raise TranslationError(f"polygon() requires points{_position(node)}")

        if name == "children":
            self.uses_children_helper = True
            if not node.arguments:
                return "_scadtopy_children(_children)"
            index = self.argument_expression(node.arguments, ("index",), 0)
            if index is None:
                return "_scadtopy_children(_children)"
            return f"_scadtopy_children(_children, {self.expression(index)})"
        if name in self.module_names:
            if children and name in self.children_modules:
                arguments.append(f"_children=[{', '.join(children)}]")
            return f"{self.py_name(name)}({', '.join(arguments)})"
        if children:
            self.note_fixme(node, f"child geometry for module {name!r} is not translated")
            return "None"
        self.note_fixme(node, f"OpenSCAD module {name!r} has no Piecad mapping")
        return "None"
        raise AssertionError("unreachable")

    def transform_call(self, name: str, node: ModularCall, geometry: str) -> str:
        if name == "translate":
            self.reject_unexpected_named_arguments(node, {"v"})
            vector = self.argument_expression(node.arguments, ("v",), 0)
            if vector is None or len(node.arguments) != 1:
                raise TranslationError(f"translate() requires one vector{_position(node)}")
            rendered = self.expression(vector)
            if _is_2d(node.children):
                rendered = _first_two(rendered)
            return f"{_receiver(geometry)}.translate({rendered})"

        if name == "scale":
            self.reject_unexpected_named_arguments(node, {"v"})
            factors = self.argument_expression(node.arguments, ("v",), 0)
            if factors is None or len(node.arguments) != 1:
                raise TranslationError(f"scale() requires one factor or vector{_position(node)}")
            rendered = self.expression(factors)
            if not isinstance(factors, ListComprehension):
                rendered = f"[{rendered}, {rendered}, {rendered}]"
            return f"{_receiver(geometry)}.scale({rendered})"

        if name == "rotate":
            self.reject_unexpected_named_arguments(node, {"a", "v"})
            angle = self.argument_expression(node.arguments, ("a",), 0)
            axis = self.argument_expression(node.arguments, ("v",), 1)
            if angle is None or len(node.arguments) not in {1, 2}:
                raise TranslationError(f"rotate() requires an angle{_position(node)}")
            if axis is None:
                if isinstance(angle, ListComprehension):
                    angles = self.expression(angle)
                else:
                    rendered_angle = self.expression(angle)
                    angles = f"[0, 0, {rendered_angle}]"
            else:
                if isinstance(angle, ListComprehension):
                    raise TranslationError(
                        f"rotate() with an axis requires a scalar angle{_position(node)}"
                    )
                axis_values = self.literal_axis(axis, name)
                if axis_values is None:
                    return "None"
                axis_index = next(index for index, value in enumerate(axis_values) if value != 0)
                rendered_angle = self.expression(angle)
                angles_list = ["0", "0", "0"]
                sign = "-" if axis_values[axis_index] < 0 else ""
                angles_list[axis_index] = f"{sign}({rendered_angle})"
                angles = f"[{', '.join(angles_list)}]"
            return f"{_receiver(geometry)}.rotate({angles})"

        if name == "mirror":
            self.reject_unexpected_named_arguments(node, {"v"})
            axis = self.argument_expression(node.arguments, ("v",), 0)
            if axis is None or len(node.arguments) != 1:
                raise TranslationError(f"mirror() requires one axis vector{_position(node)}")
            axis_values = self.literal_axis(axis, name)
            if axis_values is None:
                return "None"
            flags = [str(value != 0) for value in axis_values]
            return f"{_receiver(geometry)}.mirror(({', '.join(flags)}))"

        raise AssertionError(f"Missing transform mapping for {name}")

    def literal_axis(self, expression: object, operation: str) -> list[float] | None:
        if not isinstance(expression, ListComprehension) or len(expression.elements) != 3:
            self.note_fixme(expression, f"{operation}() requires a literal 3-vector")
            return None
        values: list[float] = []
        for element in expression.elements:
            if isinstance(element, NumberLiteral):
                values.append(element.val)
            elif isinstance(element, UnaryMinusOp) and isinstance(
                element.expr, NumberLiteral
            ):
                values.append(-element.expr.val)
            else:
                self.note_fixme(expression, f"{operation}() vector is not a literal")
                return None
        nonzero = [value for value in values if value != 0]
        if len(nonzero) != 1:
            self.note_fixme(
                expression, f"{operation}() vector must have one nonzero axis component"
            )
            return None
        return values

    def reject_children(self, node: ModularCall, children: list[str]) -> None:
        if children:
            raise TranslationError(
                f"Child geometry is not valid for {node.name.name}(){_position(node)}"
            )

    def reject_unexpected_named_arguments(
        self, node: ModularCall, allowed: set[str]
    ) -> None:
        unexpected = {
            argument.name.name
            for argument in node.arguments
            if isinstance(argument, NamedArgument) and argument.name.name not in allowed
        }
        if unexpected:
            raise TranslationError(
                f"Unsupported {node.name.name} parameter(s): {', '.join(sorted(unexpected))}"
                f"{_position(node)}"
            )

    def cube(self, node: ModularCall) -> str:
        self.reject_unexpected_named_arguments(node, {"size", "center"})
        if len(node.arguments) > 2:
            raise TranslationError(f"Too many cube() arguments{_position(node)}")
        size = self.argument_value(node.arguments, ("size",), 0)
        center = self.argument_value(node.arguments, ("center",), 1)
        if size is None:
            raise TranslationError(f"cube() requires size{_position(node)}")
        centered = f", center={center}" if center is not None else ""
        return f"cube({size}{centered})"

    def cylinder(self, node: ModularCall) -> str:
        height = self.argument_value(node.arguments, ("h", "height"), 0)
        radius = self.argument_value(node.arguments, ("r", "radius"), 1)
        r1 = self.argument_value(node.arguments, ("r1",), -1)
        r2 = self.argument_value(node.arguments, ("r2",), -1)
        diameter = self.argument_value(node.arguments, ("d", "diameter"), -1)
        d1 = self.argument_value(node.arguments, ("d1",), -1)
        d2 = self.argument_value(node.arguments, ("d2",), -1)
        center = self.argument_value(node.arguments, ("center",), 2)
        segments = self.argument_value(node.arguments, ("$fn",), -1)
        if height is None:
            raise TranslationError(f"cylinder() requires height{_position(node)}")
        unsupported_arguments = {
            argument.name.name
            for argument in node.arguments
            if isinstance(argument, NamedArgument)
        } - {"h", "height", "r", "r1", "r2", "radius", "d", "d1", "d2", "diameter", "center", "$fn"}
        if unsupported_arguments:
            raise TranslationError(
                f"Unsupported cylinder parameter(s): {', '.join(sorted(unsupported_arguments))}"
                f"{_position(node)}"
            )
        positional = [
            argument
            for argument in node.arguments
            if isinstance(argument, PositionalArgument)
        ]
        if len(positional) > 3 or (
            len(positional) == 3 and not isinstance(positional[2].expr, BooleanLiteral)
        ):
            raise TranslationError(
                f"Only cylinder(height, radius, center) positional arguments are supported"
                f"{_position(node)}"
            )
        if diameter is not None:
            radius = f"({diameter}) / 2"
        if d1 is not None:
            r1 = f"({d1}) / 2"
        if d2 is not None:
            r2 = f"({d2}) / 2"
        keywords = [f"segments={segments}"] if segments is not None else []
        if center is not None:
            keywords.append(f"center={center}")
        if r1 is not None:
            return f"elliptical_cylinder({height}, ({r1}, {r2}){', ' if keywords else ''}{', '.join(keywords)})"
        return f"cylinder({height}, {radius}{', ' if keywords else ''}{', '.join(keywords)})"

    def square(self, node: ModularCall) -> str:
        self.reject_unexpected_named_arguments(node, {"size", "center"})
        if len(node.arguments) > 2:
            raise TranslationError(f"Too many square() arguments{_position(node)}")
        size = self.argument_value(node.arguments, ("size",), 0)
        center = self.argument_value(node.arguments, ("center",), 1)
        if size is None:
            raise TranslationError(f"square() requires size{_position(node)}")
        centered = f", center={center}" if center is not None else ""
        if self.is_vector_expression(node.arguments, ("size",), 0):
            return f"rectangle({size}{centered})"
        return f"square({size}{centered})"

    def primitive_call(
        self,
        target: str,
        node: ModularCall,
        aliases: dict[str, str],
    ) -> str:
        values: list[str] = []
        allowed = set(aliases) | set(aliases.values())
        self.reject_unexpected_named_arguments(node, allowed)
        if len(node.arguments) > 2:
            raise TranslationError(f"Too many {target}() arguments{_position(node)}")
        for argument in node.arguments:
            if isinstance(argument, PositionalArgument):
                values.append(self.expression(argument.expr))
            elif isinstance(argument, NamedArgument):
                name = aliases.get(argument.name.name, argument.name.name)
                if name.startswith("$"):
                    raise TranslationError(
                        f"Unsupported {target} parameter {name!r}{_position(node)}"
                    )
                if name == "diameter":
                    values.append(f"radius=({self.expression(argument.expr)}) / 2")
                else:
                    values.append(f"{name}={self.expression(argument.expr)}")
        return f"{target}({', '.join(values)})"

    def call_arguments(self, arguments: list[object]) -> list[str]:
        rendered: list[str] = []
        for argument in arguments:
            if isinstance(argument, PositionalArgument):
                rendered.append(self.expression(argument.expr))
            elif isinstance(argument, NamedArgument):
                rendered.append(
                    f"{argument.name.name}={self.expression(argument.expr)}"
                )
            else:
                self.unsupported(argument)
        return rendered

    def argument_value(
        self,
        arguments: list[object],
        names: tuple[str, ...],
        positional_index: int,
    ) -> str | None:
        expression = self.argument_expression(arguments, names, positional_index)
        return self.expression(expression) if expression is not None else None

    def argument_expression(
        self,
        arguments: list[object],
        names: tuple[str, ...],
        positional_index: int,
    ) -> object | None:
        for argument in arguments:
            if isinstance(argument, NamedArgument) and argument.name.name in names:
                return argument.expr
        positional = [
            argument
            for argument in arguments
            if isinstance(argument, PositionalArgument)
        ]
        if 0 <= positional_index < len(positional):
            return positional[positional_index].expr
        return None

    def is_vector_expression(
        self,
        arguments: list[object],
        names: tuple[str, ...],
        positional_index: int,
    ) -> bool:
        for argument in arguments:
            if isinstance(argument, NamedArgument) and argument.name.name in names:
                return isinstance(argument.expr, ListComprehension)
        positional = [
            argument
            for argument in arguments
            if isinstance(argument, PositionalArgument)
        ]
        return (
            positional_index < len(positional)
            and isinstance(positional[positional_index].expr, ListComprehension)
        )

    def combine(self, geometries: list[str]) -> str:
        return geometries[0] if len(geometries) == 1 else f"union({', '.join(geometries)})"

    def note_fixme(self, node: object, message: str) -> None:
        line = _position(node).removeprefix(" at line ")
        location = f" at line {line}" if line else ""
        fixme = f"pass  # FIXME: {message}{location}"
        if fixme not in self.fixmes:
            self.fixmes.append(fixme)

    def let_expression(self, assignments: list[Assignment], body: str) -> str:
        for assignment in reversed(assignments):
            body = (
                f"(lambda {assignment.name.name}: {body})"
                f"({self.expression(assignment.expr)})"
            )
        return body

    def list_comprehension(self, node: ListComprehension) -> str:
        elements = [
            self.vector_segment(element)
            for element in node.elements
            if not isinstance(element, (CommentLine, CommentSpan, BlankLine))
        ]
        if not elements:
            return "[]"
        if all(
            not isinstance(
                element,
                (
                    ListCompCFor,
                    ListCompEach,
                    ListCompFor,
                    ListCompIf,
                    ListCompIfElse,
                    ListCompLet,
                ),
            )
            for element in node.elements
        ):
            return f"[{', '.join(self.expression(element) for element in node.elements)}]"
        return f"sum([{', '.join(elements)}], [])"

    def vector_segment(self, element: object, clauses: list[str] | None = None) -> str:
        clauses = list(clauses or [])
        if isinstance(element, ListCompFor):
            clauses.extend(
                f"for {assignment.name.name} in {self.expression(assignment.expr)}"
                for assignment in element.assignments
            )
            return self.vector_segment(element.body, clauses)
        if isinstance(element, ListCompIf):
            if not any(clause.startswith("for ") for clause in clauses):
                clauses.insert(0, "for _ in [None]")
            clauses.append(f"if {self.expression(element.condition)}")
            return self.vector_segment(element.true_expr, clauses)
        if isinstance(element, ListCompIfElse):
            value = (
                f"({self.vector_element_value(element.true_expr)} if "
                f"{self.expression(element.condition)} else "
                f"{self.vector_element_value(element.false_expr)})"
            )
            return f"[{value} {' '.join(clauses)}]".rstrip()
        if isinstance(element, ListCompLet):
            value = self.vector_segment(element.body, clauses)
            return self.let_expression(element.assignments, value)
        if isinstance(element, ListCompEach):
            value = self.vector_element_value(element.body)
            if clauses:
                self.note_fixme(element, "each inside a list comprehension is approximated")
                return f"[{value} {' '.join(clauses)}]"
            return value
        if isinstance(element, ListCompCFor):
            self.note_fixme(element, "C-style list comprehension is not translated")
            return "[]"
        value = self.vector_element_value(element)
        return f"[{value} {' '.join(clauses)}]".rstrip()

    def vector_element_value(self, element: object) -> str:
        if isinstance(element, (ListCompFor, ListCompIf, ListCompIfElse, ListCompLet)):
            segment = self.vector_segment(element)
            self.note_fixme(element, "nested list-comprehension control is approximated")
            return segment
        if isinstance(element, ListCompEach):
            return self.vector_element_value(element.body)
        return self.expression(element)

    def expression(self, node: object) -> str:
        return _tidy(self._expression(node))

    def _expression(self, node: object) -> str:
        if isinstance(node, CommentedExpr):
            return self.expression(node.expr)
        if isinstance(node, Identifier):
            if node.name == "$children":
                return "len(_children)"
            if node.name == "PI":
                self.uses_math = True
                return "math.pi"
            return node.name
        if isinstance(node, NumberLiteral):
            return repr(node.val)
        if isinstance(node, StringLiteral):
            return repr(node.val)
        if isinstance(node, BooleanLiteral):
            return "True" if node.val else "False"
        if isinstance(node, UndefinedLiteral):
            return "None"
        if isinstance(node, RangeLiteral):
            self.uses_ranges = True
            return (
                f"_scadtopy_range({self.expression(node.start)}, "
                f"{self.expression(node.step)}, {self.expression(node.end)})"
            )
        if isinstance(node, LetOp):
            return self.let_expression(node.assignments, self.expression(node.body))
        if isinstance(node, EchoOp):
            return self.echo_expression(node.arguments, self.expression(node.body))
        if isinstance(node, AssertOp):
            self.uses_assert_helper = True
            condition, message = self.assert_values(node.arguments, node)
            return (
                f"_scadtopy_assert({condition}, {message}, "
                f"lambda: {self.expression(node.body)})"
            )
        if isinstance(node, FunctionLiteral):
            return f"lambda {self.parameters(node.parameters)}: {self.expression(node.body)}"
        if isinstance(node, RenderExpression):
            return self.module_group(node.children)
        if isinstance(node, ListComprehension):
            return self.list_comprehension(node)
        if isinstance(node, PrimaryCall):
            if not isinstance(node.left, Identifier):
                self.note_fixme(node, "calls through a non-identifier expression are not translated")
                return "None"
            name = node.left.name
            arguments = self.call_arguments(node.arguments)
            if name not in self.function_names:
                if name in _MATH_FUNCTIONS:
                    self.uses_math = True
                    return f"{_MATH_FUNCTIONS[name]}({', '.join(arguments)})"
                if name == "norm" and len(arguments) == 1:
                    self.uses_math = True
                    return f"math.hypot(*{arguments[0]})"
                if name == "cross" and len(node.arguments) == 2:
                    return self.cross(node.arguments, arguments)
            return f"{self.py_name(name)}({', '.join(arguments)})"
        if isinstance(node, PrimaryIndex):
            if isinstance(node.index, NumberLiteral) and float(node.index.val).is_integer():
                return f"{self.expression(node.left)}[{int(node.index.val)}]"
            return (
                f"{self.expression(node.left)}"
                f"[int({self.expression(node.index)})]"
            )
        if isinstance(node, PrimaryMember):
            return f"{self.expression(node.left)}.{node.member.name}"

        name = _node_name(node)
        if name in _BINARY_OPERATORS:
            return (
                f"({self.expression(node.left)} {_BINARY_OPERATORS[name]} "
                f"{self.expression(node.right)})"
            )
        if name in {"UnaryMinusOp", "LogicalNotOp", "BitwiseNotOp"}:
            operand = self.expression(node.expr)
            operator = {
                "UnaryMinusOp": "-",
                "LogicalNotOp": "not ",
                "BitwiseNotOp": "~",
            }[name]
            return f"({operator}{operand})"
        if name == "TernaryOp":
            return (
                f"({self.expression(node.true_expr)} if "
                f"{self.expression(node.condition)} else "
                f"{self.expression(node.false_expr)})"
            )
        self.note_fixme(node, f"{name} expression is not translated")
        return "None"

    def unsupported(self, node: object) -> None:
        raise TranslationError(
            f"Unsupported OpenSCAD AST node {_node_name(node)}{_position(node)}"
        )


def translate_file(source: str | Path) -> str:
    """Parse an OpenSCAD source file and return equivalent Piecad Python code."""
    path = Path(source)
    nodes = getASTfromFile(str(path), include_comments=True, process_includes=True)
    if nodes is None:
        raise TranslationError(f"Could not parse OpenSCAD file: {path}")
    translator = _Translator()
    translator.output_stem = path.stem
    translator.base_dir = path.resolve().parent
    return translator.translate(nodes)
