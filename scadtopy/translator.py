"""Translate OpenSCAD AST nodes into Python code using Piecad."""

from __future__ import annotations

import ast
import builtins
import keyword
import math
import re
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from openscad_parser.ast import getASTfromFile
from openscad_parser.ast.nodes import (
    AdditionOp,
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
    GreaterThanOp,
    GreaterThanOrEqualOp,
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
    LessThanOp,
    LessThanOrEqualOp,
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
    SubtractionOp,
    UnaryMinusOp,
    UndefinedLiteral,
    UseStatement,
)


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

_BUILTIN_HELPERS = {
    "round": (
        "def _scadtopy_round(x):\n"
        "    return math.copysign(math.floor(abs(x) + 0.5), x)"
    ),
    "concat": (
        "def _scadtopy_concat(*values):\n"
        "    result = []\n"
        "    for value in values:\n"
        "        if isinstance(value, (list, tuple)):\n"
        "            result.extend(value)\n"
        "        else:\n"
        "            result.append(value)\n"
        "    return result"
    ),
    "str": (
        "def _scadtopy_str(*values):\n"
        "    def render(value):\n"
        "        if value is True:\n"
        "            return 'true'\n"
        "        if value is False:\n"
        "            return 'false'\n"
        "        if value is None:\n"
        "            return 'undef'\n"
        "        if isinstance(value, (list, tuple)):\n"
        "            return '[' + ', '.join(render(item) for item in value) + ']'\n"
        "        if isinstance(value, float) and value.is_integer():\n"
        "            return str(int(value))\n"
        "        return str(value)\n"
        "\n"
        "    return ''.join(value if isinstance(value, str) else render(value) for value in values)"
    ),
    "rands": (
        "def _scadtopy_rands(low, high, count, seed=None):\n"
        "    generator = random.Random(seed)\n"
        "    return [generator.uniform(low, high) for _ in range(int(count))]"
    ),
    "lookup": (
        "def _scadtopy_lookup(key, table):\n"
        "    table = sorted(table)\n"
        "    if key <= table[0][0]:\n"
        "        return table[0][1]\n"
        "    for (x0, y0), (x1, y1) in zip(table, table[1:]):\n"
        "        if key <= x1:\n"
        "            return y0 + (y1 - y0) * (key - x0) / (x1 - x0)\n"
        "    return table[-1][1]"
    ),
    "search": (
        "def _scadtopy_search(match_value, string_or_vector, "
        "num_returns_per_match=1, index_col_num=0):\n"
        "    if isinstance(match_value, bool):\n"
        "        return None\n"
        "    single_string = isinstance(match_value, str)\n"
        "    single_value = not single_string and not isinstance(match_value, (list, tuple))\n"
        "    if single_string:\n"
        "        queries = list(match_value)\n"
        "    elif isinstance(match_value, (list, tuple)):\n"
        "        queries = list(match_value)\n"
        "    else:\n"
        "        queries = [match_value]\n"
        "    values = list(string_or_vector)\n"
        "    index_col_num = int(index_col_num)\n"
        "    limit = int(num_returns_per_match)\n"
        "    results = []\n"
        "    for query in queries:\n"
        "        if isinstance(query, bool):\n"
        "            results.append([])\n"
        "            continue\n"
        "        matches = []\n"
        "        for index, value in enumerate(values):\n"
        "            if isinstance(value, (list, tuple)):\n"
        "                if not -len(value) <= index_col_num < len(value):\n"
        "                    continue\n"
        "                value = value[index_col_num]\n"
        "            if query == value:\n"
        "                matches.append(index)\n"
        "                if limit > 0 and len(matches) >= limit:\n"
        "                    break\n"
        "        results.append(matches)\n"
        "    if limit == 1:\n"
        "        if single_string:\n"
        "            return [matches[0] for matches in results if matches]\n"
        "        if single_value:\n"
        "            return [results[0][0]] if results and results[0] else []\n"
        "        return [matches[0] if matches else [] for matches in results]\n"
        "    return results"
    ),
}

try:
    import piecad as _piecad

    _PIECAD_EXPORTS = frozenset(n for n in dir(_piecad) if not n.startswith("_"))
except ImportError:
    _PIECAD_EXPORTS = frozenset()


def _node_name(node: object) -> str:
    return type(node).__name__


def _uses_children(node: object) -> bool:
    if isinstance(node, ModularCall) and node.name.name in {"child", "children"}:
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


def _unsupported_special_variables(node: object) -> set[str]:
    if isinstance(node, Identifier):
        return {node.name} if node.name in _UNSUPPORTED_SPECIAL_VARIABLES else set()
    if isinstance(node, (list, tuple)):
        return set().union(*(_unsupported_special_variables(item) for item in node))
    if hasattr(node, "__dict__"):
        return set().union(
            *(
                _unsupported_special_variables(value)
                for key, value in vars(node).items()
                if key != "position"
            )
        )
    return set()


def _called_names(node: object) -> set[str]:
    names: set[str] = set()
    if isinstance(node, PrimaryCall) and isinstance(node.left, Identifier):
        names.add(node.left.name)
    elif isinstance(node, ModularCall):
        names.add(node.name.name)
    if isinstance(node, (list, tuple)):
        for item in node:
            names.update(_called_names(item))
    elif hasattr(node, "__dict__"):
        for key, value in vars(node).items():
            if key != "position":
                names.update(_called_names(value))
    return names


def _identifier_names(node: object) -> set[str]:
    if isinstance(node, Identifier):
        return {node.name}
    if isinstance(node, (list, tuple)):
        return set().union(*(_identifier_names(item) for item in node))
    if hasattr(node, "__dict__"):
        return set().union(
            *(
                _identifier_names(value)
                for key, value in vars(node).items()
                if key != "position"
            )
        )
    return set()


def _contains_function_literal(node: object) -> bool:
    if isinstance(node, FunctionLiteral):
        return True
    if isinstance(node, (list, tuple)):
        return any(_contains_function_literal(item) for item in node)
    if hasattr(node, "__dict__"):
        return any(
            _contains_function_literal(value)
            for key, value in vars(node).items()
            if key != "position"
        )
    return False


def _unwrap_commented_names(node: object) -> None:
    """Replace comment-wrapped identifiers in `name` slots with the identifier."""
    if isinstance(node, (list, tuple)):
        for item in node:
            _unwrap_commented_names(item)
    elif hasattr(node, "__dict__"):
        for key, value in vars(node).items():
            if key == "position":
                continue
            if key == "name" and isinstance(value, CommentedExpr):
                while isinstance(value, CommentedExpr):
                    value = value.expr
                setattr(node, key, value)
            else:
                _unwrap_commented_names(value)


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


def _modules_with_nested_defaults(nodes: Iterable[object]) -> set[str]:
    names: set[str] = set()
    for node in nodes:
        if isinstance(node, ModuleDeclaration):
            if any(
                isinstance(child, (FunctionDeclaration, ModuleDeclaration))
                and any(parameter.default is not None for parameter in child.parameters)
                for child in node.children
            ):
                names.add(node.name.name)
            names |= _modules_with_nested_defaults(node.children)
    return names


_SEGMENT_VARIABLES = ("$fn", "$fa", "$fs")


def _literal_number(node: object) -> float | None:
    if isinstance(node, NumberLiteral):
        return node.val if math.isfinite(node.val) else None
    if isinstance(node, UnaryMinusOp) and isinstance(node.expr, NumberLiteral):
        return -node.expr.val if math.isfinite(node.expr.val) else None
    return None


def _direct_static_extrusion(node: ModularCall) -> bool:
    named = {
        arg.name.name: arg.expr
        for arg in node.arguments
        if isinstance(arg, NamedArgument)
    }
    positional = [
        arg.expr for arg in node.arguments if isinstance(arg, PositionalArgument)
    ]
    twist = _literal_number(named["twist"]) if "twist" in named else 0
    slices = _literal_number(named["slices"]) if "slices" in named else 0
    if twist is None or slices is None or not all(map(math.isfinite, (twist, slices))):
        return False
    center = named.get("center", positional[1] if len(positional) > 1 else None)
    height = named.get("height", positional[0] if positional else None)
    if center is not None and not isinstance(center, BooleanLiteral):
        return False
    if (
        isinstance(center, BooleanLiteral)
        and center.val
        and _literal_number(height) is None
    ):
        return False
    for name in _SEGMENT_VARIABLES:
        if name in named:
            value = _literal_number(named[name])
            if value is None or not math.isfinite(value):
                return False
    if twist and not slices:
        fn = _literal_number(named.get("$fn"))
        if fn is None or int(fn) <= 0:
            return False
    if "scale" in named:
        scale = named["scale"]
        if isinstance(scale, ListComprehension):
            return all(
                _literal_number(element) is not None for element in scale.elements
            )
        return _literal_number(scale) is not None
    return True


def _segment_module_requirements(
    nodes: list[object], source_path: Path | None, base_dir: Path | None
) -> dict[str, set[str]]:
    # Analyze used files together so separately translated module signatures agree.
    root = source_path.resolve() if source_path is not None else None
    files: dict[Path | None, tuple[list[object], Path | None, list[Path]]] = {}

    def load(key: Path | None, contents: list[object], directory: Path | None) -> None:
        if key in files:
            return
        imports: list[Path] = []
        files[key] = (contents, directory, imports)
        if directory is None:
            return
        for node in contents:
            if isinstance(node, UseStatement):
                path = (directory / node.filepath.val).resolve()
                if path.is_file():
                    imports.append(path)
                    used = getASTfromFile(str(path)) or []
                    _unwrap_commented_names(used)
                    load(path, used, path.parent)

    load(root, nodes, base_dir)
    declarations: dict[tuple[Path | None, str], list[ModuleDeclaration]] = {}

    def collect(key: Path | None, contents: Iterable[object]) -> None:
        for node in contents:
            if isinstance(node, ModuleDeclaration):
                declarations.setdefault((key, node.name.name), []).append(node)
                collect(key, node.children)

    for key, (contents, _, _) in files.items():
        collect(key, contents)

    visible: dict[Path | None, dict[str, tuple[Path | None, str]]] = {}
    for key, (_, _, imports) in files.items():
        names: dict[str, tuple[Path | None, str]] = {}
        for imported in imports:
            names.update(
                (name, (owner, name))
                for owner, name in declarations
                if owner == imported
            )
        names.update(
            (name, (owner, name)) for owner, name in declarations if owner == key
        )
        visible[key] = names

    requirements = {key: set() for key in declarations}
    calls: dict[
        tuple[Path | None, str], list[tuple[tuple[Path | None, str], set[str]]]
    ] = {key: [] for key in declarations}
    segment_primitives = {"circle", "sphere", "cylinder", "rotate_extrude"}

    for key, modules in declarations.items():

        def visit(node: object, bound: frozenset[str] = frozenset()) -> None:
            if isinstance(node, Identifier):
                if node.name in _SEGMENT_VARIABLES and node.name not in bound:
                    requirements[key].add(node.name)
            elif isinstance(node, (Assignment, NamedArgument)):
                visit(node.expr, bound)
            elif isinstance(node, ModuleDeclaration):
                visit(node.parameters, bound)
            elif isinstance(node, ModularCall):
                name = node.name.name
                provided = {
                    arg.name.name
                    for arg in node.arguments
                    if isinstance(arg, NamedArgument)
                    and arg.name.name in _SEGMENT_VARIABLES
                }
                needs_segments = name in segment_primitives
                if name == "offset":
                    needs_segments = not any(
                        isinstance(arg, NamedArgument) and arg.name.name == "delta"
                        for arg in node.arguments
                    )
                if name == "linear_extrude":
                    needs_segments = any(
                        isinstance(arg, NamedArgument)
                        and arg.name.name in {"twist", "slices", "scale"}
                        for arg in node.arguments
                    ) and not _direct_static_extrusion(node)
                if needs_segments:
                    requirements[key].update(set(_SEGMENT_VARIABLES) - provided - bound)
                elif name in visible[key[0]]:
                    calls[key].append((visible[key[0]][name], provided | bound))
                visit(node.arguments, bound)
                child_bound = bound
                if name in {"linear_extrude", "offset", "rotate_extrude"}:
                    child_bound = bound | provided
                visit(node.children, child_bound)
            elif isinstance(node, (list, tuple)):
                for item in node:
                    visit(item, bound)
            elif hasattr(node, "__dict__"):
                for attribute, value in vars(node).items():
                    if attribute != "position":
                        visit(value, bound)

        for module in modules:
            # Module assignments are emitted before the geometry expressions.
            assigned: frozenset[str] = frozenset()
            for child in module.children:
                if isinstance(child, Assignment):
                    visit(child.expr, assigned)
                    if child.name.name in _SEGMENT_VARIABLES:
                        assigned |= {child.name.name}
            for child in module.children:
                if isinstance(child, Assignment):
                    continue
                if isinstance(child, FunctionDeclaration):
                    visit(child.parameters)
                    visit(child.expr, assigned)
                elif isinstance(child, ModuleDeclaration):
                    visit(child.parameters)
                else:
                    visit(child, assigned)

    changed = True
    while changed:
        changed = False
        for key, dependencies in calls.items():
            before = len(requirements[key])
            for target, provided in dependencies:
                requirements[key].update(requirements[target] - provided)
            changed |= len(requirements[key]) != before
    return {name: requirements[target] for name, target in visible[root].items()}


_CENTER_MODULES = frozenset({"cube", "square", "cylinder", "linear_extrude"})

_EXPORT_COMMENT = re.compile(r"\s*export\s+(\S+\.\w+)\s*$", re.I)

_PIECAD_LOAD_EXTENSIONS = {"3mf", "dxf", "glb", "gltf", "obj", "ply", "stl", "svg"}
_UNSUPPORTED_SPECIAL_VARIABLES = {"$t", "$preview", "$vpr", "$vpt", "$vpd"}


def _tidy(code: str) -> str:
    """Drop redundant parentheses by round-tripping through the Python parser."""
    try:
        return ast.unparse(ast.parse(code, mode="eval"))
    except SyntaxError:
        return code


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
        self.source_path: Path | None = None
        self.base_dir: Path | None = None
        self.function_names: set[str] = set()
        self.uses_vec2 = False
        self.helpers: set[str] = set()
        self.module_names: set[str] = set()
        self.local_module_names: set[str] = set()
        self.modules_with_nested_defaults: set[str] = set()
        self.imported_identifiers: set[str] = set()
        self.unknown_imports = False
        self.user_identifiers: set[str] = set()
        self.segment_modules: dict[str, set[str]] = {}
        self.geometry_modules: dict[str, ModuleDeclaration] = {}
        self.shadowing_names: set[str] = set()
        self.children_modules: set[str] = set()
        self.uses_children_helper = False
        self.uses_linear_extrude_helper = False
        self.uses_text_helper = False
        self.uses_translate_helper = False
        self.uses_resize_helper = False
        self.uses_segments_helper = False
        self.segment_unset = "_scadtopy_unset"
        self.uses_math = False
        self.segment_overrides: tuple[str, str, str] | None = None
        self.in_module = False
        self.expression_definitions: list[str] = []
        self.assert_counter = 0
        self.c_for_counter = 0
        self.each_counter = 0
        self.module_scope_counter = 0
        self.expression_bindings: list[str] = []
        self.echo_print = "print"
        self.uses_echo_print_alias = False
        self.diagnostics: list[str] = []

    def py_name(self, name: str) -> str:
        return f"{name}_" if name in self.shadowing_names else name

    def translate(self, nodes: Iterable[object]) -> str:
        nodes = list(nodes)
        _unwrap_commented_names(nodes)
        self.shadowing_names = {
            node.name.name
            for node in nodes
            if isinstance(node, (FunctionDeclaration, ModuleDeclaration))
            and node.name.name in _PIECAD_EXPORTS
        }
        self.uses_math = False
        self.segment_overrides = None
        self.in_module = False
        self.expression_definitions = []
        self.assert_counter = 0
        self.c_for_counter = 0
        self.each_counter = 0
        self.module_scope_counter = 0
        self.expression_bindings = []
        identifiers = _identifier_names(nodes)
        self.user_identifiers = identifiers
        self.imported_identifiers = set()
        self.unknown_imports = False
        self.segment_unset = "_scadtopy_unset"
        while self.segment_unset in identifiers:
            self.segment_unset += "_"
        self.echo_print = "print"
        if "print" in identifiers:
            self.echo_print = "_scadtopy_print"
            while self.echo_print in identifiers:
                self.echo_print += "_"
        self.uses_echo_print_alias = False
        self.diagnostics = []
        self.uses_children_helper = False
        self.uses_linear_extrude_helper = False
        self.uses_text_helper = False
        self.uses_translate_helper = False
        self.uses_resize_helper = False
        self.uses_segments_helper = False
        self.uses_vec2 = False
        self.helpers = set()
        self.function_names = {
            node.name.name for node in nodes if isinstance(node, FunctionDeclaration)
        }
        self.local_module_names = _all_module_names(nodes)
        self.modules_with_nested_defaults = _modules_with_nested_defaults(nodes)
        self.module_names = set(self.local_module_names)
        self.children_modules = _children_modules(nodes)
        declarations = [node for node in nodes if isinstance(node, ModuleDeclaration)]
        nested_names = set().union(
            *(_all_module_names(node.children) for node in declarations)
        )
        self.geometry_modules = {
            node.name.name: node
            for node in declarations
            if node.name.name not in nested_names
            and sum(other.name.name == node.name.name for other in declarations) == 1
        }
        if any(isinstance(node, UseStatement) for node in nodes):
            self.geometry_modules.clear()
        for node in nodes:
            if isinstance(node, UseStatement):
                self.load_used_names(node)
        self.segment_modules = _segment_module_requirements(
            nodes, self.source_path, self.base_dir
        )
        declaration_indices: dict[str, int] = {}
        for index, node in enumerate(nodes):
            if isinstance(node, (FunctionDeclaration, ModuleDeclaration)):
                declaration_indices.setdefault(node.name.name, index)
        hoist_start = next(
            (
                index
                for index, node in enumerate(nodes)
                if any(
                    declaration_indices.get(name, index) > index
                    for name in _called_names(node)
                )
            ),
            None,
        )
        hoisted_declarations: list[str] = []
        if hoist_start is not None:
            for node in nodes[hoist_start:]:
                if isinstance(node, ModuleDeclaration):
                    hoisted_declarations.extend(self.module_declaration(node))
                elif isinstance(node, FunctionDeclaration):
                    hoisted_declarations.extend(self.function_declaration(node))
        statements: list[str] = [
            "_parts = []",
            "_scadtopy_default_fn = 0",
            "_scadtopy_default_fa = Config.get_default_min_angle()",
            "_scadtopy_default_fs = Config.get_default_min_segment_length()",
        ]
        saw_model = False
        part_count = 0
        exports: list[tuple[int, str]] = []
        last_statement_is_model = False

        for index, node in enumerate(nodes):
            if index == hoist_start:
                statements.extend(hoisted_declarations)
            if isinstance(node, Assignment):
                unsupported = _unsupported_special_variables(node.expr)
                if unsupported:
                    statements.append(
                        f"# UNSUPPORTED: {', '.join(sorted(unsupported))} "
                        "are not translated"
                    )
                    last_statement_is_model = False
                    continue
                prefix_statements, expression = self.statements_before_expression(
                    node.expr
                )
                statements.extend(prefix_statements)
                value = self.expression(expression)
                if node.name.name == "$fn":
                    statements.append(f"_scadtopy_default_fn = int({value})")
                elif node.name.name == "$fa":
                    statements.append(f"_scadtopy_default_fa = {value}")
                elif node.name.name == "$fs":
                    statements.append(f"_scadtopy_default_fs = {value}")
                elif node.name.name.startswith("$"):
                    statements.append(f"# UNSUPPORTED: {node.name.name} = {value}")
                else:
                    statements.append(f"{node.name.name} = {value}")
                last_statement_is_model = False
            elif isinstance(node, ModuleDeclaration):
                if hoist_start is None or index < hoist_start:
                    statements.extend(self.module_declaration(node))
                last_statement_is_model = False
            elif isinstance(node, FunctionDeclaration):
                if hoist_start is None or index < hoist_start:
                    statements.extend(self.function_declaration(node))
                last_statement_is_model = False
            elif isinstance(node, ModuleInstantiation):
                unsupported = _unsupported_special_variables(node)
                if unsupported:
                    statements.append(
                        f"# UNSUPPORTED: {', '.join(sorted(unsupported))} "
                        "are not translated"
                    )
                    last_statement_is_model = False
                    continue
                if isinstance(node, ModularModifierDisable):
                    self.module_call(node)
                    last_statement_is_model = False
                elif isinstance(node, (ModularEcho, ModularAssert)):
                    prefix_statements, model = self.module_with_statements(node)
                    statements.extend(prefix_statements)
                    if model != "None":
                        saw_model = True
                        statements.append(f"_parts.append({model})")
                        part_count += 1
                        last_statement_is_model = True
                    else:
                        last_statement_is_model = False
                else:
                    saw_model = True
                    model = self.module_call(node)
                    if model != "None":
                        statements.append(f"_parts.append({model})")
                        part_count += 1
                        last_statement_is_model = True
                    else:
                        last_statement_is_model = False
            elif isinstance(node, (CommentLine, CommentSpan)):
                if (
                    isinstance(node, CommentLine)
                    and node.same_line
                    and last_statement_is_model
                ):
                    exported = _EXPORT_COMMENT.match(node.text)
                    if exported:
                        exports.append((part_count - 1, exported.group(1)))
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
                statements.append(
                    f"pass  # FIXME: {_node_name(node)} is not translated"
                )
                last_statement_is_model = False

        if saw_model:
            statements.extend(
                [
                    "",
                    'if __name__ == "__main__":',
                    *(
                        f"    save({name!r}, _parts[{index}])"
                        for index, name in exports
                    ),
                    *self.save_lines(),
                    "    for _part in _parts:",
                    "        view(_part)",
                    "    view_all_now()",
                ]
            )

        statements.extend(self.diagnostics)
        body = "\n".join(statements).rstrip()
        prefix = ""
        if self.uses_echo_print_alias:
            prefix = f"from builtins import print as {self.echo_print}\n\n"
        if self.uses_math or "round" in self.helpers:
            prefix += "import math\n\n"
        if "rands" in self.helpers:
            prefix += "import random\n\n"
        for helper in sorted(self.helpers):
            prefix += _BUILTIN_HELPERS[helper] + "\n\n"
        prefix += "from piecad import *\n"
        if self.uses_vec2:
            prefix += "from piecad.lin_math import Vec2\n"
        prefix += "\n"
        if self.expression_definitions:
            prefix += "\n\n".join(self.expression_definitions) + "\n\n"
        segment_defaults = "".join(
            f"    if {name} is {self.segment_unset}:\n"
            f"        {name} = _scadtopy_default_{name}\n"
            for name in ("fn", "fa", "fs")
        )
        if self.uses_segments_helper or self.uses_linear_extrude_helper:
            prefix += (
                f"{self.segment_unset} = object()\n\n"
                f"def _scadtopy_calculate_segments(radius, fn={self.segment_unset}, "
                f"fa={self.segment_unset}, fs={self.segment_unset}):\n"
                f"{segment_defaults}"
                "    if fn > 0:\n"
                "        return max(fn, 3)\n"
                "    return Config.calculate_default_segments(radius, fa, fs)\n\n"
            )
        if self.uses_translate_helper:
            prefix += (
                "def _scadtopy_translate(obj, vector):\n"
                "    if isinstance(obj, Obj2d):\n"
                "        vector = vector[:2]\n"
                "    return obj.translate(vector)\n\n"
            )
        if self.uses_resize_helper:
            prefix += (
                "def _scadtopy_resize(obj, newsize, auto=False):\n"
                "    if isinstance(obj, Obj2d):\n"
                "        dimensions = 2\n"
                "    elif isinstance(obj, Obj3d):\n"
                "        dimensions = 3\n"
                "    else:\n"
                "        raise TypeError('resize() requires an Obj2d or Obj3d')\n"
                "    if not isinstance(newsize, (list, tuple)):\n"
                "        raise TypeError('resize() newsize must be a vector')\n"
                "    targets = list(newsize[:dimensions])\n"
                "    targets.extend([0] * (dimensions - len(targets)))\n"
                "    if isinstance(auto, bool):\n"
                "        auto_axes = [auto] * dimensions\n"
                "    elif isinstance(auto, (list, tuple)):\n"
                "        auto_axes = list(auto[:dimensions])\n"
                "        auto_axes.extend([False] * (dimensions - len(auto_axes)))\n"
                "    else:\n"
                "        raise TypeError('resize() auto must be a boolean or vector')\n"
                "    bounds = obj.bounding_box()\n"
                "    sizes = [bounds[i + dimensions] - bounds[i] for i in range(dimensions)]\n"
                "    explicit = [i for i, target in enumerate(targets) if target > 0]\n"
                "    for i in explicit:\n"
                "        if sizes[i] == 0:\n"
                "            raise ValueError('Cannot resize an axis with zero extent')\n"
                "    largest = max(explicit, key=lambda i: targets[i]) if explicit else None\n"
                "    auto_scale = targets[largest] / sizes[largest] if largest is not None else 1\n"
                "    piecad_sizes = []\n"
                "    for i, target in enumerate(targets):\n"
                "        if target > 0:\n"
                "            piecad_sizes.append(target)\n"
                "        elif auto_axes[i] and sizes[i] != 0:\n"
                "            piecad_sizes.append(sizes[i] * auto_scale)\n"
                "        else:\n"
                "            piecad_sizes.append(None)\n"
                "    return obj.resize(piecad_sizes)\n\n"
            )
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
                "def _scadtopy_linear_extrude(obj, height, center, twist, slices, scale, "
                f"fn={self.segment_unset}, fa={self.segment_unset}, fs={self.segment_unset}):\n"
                f"{segment_defaults}"
                "    if not isinstance(scale, (list, tuple)):\n"
                "        scale = (scale, scale)\n"
                "    bounds = obj.bounding_box()\n"
                "    if len(bounds) == 4:\n"
                "        min_x, min_y, max_x, max_y = bounds\n"
                "    else:\n"
                "        min_x, min_y, _, max_x, max_y, _ = bounds\n"
                "    radius = math.hypot(max(abs(min_x), abs(max_x)), "
                "max(abs(min_y), abs(max_y)))\n"
                "    segments = _scadtopy_calculate_segments(radius, fn, fa, fs)\n"
                "    divisions = int(slices) if slices else (\n"
                "        max(1, math.ceil(abs(twist) / 360 * segments))\n"
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

    def builtin_call(
        self, name: str, nodes: list[object], arguments: list[str]
    ) -> str | None:
        count = len(arguments)
        if name == "search":
            parameters = (
                "match_value",
                "string_or_vector",
                "num_returns_per_match",
                "index_col_num",
            )
            aliases = {parameter: parameter for parameter in parameters}
            aliases.update({f"_{parameter}_": parameter for parameter in parameters})
            bound: dict[str, str] = {}
            positional_index = 0
            for node in nodes:
                if isinstance(node, PositionalArgument):
                    if positional_index >= len(parameters):
                        raise TranslationError(
                            "search() accepts at most four arguments"
                        )
                    parameter = parameters[positional_index]
                    positional_index += 1
                    value = self.expression(node.expr)
                elif isinstance(node, NamedArgument):
                    parameter = aliases.get(node.name.name)
                    if parameter is None:
                        raise TranslationError(
                            f"Unsupported search() parameter {node.name.name!r}"
                        )
                    value = self.expression(node.expr)
                else:
                    self.unsupported(node)
                if parameter in bound:
                    raise TranslationError(
                        f"search() got multiple values for {parameter!r}"
                    )
                bound[parameter] = value
            if "match_value" not in bound or "string_or_vector" not in bound:
                raise TranslationError(
                    "search() requires match_value and string_or_vector"
                )
            self.helpers.add("search")
            return (
                "_scadtopy_search("
                + ", ".join(
                    f"{parameter}={value}" for parameter, value in bound.items()
                )
                + ")"
            )
        if name == "version" and count == 0:
            return 'tuple(map(int, version().split(".")))'
        if name == "is_undef" and count == 1:
            return f"{arguments[0]} is None"
        if name == "is_list" and count == 1:
            return f"isinstance({arguments[0]}, (list, tuple))"
        if name == "is_string" and count == 1:
            return f"isinstance({arguments[0]}, str)"
        if name == "is_bool" and count == 1:
            return f"isinstance({arguments[0]}, bool)"
        if name == "is_num" and count == 1:
            return f"isinstance({arguments[0]}, (int, float))"
        if name == "is_function" and count == 1:
            return f"callable({arguments[0]})"
        if name == "concat" and count:
            if all(
                isinstance(getattr(node, "expr", None), ListComprehension)
                for node in nodes
            ):
                return " + ".join(arguments)
            self.helpers.add("concat")
        elif name == "rands" and count in {3, 4}:
            self.helpers.add("rands")
        elif name in {"round", "str", "lookup"} and (
            count == 2 if name == "lookup" else count >= 1
        ):
            self.helpers.add(name)
        else:
            return None
        return f"_scadtopy_{name}({', '.join(arguments)})"

    def cross(self, nodes: list[object], arguments: list[str]) -> str:
        values = [getattr(argument, "expr", None) for argument in nodes]
        if all(
            isinstance(value, ListComprehension) and len(value.elements) == 2
            for value in values
        ):
            self.uses_vec2 = True
            return f"Vec2.cross({', '.join(arguments)})[2]"
        return f"Vec3.cross({', '.join(arguments)})"

    def warn(self, node: object, message: str) -> None:
        if self.source_path is None:
            warnings.warn(message, UserWarning, stacklevel=2)
            return
        line = getattr(getattr(node, "position", None), "line", None) or 1
        warnings.warn_explicit(
            message,
            UserWarning,
            str(self.source_path),
            line,
        )

    def used_nodes(self, node: UseStatement) -> list[object]:
        if self.base_dir is None:
            return []
        path = self.base_dir / node.filepath.val
        if not path.is_file():
            return []
        return getASTfromFile(str(path)) or []

    def load_used_names(self, node: UseStatement) -> None:
        nodes = self.used_nodes(node)
        self.imported_identifiers |= _identifier_names(nodes) | _PIECAD_EXPORTS
        self.imported_identifiers.update({"math", "random", "Vec2"})
        if not nodes or any(isinstance(item, UseStatement) for item in nodes):
            self.unknown_imports = True
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
        self.warn(
            node,
            f"use <{node.filepath.val}> emits a Python import only; "
            "translate the used .scad file separately to a matching .py file",
        )
        module = Path(node.filepath.val).stem
        if not module.isidentifier():
            return (
                f"pass  # FIXME: cannot import {node.filepath.val!r} as a Python module"
            )
        return f"from {module} import *"

    def save_lines(self) -> list[str]:
        if not self.output_stem:
            return []
        return [
            "    _scadtopy_parts_3d = [part for part in _parts "
            "if isinstance(part, Obj3d)]",
            "    if _scadtopy_parts_3d:",
            f'        save("{self.output_stem}.3mf", union(*_scadtopy_parts_3d))',
            "    else:",
            "        _scadtopy_parts_2d = [part for part in _parts "
            "if isinstance(part, Obj2d)]",
            "        if _scadtopy_parts_2d:",
            f'            save("{self.output_stem}.svg", union(*_scadtopy_parts_2d))',
        ]

    def module_declaration(self, node: ModuleDeclaration) -> list[str]:
        unsupported = _unsupported_special_variables(node.parameters)
        if unsupported:
            return [
                f"def {self.py_name(node.name.name)}(*args, **kwargs):",
                f"    # UNSUPPORTED: {', '.join(sorted(unsupported))} "
                "are not translated",
                "    return None",
            ]
        params = [self.parameters(node.parameters)] if node.parameters else []
        needed = self.segment_modules.get(node.name.name, set(_SEGMENT_VARIABLES))
        params.extend(
            f"_scadtopy_special_{name[1:]}=None"
            for name in _SEGMENT_VARIABLES
            if name in needed
        )
        if _uses_children(node.children):
            params.append("_children=()")
        lines = [f"def {self.py_name(node.name.name)}({', '.join(params)}):"]
        enclosing_expression_definitions = self.expression_definitions
        self.expression_definitions = []
        assignments: list[str] = []
        for name in _SEGMENT_VARIABLES:
            if name in needed:
                suffix = name[1:]
                assignments.extend(
                    [
                        f"    if _scadtopy_special_{suffix} is None:",
                        f"        _scadtopy_special_{suffix} = _scadtopy_default_{suffix}",
                    ]
                )
        previous_in_module = self.in_module
        self.in_module = True
        geometry: list[tuple[bool, str]] = []
        for child in node.children:
            if isinstance(child, Assignment):
                unsupported = _unsupported_special_variables(child.expr)
                if unsupported:
                    assignments.append(
                        f"    # UNSUPPORTED: {', '.join(sorted(unsupported))} "
                        "are not translated"
                    )
                    continue
                prefix_statements, expression = self.statements_before_expression(
                    child.expr
                )
                assignments.extend(
                    f"    {statement}" for statement in prefix_statements
                )
                value = self.expression(expression)
                if child.name.name == "$fn":
                    assignments.append(f"    _scadtopy_special_fn = int({value})")
                elif child.name.name == "$fa":
                    assignments.append(f"    _scadtopy_special_fa = {value}")
                elif child.name.name == "$fs":
                    assignments.append(f"    _scadtopy_special_fs = {value}")
                elif child.name.name.startswith("$"):
                    assignments.append(
                        f"    # UNSUPPORTED: {child.name.name} = {value}"
                    )
                else:
                    assignments.append(f"    {child.name.name} = {value}")
            elif isinstance(child, (ModularEcho, ModularAssert)):
                unsupported = _unsupported_special_variables(child)
                if unsupported:
                    assignments.append(
                        f"    # UNSUPPORTED: {', '.join(sorted(unsupported))} "
                        "are not translated"
                    )
                    continue
                prefix_statements, translated = self.module_with_statements(child)
                geometry.extend((True, statement) for statement in prefix_statements)
                if translated != "None":
                    geometry.append((False, translated))
            elif isinstance(child, ModuleInstantiation):
                unsupported = _unsupported_special_variables(child)
                if unsupported:
                    assignments.append(
                        f"    # UNSUPPORTED: {', '.join(sorted(unsupported))} "
                        "are not translated"
                    )
                    continue
                translated = self.module_call(child)
                if translated != "None":
                    geometry.append((False, translated))
            elif isinstance(child, FunctionDeclaration):
                lines.extend(f"    {line}" for line in self.function_declaration(child))
            elif isinstance(child, ModuleDeclaration):
                lines.extend(f"    {line}" for line in self.module_declaration(child))
            elif isinstance(child, (CommentLine, CommentSpan)):
                self.append_comment(assignments, child, indent="    ")
            elif isinstance(child, BlankLine):
                assignments.append("")
            else:
                assignments.append(
                    f"    pass  # FIXME: {_node_name(child)} is not translated"
                )

        self.in_module = previous_in_module
        lines[1:1] = [
            f"    {line}"
            for definition in self.expression_definitions
            for line in definition.splitlines()
        ]
        self.expression_definitions = enclosing_expression_definitions
        if assignments:
            lines.extend(assignments)
        if geometry:
            if any(is_assert for is_assert, _ in geometry):
                parts = []
                for is_assert, translated in geometry:
                    if is_assert:
                        lines.append(f"    {translated}")
                    else:
                        name = f"_scadtopy_geometry_{len(parts)}"
                        lines.append(f"    {name} = {translated}")
                        parts.append(name)
                lines.append(f"    return {self.combine(parts) if parts else 'None'}")
            else:
                lines.append(
                    f"    return {self.combine([value for _, value in geometry])}"
                )
        else:
            lines.append("    return None")
        return lines

    def function_declaration(self, node: FunctionDeclaration) -> list[str]:
        unsupported = _unsupported_special_variables([node.parameters, node.expr])
        if unsupported:
            return [
                f"def {self.py_name(node.name.name)}(*args, **kwargs):",
                f"    # UNSUPPORTED: {', '.join(sorted(unsupported))} "
                "are not translated",
                "    return None",
            ]
        params = self.parameters(node.parameters)
        enclosing_expression_definitions = self.expression_definitions
        self.expression_definitions = []
        expression = node.expr
        prefix_statements: list[str] = []
        if isinstance(expression, LetOp):
            names = {assignment.name.name for assignment in expression.assignments}
            assigned: set[str] = set()
            safe = len(names) == len(expression.assignments)
            for assignment in expression.assignments:
                if (names - assigned) & _identifier_names(assignment.expr):
                    safe = False
                assigned.add(assignment.name.name)
            if safe:
                prefix_statements.extend(
                    f"{assignment.name.name} = {self.expression(assignment.expr)}"
                    for assignment in expression.assignments
                )
                expression = expression.body
        statements, expression = self.statements_before_expression(expression)
        prefix_statements.extend(statements)
        value = self.expression(expression)
        definitions = self.expression_definitions
        self.expression_definitions = enclosing_expression_definitions
        return [
            f"def {self.py_name(node.name.name)}({params}):",
            *(
                f"    {line}"
                for definition in definitions
                for line in definition.splitlines()
            ),
            *(f"    {statement}" for statement in prefix_statements),
            f"    return {value}",
        ]

    def parameters(self, parameters: list[object]) -> str:
        rendered: list[str] = []
        seen_default = False
        for parameter in parameters:
            text = parameter.name.name
            if parameter.default is not None:
                seen_default = True
                text += f"={self.expression(parameter.default)}"
            elif seen_default:
                # Python forbids a required parameter after a defaulted one;
                # an omitted OpenSCAD parameter is undef.
                text += "=None"
            rendered.append(text)
        return ", ".join(rendered)

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
            f"{indent}# {text}" if text else f"{indent}#" for text in text_lines
        )

    def assert_values(self, arguments: list[object], node: object) -> tuple[str, str]:
        condition = self.argument_expression(arguments, ("condition",), 0)
        message = self.argument_expression(arguments, ("message",), 1)
        condition_value = (
            self.expression(condition) if condition is not None else "False"
        )
        message_value = (
            self.expression(message)
            if message is not None
            else repr(f"OpenSCAD assert failed{_position(node)}")
        )
        return condition_value, message_value

    def assert_statement(self, node: AssertOp | ModularAssert) -> str:
        condition, message = self.assert_values(node.arguments, node)
        return f"assert {condition}, {message}"

    def statements_before_expression(self, node: object) -> tuple[list[str], object]:
        statements = []
        while isinstance(node, (AssertOp, EchoOp)):
            if isinstance(node, AssertOp):
                statements.append(self.assert_statement(node))
            else:
                statements.append(self.echo_statement(node.arguments))
            node = node.body
        return statements, node

    @contextmanager
    def bind_expression_names(self, names: Iterable[str]) -> Iterator[None]:
        previous_length = len(self.expression_bindings)
        self.expression_bindings.extend(names)
        try:
            yield
        finally:
            del self.expression_bindings[previous_length:]

    def assert_expression(self, node: AssertOp | ModularAssert) -> str:
        self.assert_counter += 1
        name = f"_scadtopy_assert_{self.assert_counter}"
        bindings = list(dict.fromkeys(self.expression_bindings))
        condition, message = self.assert_values(node.arguments, node)
        if isinstance(node, AssertOp):
            statements, expression = self.statements_before_expression(node.body)
            value = self.expression(expression)
        else:
            children = self.module_list(node.children)
            if len(children) == 1 and not isinstance(children[0], Assignment):
                statements, value = self.module_with_statements(children[0])
            else:
                statements, value = [], self.module_group(children)
        body = "".join(f"    {statement}\n" for statement in statements)
        self.expression_definitions.append(
            f"def {name}({', '.join(bindings)}):\n"
            f"    assert {condition}, {message}\n"
            f"{body}"
            f"    return {value}"
        )
        return f"{name}({', '.join(bindings)})"

    def echo_statement(self, arguments: list[object]) -> str:
        if self.echo_print != "print":
            self.uses_echo_print_alias = True
        values = []
        for argument in arguments:
            if isinstance(argument, PositionalArgument):
                values.append(self.expression(argument.expr))
            elif isinstance(argument, NamedArgument):
                values.extend(
                    (repr(f"{argument.name.name} ="), self.expression(argument.expr))
                )
            else:
                self.note_fixme(argument, "unknown echo argument is not translated")
        return f"{self.echo_print}({', '.join(values)})"

    def echo_expression(self, arguments: list[object], result: str) -> str:
        return f"({self.echo_statement(arguments)}, {result})[1]"

    def module_with_statements(self, node: object) -> tuple[list[str], str]:
        statements = []
        while isinstance(node, (ModularAssert, ModularEcho)):
            statements.append(
                self.assert_statement(node)
                if isinstance(node, ModularAssert)
                else self.echo_statement(node.arguments)
            )
            children = [
                child
                for child in self.module_list(node.children)
                if not isinstance(child, (CommentLine, CommentSpan, BlankLine))
            ]
            if len(children) != 1 or isinstance(children[0], Assignment):
                return statements, self.module_group(children)
            node = children[0]
        return statements, self.module_call(node)

    def module_list(self, nodes: object) -> list[object]:
        if isinstance(nodes, list):
            return nodes
        return [nodes]

    def module_children(self, nodes: object) -> list[str]:
        geometries: list[str] = []
        assertions: list[str] = []
        for node in self.module_list(nodes):
            if isinstance(node, (CommentLine, CommentSpan, BlankLine)):
                continue
            if isinstance(node, ModularModifierDisable):
                self.module_call(node)
                continue
            if isinstance(node, (ModularAssert, ModularEcho)) and not node.children:
                assertions.append(self.module_call(node))
                continue
            geometry = self.module_call(node)
            if geometry != "None":
                if assertions:
                    geometry = f"({', '.join(assertions)}, {geometry})[-1]"
                    assertions = []
                geometries.append(geometry)
        if assertions:
            if geometries:
                geometry = geometries.pop()
                geometries.append(f"({geometry}, {', '.join(assertions)})[0]")
            else:
                geometries.append(f"({', '.join(assertions)}, None)[-1]")
        return geometries

    def module_group(self, nodes: object) -> str:
        if any(isinstance(node, Assignment) for node in self.module_list(nodes)):
            call, _ = self.scoped_module_block(nodes, as_list=False)
            return call
        geometries = self.module_children(nodes)
        if not geometries:
            return "None"
        return self.combine(geometries)

    def scoped_module_block(self, nodes: object, as_list: bool) -> tuple[str, int]:
        children = self.module_list(nodes)
        assignments: dict[str, Assignment] = {}
        for node in children:
            if isinstance(node, Assignment):
                name = node.name.name
                if name in assignments:
                    self.warn(
                        node, f"Duplicate assignment to {name}; using the last value"
                    )
                assignments[name] = node

        self.module_scope_counter += 1
        name = f"_scadtopy_block_{self.module_scope_counter}"
        reserved = self.user_identifiers | self.imported_identifiers
        while any(
            candidate in reserved
            for candidate in (
                name,
                f"{name}_apply",
                *(f"{name}_outer_{i}" for i in range(len(assignments))),
                *(f"{name}_child_{i}" for i in range(len(children))),
                *(f"{name}_{special[1:]}" for special in _SEGMENT_VARIABLES),
            )
        ):
            name += "_"

        ordinary_names = [n for n in assignments if not n.startswith("$")]
        outer_names = [
            binding
            for binding in dict.fromkeys(self.expression_bindings)
            if binding not in ordinary_names
        ]
        needed: set[str] = set()
        assigned: set[str] = set()
        for assignment in assignments.values():
            needed.update(
                (set(ordinary_names) - assigned) & _identifier_names(assignment.expr)
            )
            assigned.add(assignment.name.name)
        shadow_names = [n for n in ordinary_names if n in needed]
        getters = [f"{name}_outer_{i}" for i in range(len(shadow_names))]
        parameters = ", ".join(outer_names + getters)
        lines = [f"def {name}({parameters}):"]
        for variable, getter in zip(shadow_names, getters):
            lines.extend(
                [
                    "    try:",
                    f"        {variable} = {getter}()",
                    "    except NameError:",
                    f"        {variable} = None",
                ]
            )

        previous_overrides = self.segment_overrides
        settings = list(self.segment_tokens())
        bound = list(shadow_names)
        try:
            for variable, assignment in assignments.items():
                unsupported = _unsupported_special_variables(assignment.expr)
                if unsupported:
                    self.note_unsupported(
                        assignment,
                        f"{', '.join(sorted(unsupported))} are not translated",
                    )
                    continue
                with self.bind_expression_names(bound):
                    statements, expression = self.statements_before_expression(
                        assignment.expr
                    )
                    value = self.expression(expression)
                lines.extend(f"    {statement}" for statement in statements)
                if variable in _SEGMENT_VARIABLES:
                    index = _SEGMENT_VARIABLES.index(variable)
                    target = f"{name}_{variable[1:]}"
                    lines.append(
                        f"    {target} = {'int(' + value + ')' if variable == '$fn' else value}"
                    )
                    settings[index] = target
                    self.segment_overrides = tuple(settings)
                    bound.append(target)
                elif variable.startswith("$"):
                    self.note_unsupported(assignment, f"{variable} is not translated")
                else:
                    lines.append(f"    {variable} = {value}")
                    if variable not in bound:
                        bound.append(variable)

            with self.bind_expression_names(bound):
                geometries = self.module_children(
                    [child for child in children if not isinstance(child, Assignment)]
                )
        finally:
            self.segment_overrides = previous_overrides

        result = (
            f"[{', '.join(geometries)}]"
            if as_list
            else self.combine(geometries) if geometries else "None"
        )
        lines.append(f"    return {result}")
        self.expression_definitions.append("\n".join(lines))
        arguments = outer_names + [f"lambda: {variable}" for variable in shadow_names]
        return f"{name}({', '.join(arguments)})", len(geometries)

    def module_loop(
        self, assignments: list[Assignment], body: object, operation: str = "union"
    ) -> str:
        names: list[str] = []
        clauses = []
        for assignment in assignments:
            with self.bind_expression_names(names):
                clauses.append(
                    f"for {assignment.name.name} in {self.expression(assignment.expr)}"
                )
            names.append(assignment.name.name)
        with self.bind_expression_names(names):
            value = self.module_group(body)
        clauses = " ".join(clauses)
        return f"{operation}(*[{value} {clauses}])"

    def module_call(self, node: object) -> str:
        if (
            isinstance(node, ModularCall)
            and node.name.name == "assign"
            and node.name.name not in self.module_names
        ):
            return _tidy(self.assign_module(node))
        if isinstance(node, ModularCall) and any(
            isinstance(child, Assignment) for child in self.module_list(node.children)
        ):
            previous_overrides = self.segment_overrides
            if node.name.name in {"linear_extrude", "offset", "rotate_extrude"}:
                self.segment_overrides = self.segment_settings(node.arguments)
            try:
                block, count = self.scoped_module_block(node.children, as_list=True)
            finally:
                self.segment_overrides = previous_overrides
            name = block.split("(", 1)[0]
            children = [f"{name}_child_{i}" for i in range(count)]
            if count == 1:
                return _tidy(self._module_call(node, children_override=[f"{block}[0]"]))
            if count > 1 and node.name.name in _PIECAD_NAMES:
                self._module_call(node, children_override=children)
                return f"{_PIECAD_NAMES[node.name.name]}(*{block})"
            if count > 1 and node.name.name in {
                *_TRANSFORMS,
                "resize",
                "multmatrix",
                "linear_extrude",
                "rotate_extrude",
                "offset",
                "render",
                "group",
            }:
                return _tidy(
                    self._module_call(node, children_override=[f"union(*{block})"])
                )
            outer_names = list(dict.fromkeys(self.expression_bindings))
            result = self._module_call(node, children_override=children)
            self.expression_definitions.append(
                f"def {name}_apply({', '.join(outer_names + children)}):\n"
                f"    return {_tidy(result)}"
            )
            arguments = ", ".join(outer_names + [f"*{block}"])
            return f"{name}_apply({arguments})"
        return _tidy(self._module_call(node))

    def assign_module(self, node: ModularCall) -> str:
        for argument in node.arguments:
            if not isinstance(argument, (NamedArgument, PositionalArgument)):
                self.note_unsupported(argument, "assign() argument is not translated")
                return "None"
            if isinstance(argument, NamedArgument):
                name = argument.name.name
                if name.startswith("$") and name not in _SEGMENT_VARIABLES:
                    self.note_unsupported(
                        argument, f"assign() binding {name} is not translated"
                    )
                    return "None"
                if not name.startswith("$") and (
                    not name.isidentifier() or keyword.iskeyword(name) or name == "PI"
                ):
                    self.note_unsupported(
                        argument, f"assign() binding {name} is not translated"
                    )
                    return "None"
            unsupported = _unsupported_special_variables(argument.expr)
            if unsupported:
                self.note_unsupported(
                    argument, f"{', '.join(sorted(unsupported))} are not translated"
                )
                return "None"

        self.module_scope_counter += 1
        prefix = f"_scadtopy_assign_{self.module_scope_counter}"
        reserved = self.user_identifiers | self.imported_identifiers
        while any(f"{prefix}_{i}" in reserved for i in range(len(node.arguments))):
            prefix += "_"
        last = {
            argument.name.name: index
            for index, argument in enumerate(node.arguments)
            if isinstance(argument, NamedArgument)
        }
        parameters = []
        values = []
        settings = list(self.segment_tokens())
        bound = []
        for index, argument in enumerate(node.arguments):
            name = argument.name.name if isinstance(argument, NamedArgument) else None
            parameter = f"{prefix}_{index}"
            if name is not None and last[name] == index:
                if name in _SEGMENT_VARIABLES:
                    settings[_SEGMENT_VARIABLES.index(name)] = parameter
                else:
                    parameter = name
                bound.append(parameter)
            value = self.expression(argument.expr)
            if name == "$fn" and last[name] == index:
                value = f"int({value})"
            parameters.append(parameter)
            values.append(value)

        previous_overrides = self.segment_overrides
        self.segment_overrides = tuple(settings)
        definitions_start = len(self.expression_definitions)
        try:
            with self.bind_expression_names(bound):
                body = self.module_group(node.children)
        finally:
            self.segment_overrides = previous_overrides

        possible_collisions = set(bound) & (_PIECAD_EXPORTS | set(dir(builtins)))
        if possible_collisions:
            generated = "\n".join(
                [*self.expression_definitions[definitions_start:], body]
            )
            references = ast.parse(generated)
            collisions = {
                call.func.id
                for call in ast.walk(references)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id in possible_collisions
            }
            collisions.update(
                attribute.value.id
                for attribute in ast.walk(references)
                if isinstance(attribute, ast.Attribute)
                and isinstance(attribute.value, ast.Name)
                and attribute.value.id in possible_collisions
            )
            if collisions:
                self.note_unsupported(
                    node,
                    f"assign() bindings shadow generated calls: {', '.join(sorted(collisions))}",
                )
                return "None"
        return f"(lambda {', '.join(parameters)}: {body})({', '.join(values)})"

    def _module_call(
        self, node: object, children_override: list[str] | None = None
    ) -> str:
        if isinstance(node, ModularFor):
            return self.module_loop(node.assignments, node.body)
        if isinstance(node, ModularIntersectionFor):
            return self.module_loop(node.assignments, node.body, "intersect")
        if isinstance(node, ModularLet):
            with self.bind_expression_names(a.name.name for a in node.assignments):
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
            return self.echo_expression(
                node.arguments, self.module_group(node.children)
            )
        if isinstance(node, ModularAssert):
            return self.assert_expression(node)
        if isinstance(node, ModularModifierShowOnly):
            line = getattr(getattr(node, "position", None), "line", None)
            raise TranslationError(
                f"Show only ('!') is not allowed at line: {line if line is not None else '?'}"
            )
        if isinstance(node, (ModularModifierHighlight, ModularModifierBackground)):
            return self.module_call(node.child)
        if isinstance(node, ModularModifierDisable):
            return "None"
        if not isinstance(node, ModularCall):
            self.note_fixme(node, f"{_node_name(node)} is not translated")
            return "None"

        name = node.name.name
        if (
            name not in self.module_names
            and name not in _CENTER_MODULES
            and name != "import"
        ):
            # OpenSCAD silently ignores arguments a built-in doesn't have.
            node.arguments = [
                argument
                for argument in node.arguments
                if not (
                    isinstance(argument, NamedArgument)
                    and argument.name.name == "center"
                )
            ]
        arguments = self.call_arguments(node.arguments) if name != "import" else []
        previous_segment_overrides = self.segment_overrides
        if name in {"linear_extrude", "offset", "rotate_extrude"}:
            self.segment_overrides = self.segment_settings(node.arguments)
        source_children = [
            child
            for child in self.module_list(node.children)
            if not isinstance(child, (CommentLine, CommentSpan, BlankLine))
        ]
        try:
            children = (
                self.module_children(source_children)
                if children_override is None
                else children_override
            )
        finally:
            self.segment_overrides = previous_segment_overrides
        if source_children and all(child == "None" for child in children):
            return "None"
        children = [child for child in children if child != "None"]

        if name in _TRANSFORMS:
            if not children:
                raise TranslationError(
                    f"{name}() requires a child geometry{_position(node)}"
                )
            base = self.combine(children)
            return self.transform_call(name, node, base)

        if name == "resize":
            self.reject_unexpected_named_arguments(
                node, {"newsize", "auto", "convexity"}
            )
            if len(node.arguments) > 3:
                raise TranslationError(f"Too many resize() arguments{_position(node)}")
            positional = sum(
                isinstance(argument, PositionalArgument) for argument in node.arguments
            )
            for position, parameter in enumerate(("newsize", "auto", "convexity")):
                named = sum(
                    isinstance(argument, NamedArgument)
                    and argument.name.name == parameter
                    for argument in node.arguments
                )
                if named > 1 or (named and positional > position):
                    raise TranslationError(
                        f"resize() got multiple values for {parameter!r}{_position(node)}"
                    )
            size = self.argument_value(node.arguments, ("newsize",), 0)
            auto = self.argument_value(node.arguments, ("auto",), 1)
            if size is None:
                raise TranslationError(f"resize() requires newsize{_position(node)}")
            if not children:
                raise TranslationError(
                    f"resize() requires child geometry{_position(node)}"
                )
            self.uses_resize_helper = True
            return (
                f"_scadtopy_resize({self.combine(children)}, {size}, {auto or 'False'})"
            )

        if name == "multmatrix":
            self.reject_unexpected_named_arguments(node, {"m"})
            matrix = self.argument_expression(node.arguments, ("m",), 0)
            if matrix is None or len(node.arguments) != 1:
                raise TranslationError(
                    f"multmatrix() requires one matrix{_position(node)}"
                )
            if not children:
                raise TranslationError(
                    f"multmatrix() requires a child geometry{_position(node)}"
                )
            return f"{_receiver(self.combine(children))}.transform(({self.expression(matrix)})[:3])"

        if name == "linear_extrude":
            if not children:
                raise TranslationError(
                    f"linear_extrude() requires a child geometry{_position(node)}"
                )
            self.reject_unexpected_named_arguments(
                node,
                {
                    "height",
                    "center",
                    "twist",
                    "slices",
                    "scale",
                    "convexity",
                    "$fn",
                    "$fa",
                    "$fs",
                },
            )
            height = self.argument_value(node.arguments, ("height",), 0)
            if height is None:
                raise TranslationError(
                    f"linear_extrude() requires height{_position(node)}"
                )
            center = self.argument_value(node.arguments, ("center",), 1)
            twist = self.argument_value(node.arguments, ("twist",), -1)
            slices = self.argument_value(node.arguments, ("slices",), -1)
            scale = self.argument_value(node.arguments, ("scale",), -1)
            if twist is None and slices is None and scale is None:
                extruded = f"{_receiver(self.combine(children))}.extrude({height})"
                if center == "True":
                    extruded += f".translate([0, 0, -({height}) / 2])"
                elif center is not None and center != "False":
                    extruded += (
                        f".translate([0, 0, (-({height}) / 2 if {center} else 0)])"
                    )
                return _tidy(extruded)
            direct = self.direct_linear_extrude(
                node, self.combine(children), height, center, twist, slices, scale
            )
            if direct is not None:
                return direct
            self.uses_linear_extrude_helper = True
            self.uses_math = True
            arguments = [
                self.combine(children),
                height,
                center or "False",
                twist or "0",
                slices or "None",
                scale or "1",
                *self.segment_arguments(self.segment_settings(node.arguments)),
            ]
            return f"_scadtopy_linear_extrude({', '.join(arguments)})"

        if name == "rotate_extrude":
            self.reject_unexpected_named_arguments(
                node, {"angle", "convexity", "$fn", "$fa", "$fs"}
            )
            if not children:
                return "None"
            angle = self.argument_value(node.arguments, ("angle",), 0)
            profile = self.combine(children)
            angle = angle or "360.0"
            radius = (
                "max(abs(_scadtopy_profile.bounding_box()[0]), "
                "abs(_scadtopy_profile.bounding_box()[2]))"
            )
            segments = self.segment_count(node, radius)
            return (
                f"(lambda _scadtopy_profile: revolve(_scadtopy_profile, {angle}, "
                f"segments={segments}))({profile})"
            )

        if name in _PIECAD_NAMES:
            if not children:
                raise TranslationError(
                    f"{name}() requires child geometry{_position(node)}"
                )
            if node.arguments:
                raise TranslationError(
                    f"{name}() does not accept arguments{_position(node)}"
                )
            if name == "union" and len(children) == 1:
                return children[0]
            return f"{_PIECAD_NAMES[name]}({', '.join(children)})"

        if name == "minkowski":
            if node.arguments:
                raise TranslationError(
                    f"minkowski() does not accept arguments{_position(node)}"
                )
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
            geometry = self.combine(children)
            if delta is not None:
                join = f'"square" if {chamfer} else "miter"' if chamfer else '"miter"'
                call = f"{_receiver(geometry)}.offset({delta}, {join}, 1000.0"
            else:
                call = f'{_receiver(geometry)}.offset({radius or "1"}, "round"'
                call += f", segments={self.segment_count(node, radius or '1.0')}"
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

        if name in {"render", "group"}:
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
        if name == "import":
            self.reject_children(node, children)
            file_node = self.argument_expression(node.arguments, ("file",), 0)
            if file_node is None:
                self.note_fixme(node, "import() requires a file name")
                return "None"
            unsupported_arguments = {
                argument.name.name
                for argument in node.arguments
                if isinstance(argument, NamedArgument)
            } - {"file", "convexity"}
            positional = [
                argument
                for argument in node.arguments
                if isinstance(argument, PositionalArgument)
            ]
            if len(positional) > 1:
                unsupported_arguments.add("positional layer")
            if unsupported_arguments:
                self.note_unsupported(
                    node,
                    f"import() parameter(s) {', '.join(sorted(unsupported_arguments))} "
                    "are not supported by Piecad load()",
                )
                return "None"
            if isinstance(file_node, StringLiteral):
                extension = file_node.val.rpartition(".")[2].lower()
                if extension not in _PIECAD_LOAD_EXTENSIONS:
                    self.note_fixme(
                        node,
                        f"import() of .{extension} files is not supported by Piecad load()",
                    )
                    return "None"
            filename = self.expression(file_node)
            warning_argument = (
                f'"{file_node.val}"'
                if isinstance(file_node, StringLiteral)
                else filename
            )
            self.warn(
                node,
                f"import({warning_argument}) is passed to Piecad unchanged and "
                "resolves relative to the Python process working directory, "
                "not the .scad file directory",
            )
            return f"load({filename})"
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
                {
                    "r": "radius",
                    "d": "diameter",
                    "$fn": "segments",
                },
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
                {
                    "r": "radius",
                    "d": "diameter",
                    "$fn": "segments",
                },
            )
        if name == "polygon":
            self.reject_children(node, children)
            points = self.argument_value(node.arguments, ("points",), 0)
            paths = self.argument_value(node.arguments, ("paths",), 1)
            self.reject_unexpected_named_arguments(node, {"points", "paths"})
            if paths is not None:
                if points is None:
                    raise TranslationError(
                        f"polygon() requires points{_position(node)}"
                    )
                return (
                    f"polygon([[{points}[int(index)] for index in reversed(path)] "
                    f"for path in {paths}])"
                )
            if points is not None:
                return f"polygon([[point for point in reversed({points})]])"
            raise TranslationError(f"polygon() requires points{_position(node)}")

        if name == "children" or (name == "child" and name not in self.module_names):
            self.uses_children_helper = True
            index = self.argument_expression(node.arguments, ("index",), 0)
            child_list = "_children" if self.in_module or name == "children" else "()"
            if index is not None:
                return f"_scadtopy_children({child_list}, {self.expression(index)})"
            if name == "child":
                return f"_scadtopy_children({child_list}, 0)"
            return f"_scadtopy_children({child_list})"
        if name in self.module_names:
            passes_children = bool(children and name in self.children_modules)
            arguments = self.module_call_arguments(
                name, node.arguments, passes_children
            )
            if passes_children:
                arguments.append(f"_children=[{', '.join(children)}]")
            return f"{self.py_name(name)}({', '.join(arguments)})"
        if children:
            self.note_fixme(
                node, f"child geometry for module {name!r} is not translated"
            )
            return "None"
        self.note_fixme(node, f"OpenSCAD module {name!r} has no Piecad mapping")
        return "None"
        raise AssertionError("unreachable")

    def direct_linear_extrude(
        self,
        node: ModularCall,
        profile: str,
        height: str,
        center: str | None,
        twist: str | None,
        slices: str | None,
        scale: str | None,
    ) -> str | None:
        def number(expression: str) -> int | float | None:
            try:
                value = ast.literal_eval(expression)
            except (ValueError, SyntaxError):
                return None
            return value if type(value) in (int, float) else None

        twist_value = number(twist or "0")
        slices_value = number(slices) if slices is not None else None
        if twist_value is None or (slices is not None and slices_value is None):
            return None
        if not math.isfinite(twist_value) or (
            slices_value is not None and not math.isfinite(slices_value)
        ):
            return None
        if center not in (None, "False", "True"):
            return None
        if center == "True" and number(height) is None:
            return None
        for name in ("$fn", "$fa", "$fs"):
            value = self.argument_value(node.arguments, (name,), -1)
            if value is not None and number(value) is None:
                return None

        if slices_value:
            divisions = int(slices_value)
        elif not twist_value:
            divisions = 1
        else:
            fn = self.argument_value(node.arguments, ("$fn",), -1)
            fn_value = number(fn) if fn is not None else None
            if fn_value is None or not math.isfinite(fn_value) or int(fn_value) <= 0:
                return None
            divisions = max(
                1, math.ceil(abs(twist_value) / 360 * max(int(fn_value), 3))
            )

        scale = scale or "1"
        scale_node = ast.parse(scale, mode="eval").body
        if isinstance(scale_node, ast.List):
            scale = ast.unparse(ast.Tuple(elts=scale_node.elts, ctx=ast.Load()))
        elif number(scale) is not None:
            scale = f"({scale}, {scale})"
        else:
            return None

        if twist_value > 0:
            profile = f"{_receiver(profile)}.mirror((True, False))"
        result = (
            f"extrude_transforming({profile}, {height}, {divisions}, "
            f"{abs(twist_value)!r}, {scale})"
        )
        if twist_value > 0:
            result += ".mirror((True, False, False))"
        if center == "True":
            result += f".translate([0, 0, -({height}) / 2])"
        return result

    def transform_call(self, name: str, node: ModularCall, geometry: str) -> str:
        if name == "translate":
            self.reject_unexpected_named_arguments(node, {"v"})
            vector = self.argument_expression(node.arguments, ("v",), 0)
            if vector is None or len(node.arguments) != 1:
                raise TranslationError(
                    f"translate() requires one vector{_position(node)}"
                )
            rendered = self.expression(vector)
            dimensions = self.geometry_dimension(node.children)
            if dimensions == 2:
                parsed = ast.parse(rendered, mode="eval").body
                if isinstance(parsed, ast.List) and len(parsed.elts) == 2:
                    pass
                elif (
                    isinstance(parsed, ast.List)
                    and len(parsed.elts) == 3
                    and isinstance(parsed.elts[2], ast.Constant)
                ):
                    rendered = ast.unparse(
                        ast.List(elts=parsed.elts[:2], ctx=ast.Load())
                    )
                else:
                    rendered = f"({rendered})[:2]"
                return f"{_receiver(geometry)}.translate({rendered})"
            if dimensions == 3:
                return f"{_receiver(geometry)}.translate({rendered})"
            self.uses_translate_helper = True
            return f"_scadtopy_translate({_receiver(geometry)}, {rendered})"

        if name == "scale":
            self.reject_unexpected_named_arguments(node, {"v"})
            factors = self.argument_expression(node.arguments, ("v",), 0)
            if factors is None or len(node.arguments) != 1:
                raise TranslationError(
                    f"scale() requires one factor or vector{_position(node)}"
                )
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
                axis_index = next(
                    index for index, value in enumerate(axis_values) if value != 0
                )
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
                raise TranslationError(
                    f"mirror() requires one axis vector{_position(node)}"
                )
            axis_values = self.literal_axis(axis, name)
            if axis_values is None:
                return "None"
            flags = [str(value != 0) for value in axis_values]
            return f"{_receiver(geometry)}.mirror(({', '.join(flags)}))"

        raise AssertionError(f"Missing transform mapping for {name}")

    def literal_axis(self, expression: object, operation: str) -> list[float] | None:
        if (
            not isinstance(expression, ListComprehension)
            or len(expression.elements) != 3
        ):
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
            if isinstance(argument, NamedArgument)
            and argument.name.name not in allowed
            and argument.name.name != "convexity"
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
        if height is None:
            height = "1.0"
        unsupported_arguments = {
            argument.name.name
            for argument in node.arguments
            if isinstance(argument, NamedArgument)
        } - {
            "h",
            "height",
            "r",
            "r1",
            "r2",
            "radius",
            "d",
            "d1",
            "d2",
            "diameter",
            "center",
            "$fn",
            "$fa",
            "$fs",
        }
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
        if r1 is not None:
            segment_radius = f"max(abs({r1}), abs({r2}))"
        else:
            segment_radius = radius or "1.0"
        keywords = [f"segments={self.segment_count(node, segment_radius)}"]
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
        allowed = set(aliases) | set(aliases.values()) | {"$fa", "$fs"}
        self.reject_unexpected_named_arguments(node, allowed)
        positional_count = sum(
            isinstance(argument, PositionalArgument) for argument in node.arguments
        )
        if positional_count > 2:
            raise TranslationError(f"Too many {target}() arguments{_position(node)}")
        positional_index = 0
        for argument in node.arguments:
            if isinstance(argument, PositionalArgument):
                if positional_index == 0:
                    values.append(self.expression(argument.expr))
                positional_index += 1
            elif isinstance(argument, NamedArgument):
                if argument.name.name in {"$fn", "$fa", "$fs", "segments"}:
                    continue
                if argument.name.name == "convexity":
                    continue
                name = aliases.get(argument.name.name, argument.name.name)
                if name.startswith("$"):
                    raise TranslationError(
                        f"Unsupported {target} parameter {name!r}{_position(node)}"
                    )
                if name == "diameter":
                    values.append(f"radius=({self.expression(argument.expr)}) / 2")
                else:
                    values.append(f"{name}={self.expression(argument.expr)}")
        radius = self.argument_value(node.arguments, ("r", "radius"), 0)
        diameter = self.argument_value(node.arguments, ("d", "diameter"), -1)
        if diameter is not None:
            radius = f"({diameter}) / 2"
        if radius is not None:
            values.append(
                f"segments={self.segment_count(node, radius, positional_fn_index=1)}"
            )
        return f"{target}({', '.join(values)})"

    def segment_count(
        self,
        node: ModularCall,
        radius: str,
        positional_fn_index: int = -1,
    ) -> str:
        settings = self.segment_settings(node.arguments, positional_fn_index)
        self.uses_segments_helper = True
        arguments = [radius, *self.segment_arguments(settings)]
        return f"_scadtopy_calculate_segments({', '.join(arguments)})"

    def segment_arguments(self, settings: tuple[str, str, str]) -> list[str]:
        defaults = tuple(f"_scadtopy_default_{name}" for name in ("fn", "fa", "fs"))

        def may_have_side_effects(expression: str) -> bool:
            parsed = ast.parse(expression, mode="eval").body
            if isinstance(parsed, (ast.Name, ast.Constant)):
                return False
            try:
                ast.literal_eval(parsed)
            except (ValueError, TypeError):
                return True
            return False

        # Preserve default reads before later arguments with possible side effects.
        keep = [
            value != defaults[index]
            or any(
                later != defaults[later_index] and may_have_side_effects(later)
                for later_index, later in enumerate(settings[index + 1 :], index + 1)
            )
            for index, value in enumerate(settings)
        ]
        if all(keep):
            return list(settings)
        return [
            f"{name}={value}"
            for name, value, include in zip(("fn", "fa", "fs"), settings, keep)
            if include
        ]

    def segment_tokens(self) -> tuple[str, str, str]:
        if self.segment_overrides:
            return self.segment_overrides
        if self.in_module:
            return (
                "_scadtopy_special_fn",
                "_scadtopy_special_fa",
                "_scadtopy_special_fs",
            )
        return (
            "_scadtopy_default_fn",
            "_scadtopy_default_fa",
            "_scadtopy_default_fs",
        )

    def segment_settings(
        self,
        arguments: list[object],
        positional_fn_index: int = -1,
    ) -> tuple[str, str, str]:
        fn, fa, fs = self.segment_tokens()
        fn_override = self.argument_value(arguments, ("$fn",), positional_fn_index)
        fa_override = self.argument_value(arguments, ("$fa",), -1)
        fs_override = self.argument_value(arguments, ("$fs",), -1)
        return (
            f"int({fn_override})" if fn_override is not None else fn,
            fa_override if fa_override is not None else fa,
            fs_override if fs_override is not None else fs,
        )

    def module_call_arguments(
        self, module_name: str, arguments: list[object], passes_children: bool
    ) -> list[str]:
        special_names = {
            "$fn": "_scadtopy_special_fn",
            "$fa": "_scadtopy_special_fa",
            "$fs": "_scadtopy_special_fs",
        }
        rendered: list[str] = []
        provided: set[str] = set()
        needed = self.segment_modules.get(module_name, set(_SEGMENT_VARIABLES))
        for argument in arguments:
            if isinstance(argument, PositionalArgument):
                rendered.append(self.expression(argument.expr))
            elif isinstance(argument, NamedArgument):
                name = argument.name.name
                if name in special_names:
                    value = self.expression(argument.expr)
                    if name == "$fn":
                        value = f"int({value})"
                    if name in needed:
                        rendered.append(f"{special_names[name]}={value}")
                        provided.add(name)
                    elif not (
                        isinstance(argument.expr, NumberLiteral)
                        and math.isfinite(argument.expr.val)
                    ):
                        # Preserve evaluation and its order without adding a parameter.
                        rendered.append(f"**({value}, {{}})[1]")
                else:
                    rendered.append(f"{name}={self.expression(argument.expr)}")
            else:
                self.unsupported(argument)

        current_fn, current_fa, current_fs = self.segment_tokens()
        inherited = {
            "$fn": current_fn,
            "$fa": current_fa,
            "$fs": current_fs,
        }
        local = (
            module_name in self.local_module_names
            and module_name not in self.imported_identifiers
            and self.py_name(module_name) not in self.imported_identifiers
            and not self.unknown_imports
            and not passes_children
            and module_name not in self.modules_with_nested_defaults
        )
        rendered.extend(
            f"{special_names[name]}={value}"
            for name, value in inherited.items()
            if name in needed
            and name not in provided
            and not (
                local
                and value == f"_scadtopy_default_{name[1:]}"
                and value not in self.user_identifiers
            )
        )
        return rendered

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
        return positional_index < len(positional) and isinstance(
            positional[positional_index].expr, ListComprehension
        )

    def combine(self, geometries: list[str]) -> str:
        return (
            geometries[0] if len(geometries) == 1 else f"union({', '.join(geometries)})"
        )

    def geometry_dimension(
        self,
        nodes: object,
        active_modules: frozenset[str] = frozenset(),
        children_dimension: int | None = None,
    ) -> int | None:
        if isinstance(nodes, (list, tuple)):
            children = [
                node
                for node in nodes
                if isinstance(node, ModuleInstantiation)
                and not isinstance(node, ModularModifierDisable)
                and not (
                    isinstance(node, (ModularAssert, ModularEcho)) and not node.children
                )
            ]
            if not children:
                return None
            dimensions = {
                self.geometry_dimension(child, active_modules, children_dimension)
                for child in children
            }
            return dimensions.pop() if len(dimensions) == 1 else None
        if isinstance(nodes, (ModularModifierHighlight, ModularModifierBackground)):
            return self.geometry_dimension(
                nodes.child, active_modules, children_dimension
            )
        if isinstance(nodes, (ModularFor, ModularIntersectionFor)):
            return self.geometry_dimension(
                nodes.body, active_modules, children_dimension
            )
        if isinstance(nodes, ModularIfElse):
            first = self.geometry_dimension(
                nodes.true_branch, active_modules, children_dimension
            )
            second = self.geometry_dimension(
                nodes.false_branch, active_modules, children_dimension
            )
            return first if first == second else None
        if isinstance(nodes, ModularIf):
            return self.geometry_dimension(
                nodes.true_branch, active_modules, children_dimension
            )
        if isinstance(nodes, (ModularLet, ModularAssert, ModularEcho)):
            return self.geometry_dimension(
                nodes.children, active_modules, children_dimension
            )
        if not isinstance(nodes, ModularCall):
            return None
        name = nodes.name.name
        if name == "children" or (name == "child" and name not in self.module_names):
            return children_dimension
        if name == "assign" and name not in self.module_names:
            return self.geometry_dimension(
                nodes.children, active_modules, children_dimension
            )
        if name == "import":
            file_node = self.argument_expression(nodes.arguments, ("file",), 0)
            if isinstance(file_node, StringLiteral):
                extension = file_node.val.rpartition(".")[2].lower()
                if extension in _PIECAD_LOAD_EXTENSIONS:
                    return 2 if extension in {"svg", "dxf"} else 3
            return None
        if name in {"square", "circle", "polygon", "text", "projection", "offset"}:
            return 2
        if name in {
            "cube",
            "sphere",
            "cylinder",
            "polyhedron",
            "linear_extrude",
            "rotate_extrude",
            "minkowski",
        }:
            return 3
        if name in {
            "translate",
            "rotate",
            "scale",
            "mirror",
            "resize",
            "multmatrix",
            "color",
            "render",
            "group",
            "union",
            "difference",
            "intersection",
            "hull",
        }:
            return self.geometry_dimension(
                nodes.children, active_modules, children_dimension
            )
        declaration = self.geometry_modules.get(name)
        if declaration is not None and name not in active_modules:
            child_dimension = self.geometry_dimension(
                nodes.children, active_modules, children_dimension
            )
            return self.geometry_dimension(
                declaration.children, active_modules | {name}, child_dimension
            )
        return None

    def note_fixme(self, node: object, message: str) -> None:
        line = _position(node).removeprefix(" at line ")
        location = f" at line {line}" if line else ""
        fixme = f"pass  # FIXME: {message}{location}"
        if fixme not in self.diagnostics:
            self.diagnostics.append(fixme)

    def note_unsupported(self, node: object, message: str) -> None:
        diagnostic = f"# UNSUPPORTED: {message}{_position(node)}"
        if diagnostic not in self.diagnostics:
            self.diagnostics.append(diagnostic)
            self.warn(node, f"UNSUPPORTED: {message}")

    def let_expression(self, assignments: list[Assignment], body: str) -> str:
        for index in reversed(range(len(assignments))):
            assignment = assignments[index]
            with self.bind_expression_names(a.name.name for a in assignments[:index]):
                value = self.expression(assignment.expr)
            body = f"(lambda {assignment.name.name}: {body})" f"({value})"
        return body

    def list_comprehension(self, node: ListComprehension) -> str:
        elements = [
            element
            for element in node.elements
            if not isinstance(element, (CommentLine, CommentSpan, BlankLine))
        ]
        if not elements:
            return "[]"
        controls = (ListCompCFor, ListCompFor, ListCompIf, ListCompIfElse)
        if len(elements) == 1 and isinstance(elements[0], ListCompEach):
            element = elements[0]
            value = self.vector_element_value(element.body)
            if isinstance(element.body, ListComprehension):
                return value
            return f"[] + ({value})"
        if any(
            isinstance(element, ListCompLet)
            or (
                isinstance(element, ListCompEach)
                and not isinstance(element.body, ListComprehension)
            )
            for element in elements
        ):
            segments = [self.vector_segment(element) for element in elements]
            return f"sum([{', '.join(segments)}], [])"
        if len(elements) == 1 and isinstance(elements[0], controls):
            return self.vector_segment(elements[0])
        values = [
            (
                f"*{self.vector_segment(element)}"
                if isinstance(element, controls)
                else (
                    f"*{self.expression(element.body)}"
                    if isinstance(element, ListCompEach)
                    else self.expression(element)
                )
            )
            for element in elements
        ]
        return f"[{', '.join(values)}]"

    def comprehension_let_is_safe(
        self, element: LetOp | ListCompLet, clauses: list[str]
    ) -> bool:
        if not clauses or _contains_function_literal(element):
            return False
        names = {assignment.name.name for assignment in element.assignments}
        try:
            comprehension = ast.parse(f"[None {' '.join(clauses)}]", mode="eval").body
        except SyntaxError:
            return False
        assert isinstance(comprehension, ast.ListComp)
        bound: set[str] = set()
        for index, generator in enumerate(comprehension.generators):
            if index and any(
                isinstance(node, ast.Name) and node.id in names - bound
                for node in ast.walk(generator.iter)
            ):
                return False
            bound.update(
                node.id
                for node in ast.walk(generator.target)
                if isinstance(node, ast.Name)
            )
            for condition in generator.ifs:
                if any(
                    isinstance(node, ast.Name) and node.id in names - bound
                    for node in ast.walk(condition)
                ):
                    return False
        for assignment in element.assignments:
            if (names - bound) & _identifier_names(assignment.expr):
                return False
            bound.add(assignment.name.name)
        return True

    def c_for_segment(self, element: ListCompCFor, clauses: list[str]) -> str:
        initial_names = list(
            dict.fromkeys(assignment.name.name for assignment in element.inits)
        )
        update_names = [assignment.name.name for assignment in element.incrs]
        names = initial_names + update_names
        if any(not name.isidentifier() or keyword.iskeyword(name) for name in names):
            self.note_unsupported(
                element, "C-style comprehension with invalid bindings"
            )
            return "[]"

        extra_names = set(update_names) - set(initial_names)
        needed = extra_names & _identifier_names([element.condition, element.body])
        assigned: set[str] = set()
        for assignment in element.incrs:
            needed.update((extra_names - assigned) & _identifier_names(assignment.expr))
            assigned.add(assignment.name.name)
        seed_names = list(dict.fromkeys(n for n in update_names if n in needed))

        simple = self.simple_c_for_segment(element, clauses)
        if simple is not None:
            return simple

        self.c_for_counter += 1
        name = f"_scadtopy_c_for_{self.c_for_counter}"
        reserved = self.user_identifiers | self.imported_identifiers
        count = len(element.inits) + len(element.incrs)
        while any(
            candidate in reserved
            for candidate in (
                name,
                f"{name}_body",
                f"{name}_count",
                f"{name}_item",
                f"{name}_duplicate",
                *(f"{name}_outer_{index}" for index in range(len(seed_names))),
                *(f"{name}_update_{index}" for index in range(len(element.incrs))),
                *(f"{name}_ignored_{index}" for index in range(count)),
            )
        ):
            name += "_"
        outer_names = [
            binding
            for binding in dict.fromkeys(self.expression_bindings)
            if binding not in names
        ]
        state_names = initial_names + seed_names
        duplicate_name = f"{name}_duplicate"
        duplicates = len(initial_names) != len(element.inits) or len(
            set(update_names)
        ) != len(update_names)
        if duplicates:
            self.expression_definitions.append(
                f"def {duplicate_name}(value, name):\n"
                "    import warnings\n"
                "    warnings.warn(f'Ignoring duplicate variable assignment "
                "{name} = {value!r}', stacklevel=2)"
            )

        with self.bind_expression_names(state_names):
            condition = self.expression(element.condition)
            body = self.vector_segment(element.body)
            updates = []
            update_bindings: list[str] = []
            for index, assignment in enumerate(element.incrs):
                with self.bind_expression_names(update_bindings):
                    value = self.expression(assignment.expr)
                if _contains_function_literal(assignment.expr):
                    available = list(dict.fromkeys(state_names + update_bindings))
                    try:
                        expression = ast.parse(value, mode="eval")
                    except SyntaxError:
                        snapshot = available
                    else:
                        referenced = {
                            node.id
                            for node in ast.walk(expression)
                            if isinstance(node, ast.Name)
                        }
                        snapshot = [
                            binding for binding in available if binding in referenced
                        ]
                    helper = f"{name}_update_{index}"
                    params = ", ".join(snapshot)
                    updates.extend(
                        [
                            f"        def {helper}({params}):",
                            f"            return {value}",
                        ]
                    )
                    value = f"{helper}({params})"
                target = assignment.name.name
                if target in update_bindings:
                    updates.append(f"        {duplicate_name}({value}, {target!r})")
                else:
                    updates.append(f"        {target} = {value}")
                    update_bindings.append(target)

        body_name = f"{name}_body"
        counter_name = f"{name}_count"
        getter_names = [f"{name}_outer_{index}" for index in range(len(seed_names))]
        parameters = ", ".join(initial_names + outer_names + getter_names)
        arguments = ", ".join(
            initial_names
            + outer_names
            + [f"lambda: {binding}" for binding in seed_names]
        )
        definition = [
            f"def {name}({parameters}):",
        ]
        for binding, getter in zip(seed_names, getter_names):
            definition.extend(
                [
                    "    try:",
                    f"        {binding} = {getter}()",
                    "    except NameError:",
                    f"        {binding} = None",
                ]
            )
        definition.extend([f"    {counter_name} = 0", f"    while {condition}:"])
        if _contains_function_literal(element.body):
            definition.extend(
                [
                    f"        def {body_name}({', '.join(state_names)}):",
                    f"            return {_tidy(body)}",
                    f"        yield from {body_name}({', '.join(state_names)})",
                ]
            )
        else:
            definition.append(f"        yield from {_tidy(body)}")
        definition.extend(
            [
                f"        {counter_name} += 1",
                f"        if {counter_name} > 1000000:",
                "            raise RuntimeError('OpenSCAD C-style loop limit exceeded')",
                *updates,
            ]
        )
        self.expression_definitions.append("\n".join(definition))
        prior_names: set[str] = set()
        independent = True
        for assignment in element.inits:
            if assignment.name.name in prior_names or prior_names & _identifier_names(
                assignment.expr
            ):
                independent = False
                break
            prior_names.add(assignment.name.name)
        if independent:
            values = [self.expression(assignment.expr) for assignment in element.inits]
            values.extend(outer_names)
            values.extend(f"lambda: {binding}" for binding in seed_names)
            call = f"{name}({', '.join(values)})"
        else:
            bindings: list[tuple[str, str]] = []
            seen: list[str] = []
            for index, assignment in enumerate(element.inits):
                with self.bind_expression_names(seen):
                    value = self.expression(assignment.expr)
                target = assignment.name.name
                if target in seen:
                    value = f"{duplicate_name}({value}, {target!r})"
                    target = f"{name}_ignored_{index}"
                else:
                    seen.append(target)
                bindings.append((target, value))
            call = f"{name}({arguments})"
            for target, value in reversed(bindings):
                call = f"(lambda {target}: {call})({value})"
        if clauses:
            item = f"{name}_item"
            result = f"[{item} {' '.join(clauses)} for {item} in {call}]"
        else:
            result = f"[*{call}]"
        return result

    def simple_c_for_segment(
        self, element: ListCompCFor, clauses: list[str]
    ) -> str | None:
        if (
            len(element.inits) != 1
            or len(element.incrs) != 1
            or _contains_function_literal(element.body)
        ):
            return None
        initial, update = element.inits[0], element.incrs[0]
        name = initial.name.name
        condition = element.condition
        increment = update.expr
        if (
            update.name.name != name
            or not isinstance(
                condition,
                (LessThanOp, LessThanOrEqualOp, GreaterThanOp, GreaterThanOrEqualOp),
            )
            or not isinstance(condition.left, Identifier)
            or condition.left.name != name
            or not isinstance(increment, (AdditionOp, SubtractionOp))
            or not isinstance(increment.left, Identifier)
            or increment.left.name != name
        ):
            return None
        start = _literal_number(initial.expr)
        stop = _literal_number(condition.right)
        step = _literal_number(increment.right)
        if (
            start is None
            or stop is None
            or step is None
            or not all(
                float(value).is_integer() and abs(value) <= 2**52
                for value in (start, stop, step)
            )
            or step == 0
        ):
            return None
        if isinstance(increment, SubtractionOp):
            step = -step
        ascending = isinstance(condition, (LessThanOp, LessThanOrEqualOp))
        inclusive = isinstance(condition, (LessThanOrEqualOp, GreaterThanOrEqualOp))
        if (step > 0) != ascending:
            return None
        end = int(stop) + (1 if ascending else -1) if inclusive else int(stop)
        count = len(range(int(start), end, int(step)))
        if count > 1000000:
            return None
        if inclusive:
            distance = (stop - start) / step
            range_count = (
                math.floor(math.nextafter(distance, math.inf)) + 1
                if distance >= 0
                else 0
            )
            if range_count != count:
                return None
        iterator = f"frange({start!r}, {stop!r}, {step!r}"
        if inclusive:
            iterator += ", inclusive=True"
        iterator += ")"
        with self.bind_expression_names([name]):
            return self.vector_segment(
                element.body, [*clauses, f"for {name} in {iterator}"]
            )

    def vector_segment(self, element: object, clauses: list[str] | None = None) -> str:
        clauses = list(clauses or [])
        if isinstance(element, ListCompFor):
            names: list[str] = []
            for assignment in element.assignments:
                with self.bind_expression_names(names):
                    clauses.append(
                        f"for {assignment.name.name} in ({self.expression(assignment.expr)})"
                    )
                names.append(assignment.name.name)
            with self.bind_expression_names(names):
                return self.vector_segment(element.body, clauses)
        if isinstance(element, ListCompIf):
            if not any(clause.startswith("for ") for clause in clauses):
                dummy = "_scadtopy_dummy"
                while (
                    dummy in self.user_identifiers or dummy in self.imported_identifiers
                ):
                    dummy += "_"
                clauses.insert(0, f"for {dummy} in [None]")
            clauses.append(f"if ({self.expression(element.condition)})")
            return self.vector_segment(element.true_expr, clauses)
        if isinstance(element, ListCompIfElse):
            controls = (
                ListCompEach,
                ListCompCFor,
                ListCompFor,
                ListCompIf,
                ListCompIfElse,
                ListCompLet,
            )
            if isinstance(element.true_expr, controls) or isinstance(
                element.false_expr, controls
            ):
                condition = self.expression(element.condition)
                true_value = self.vector_segment(element.true_expr)
                false_value = self.vector_segment(element.false_expr)
                value = f"({true_value} if {condition} else {false_value})"
                if not clauses:
                    return value
                item = "_scadtopy_if_item"
                while (
                    item in self.user_identifiers or item in self.imported_identifiers
                ):
                    item += "_"
                return f"[{item} {' '.join(clauses)} for {item} in {value}]"
            value = (
                f"({self.vector_element_value(element.true_expr)} if "
                f"{self.expression(element.condition)} else "
                f"{self.vector_element_value(element.false_expr)})"
            )
            return f"[{value} {' '.join(clauses)}]".rstrip()
        if isinstance(element, (LetOp, ListCompLet)) and self.comprehension_let_is_safe(
            element, clauses
        ):
            names = []
            for assignment in element.assignments:
                with self.bind_expression_names(names):
                    clauses.append(
                        f"for {assignment.name.name} in [{self.expression(assignment.expr)}]"
                    )
                names.append(assignment.name.name)
            with self.bind_expression_names(names):
                return self.vector_segment(element.body, clauses)
        if isinstance(element, ListCompLet):
            with self.bind_expression_names(a.name.name for a in element.assignments):
                value = self.vector_segment(element.body, clauses)
            return self.let_expression(element.assignments, value)
        if isinstance(element, ListCompEach):
            value = self.vector_element_value(element.body)
            self.each_counter += 1
            name = f"_scadtopy_each_{self.each_counter}"
            while name in self.user_identifiers or name in self.imported_identifiers:
                name += "_"
            self.expression_definitions.append(
                f"def {name}(value):\n"
                "    import builtins\n"
                "    if value is None:\n"
                "        return []\n"
                "    if builtins.isinstance(value, (builtins.list, builtins.tuple, "
                "builtins.str, builtins.range)):\n"
                "        return [*value]\n"
                "    return [value]"
            )
            value = f"{name}({value})"
            if clauses:
                item = "_scadtopy_each_item"
                while (
                    item in self.user_identifiers or item in self.imported_identifiers
                ):
                    item += "_"
                return f"[{item} {' '.join(clauses)} for {item} in ({value})]"
            return value
        if isinstance(element, ListCompCFor):
            return self.c_for_segment(element, clauses)
        value = self.vector_element_value(element)
        return f"[{value} {' '.join(clauses)}]".rstrip()

    def vector_element_value(self, element: object) -> str:
        if isinstance(
            element,
            (ListCompCFor, ListCompFor, ListCompIf, ListCompIfElse, ListCompLet),
        ):
            segment = self.vector_segment(element)
            self.note_fixme(
                element, "nested list-comprehension control is approximated"
            )
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
            if node.name in ("$fn", "$fa", "$fs"):
                return self.segment_tokens()["$fn $fa $fs".split().index(node.name)]
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
            return (
                f"list(frange({self.expression(node.start)}, "
                f"{self.expression(node.end)}, {self.expression(node.step)}, "
                "inclusive=True))"
            )
        if isinstance(node, LetOp):
            with self.bind_expression_names(a.name.name for a in node.assignments):
                body = self.expression(node.body)
            return self.let_expression(node.assignments, body)
        if isinstance(node, EchoOp):
            return self.echo_expression(node.arguments, self.expression(node.body))
        if isinstance(node, AssertOp):
            return self.assert_expression(node)
        if isinstance(node, FunctionLiteral):
            parameters = self.parameters(node.parameters)
            with self.bind_expression_names(p.name.name for p in node.parameters):
                body = self.expression(node.body)
            return f"lambda {parameters}: {body}"
        if isinstance(node, RenderExpression):
            return self.module_group(node.children)
        if isinstance(node, ListComprehension):
            return self.list_comprehension(node)
        if isinstance(node, PrimaryCall):
            if not isinstance(node.left, Identifier):
                callable_expr = _receiver(self.expression(node.left))
                arguments = self.call_arguments(node.arguments)
                return f"{callable_expr}({', '.join(arguments)})"
            name = node.left.name
            arguments = self.call_arguments(node.arguments)
            if name not in self.function_names:
                if name in _MATH_FUNCTIONS:
                    self.uses_math = True
                    return f"{_MATH_FUNCTIONS[name]}({', '.join(arguments)})"
                builtin = self.builtin_call(name, node.arguments, arguments)
                if builtin is not None:
                    return builtin
                if name == "norm" and len(arguments) == 1:
                    self.uses_math = True
                    return f"math.hypot(*{arguments[0]})"
                if name == "cross" and len(node.arguments) == 2:
                    return self.cross(node.arguments, arguments)
            return f"{self.py_name(name)}({', '.join(arguments)})"
        if isinstance(node, PrimaryIndex):
            receiver = _receiver(self.expression(node.left))
            if (
                isinstance(node.index, NumberLiteral)
                and float(node.index.val).is_integer()
            ):
                return f"{receiver}[{int(node.index.val)}]"
            return f"{receiver}[int({self.expression(node.index)})]"
        if isinstance(node, PrimaryMember):
            receiver = _receiver(self.expression(node.left))
            index = {"x": 0, "y": 1, "z": 2}.get(node.member.name)
            if index is not None:
                return f"{receiver}[{index}]"
            return f"{receiver}.{node.member.name}"

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
    translator.source_path = path
    translator.base_dir = path.resolve().parent
    return translator.translate(nodes)
