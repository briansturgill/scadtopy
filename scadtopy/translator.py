"""Translate OpenSCAD AST nodes into Python code using Piecad."""

from __future__ import annotations

import ast
import builtins
import keyword
import math
import pickle
import re
import sys
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable, Iterator

from openscad_parser.ast import getASTfromFile
from openscad_parser.ast.nodes import (
    AdditionOp,
    Argument,
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

_FUNCTION_BUILTINS = (
    _PIECAD_EXPORTS
    | frozenset(dir(builtins))
    | frozenset(_MATH_FUNCTIONS)
    | frozenset(_BUILTIN_HELPERS)
    | frozenset(
        {
            "is_undef",
            "is_list",
            "is_string",
            "is_bool",
            "is_num",
            "is_function",
            "version",
            "norm",
            "cross",
        }
    )
)


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


def _is_user_special_variable(name: str) -> bool:
    return (
        name.startswith("$")
        and name not in _SEGMENT_VARIABLES
        and name not in _UNSUPPORTED_SPECIAL_VARIABLES
        and name != "$children"
    )


def _python_module_name(name: str) -> str:
    base = name.lstrip("_")
    reserved = [
        module for module in sys.stdlib_module_names if module.lstrip("_") == base
    ]
    if not reserved:
        return name
    prefix_length = max(len(module) - len(base) for module in reserved) + 1
    return "_" * prefix_length + name


def _positional_after_named(arguments: list[object]) -> bool:
    seen_named = False
    for argument in arguments:
        if isinstance(argument, NamedArgument):
            seen_named = True
        elif isinstance(argument, PositionalArgument) and seen_named:
            return True
    return False


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


def _has_dynamic_function_call(node: object, known_functions: set[str]) -> bool:
    if isinstance(node, PrimaryCall):
        if not isinstance(node.left, Identifier):
            return True
        if node.left.name not in known_functions and (
            node.left.name not in _FUNCTION_BUILTINS
            or any(
                isinstance(argument, NamedArgument)
                and argument.name.name in _SEGMENT_VARIABLES
                for argument in node.arguments
            )
        ):
            return True
    if isinstance(node, (list, tuple)):
        return any(_has_dynamic_function_call(item, known_functions) for item in node)
    if hasattr(node, "__dict__"):
        return any(
            _has_dynamic_function_call(value, known_functions)
            for key, value in vars(node).items()
            if key != "position"
        )
    return False


def _uncommented_expression(node: object) -> object:
    while isinstance(node, CommentedExpr):
        node = node.expr
    return node


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
                setattr(node, key, _uncommented_expression(value))
            else:
                _unwrap_commented_names(value)


def _rename_python_keywords(nodes: list[object]) -> None:
    user_import = any(
        isinstance(node, ModuleDeclaration) and node.name.name == "import"
        for node in nodes
    )

    seen: set[int] = set()

    def rename(node: object) -> None:
        if id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, Identifier):
            if keyword.iskeyword(node.name.lstrip("_")):
                node.name = f"_{node.name}"
        elif isinstance(node, (list, tuple)):
            for item in node:
                rename(item)
        elif hasattr(node, "__dict__"):
            for key, value in vars(node).items():
                if key == "position":
                    continue
                if (
                    key == "name"
                    and isinstance(node, ModularCall)
                    and isinstance(value, Identifier)
                    and value.name == "import"
                    and not user_import
                ):
                    continue
                rename(value)

    rename(nodes)


def _all_module_names(nodes: Iterable[object]) -> set[str]:
    names: set[str] = set()
    for node in nodes:
        if isinstance(node, ModuleDeclaration):
            names.add(node.name.name)
            names |= _all_module_names(node.children)
    return names


def _duplicate_parameter_names(nodes: Iterable[object]) -> tuple[set[str], set[str]]:
    functions: set[str] = set()
    modules: set[str] = set()
    for node in nodes:
        if isinstance(node, (FunctionDeclaration, ModuleDeclaration)):
            names = [parameter.name.name for parameter in node.parameters]
            if len(names) != len(set(names)):
                (functions if isinstance(node, FunctionDeclaration) else modules).add(
                    node.name.name
                )
            if isinstance(node, ModuleDeclaration):
                nested_functions, nested_modules = _duplicate_parameter_names(
                    node.children
                )
                functions.update(nested_functions)
                modules.update(nested_modules)
    return functions, modules


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
_DeclarationKey = tuple[Path | None, str, str]


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


def _segment_requirements(
    nodes: list[object], source_path: Path | None, base_dir: Path | None
) -> tuple[
    dict[str, set[str]],
    dict[str, set[str]],
    dict[tuple[str, str], set[str]],
]:
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
                    used = pickle.loads(pickle.dumps(getASTfromFile(str(path)) or []))
                    _unwrap_commented_names(used)
                    _rename_python_keywords(used)
                    load(path, used, path.parent)

    load(root, nodes, base_dir)
    declarations: dict[
        _DeclarationKey, list[ModuleDeclaration | FunctionDeclaration]
    ] = {}

    def collect(key: Path | None, contents: Iterable[object]) -> None:
        for node in contents:
            if isinstance(node, ModuleDeclaration):
                declarations.setdefault((key, "module", node.name.name), []).append(
                    node
                )
                collect(key, node.children)
            elif isinstance(node, FunctionDeclaration):
                declarations.setdefault((key, "function", node.name.name), []).append(
                    node
                )

    for key, (contents, _, _) in files.items():
        collect(key, contents)

    visible_cache: dict[Path | None, dict[tuple[str, str], _DeclarationKey]] = {}

    def visible_names(
        key: Path | None, visiting: frozenset[Path | None]
    ) -> dict[tuple[str, str], _DeclarationKey]:
        if key in visiting:
            return {}
        if key in visible_cache:
            return visible_cache[key]
        names: dict[tuple[str, str], _DeclarationKey] = {}
        for imported in files[key][2]:
            names.update(visible_names(imported, visiting | {key}))
        names.update(
            ((kind, name), (owner, kind, name))
            for owner, kind, name in declarations
            if owner == key
        )
        visible_cache[key] = names
        return names

    visible = {key: visible_names(key, frozenset()) for key in files}

    requirements = {key: set() for key in declarations}
    calls: dict[_DeclarationKey, list[tuple[_DeclarationKey, set[str]]]] = {
        key: [] for key in declarations
    }
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
            elif isinstance(node, (ModularFor, ModularIntersectionFor, ListCompFor)):
                loop_bound = bound
                for assignment in node.assignments:
                    visit(assignment.expr, loop_bound)
                    if assignment.name.name in _SEGMENT_VARIABLES:
                        loop_bound = loop_bound | {assignment.name.name}
                visit(node.body, loop_bound)
            elif isinstance(node, (LetOp, ModularLet, ListCompLet)):
                let_bound = bound
                for assignment in node.assignments:
                    visit(assignment.expr, let_bound)
                    if assignment.name.name in _SEGMENT_VARIABLES:
                        let_bound = let_bound | {assignment.name.name}
                visit(
                    node.children if isinstance(node, ModularLet) else node.body,
                    let_bound,
                )
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
                elif ("module", name) in visible[key[0]]:
                    calls[key].append(
                        (visible[key[0]][("module", name)], provided | bound)
                    )
                visit(node.arguments, bound)
                child_bound = bound
                if name in {
                    "linear_extrude",
                    "offset",
                    "rotate_extrude",
                    "color",
                    *_PIECAD_NAMES,
                }:
                    child_bound = bound | provided
                visit(node.children, child_bound)
            elif isinstance(node, PrimaryCall) and isinstance(node.left, Identifier):
                provided = {
                    arg.name.name
                    for arg in node.arguments
                    if isinstance(arg, NamedArgument)
                    and arg.name.name in _SEGMENT_VARIABLES
                }
                target = visible[key[0]].get(("function", node.left.name))
                if target is not None:
                    calls[key].append((target, provided | bound))
                elif provided or node.left.name not in _FUNCTION_BUILTINS:
                    requirements[key].update(set(_SEGMENT_VARIABLES) - bound)
                visit(node.arguments, bound)
            elif isinstance(node, PrimaryCall):
                requirements[key].update(set(_SEGMENT_VARIABLES) - bound)
                visit(node.left, bound)
                visit(node.arguments, bound)
            elif isinstance(node, (list, tuple)):
                for item in node:
                    visit(item, bound)
            elif hasattr(node, "__dict__"):
                for attribute, value in vars(node).items():
                    if attribute != "position":
                        visit(value, bound)

        for module in modules:
            explicit = frozenset(
                parameter.name.name
                for parameter in module.parameters
                if parameter.name.name in _SEGMENT_VARIABLES
            )
            if isinstance(module, FunctionDeclaration):
                for parameter in module.parameters:
                    visit(parameter.default)
                visit(module.expr, explicit)
                continue
            # Module assignments are emitted before the geometry expressions.
            assigned = explicit
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
    results = [
        {
            name: requirements[target]
            for (kind, name), target in visible[root].items()
            if kind == declaration_kind
        }
        for declaration_kind in ("module", "function")
    ]
    explicit_parameters = {
        (kind, name): {
            parameter.name.name
            for declaration in declarations[target]
            for parameter in declaration.parameters
            if parameter.name.name in _SEGMENT_VARIABLES
        }
        for (kind, name), target in visible[root].items()
    }
    return results[0], results[1], explicit_parameters


_CENTER_MODULES = frozenset({"cube", "square", "cylinder", "linear_extrude"})

_EXPORT_COMMENT = re.compile(r"\s*export\s+(\S+\.\w+)\s*$", re.I)

_PIECAD_LOAD_EXTENSIONS = {
    "3mf",
    "dxf",
    "glb",
    "gltf",
    "obj",
    "off",
    "ply",
    "stl",
    "stl_ascii",
    "svg",
}
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


def _literal_numeric_vector(
    expression: object, lengths: tuple[int, ...]
) -> list[float] | None:
    if (
        not isinstance(expression, ListComprehension)
        or len(expression.elements) not in lengths
    ):
        return None
    values = []
    for element in expression.elements:
        if isinstance(element, NumberLiteral):
            values.append(element.val)
        elif isinstance(element, UnaryMinusOp) and isinstance(
            element.expr, NumberLiteral
        ):
            values.append(-element.expr.val)
        else:
            return None
    return values


class _Translator:
    def __init__(self) -> None:
        self.output_stem = ""
        self.source_path: Path | None = None
        self.base_dir: Path | None = None
        self.function_names: set[str] = set()
        self.duplicate_functions: set[str] = set()
        self.duplicate_modules: set[str] = set()
        self.uses_vec2 = False
        self.helpers: set[str] = set()
        self.module_names: set[str] = set()
        self.local_module_names: set[str] = set()
        self.modules_with_nested_defaults: set[str] = set()
        self.imported_identifiers: set[str] = set()
        self.unknown_imports = False
        self.has_use = False
        self.user_identifiers: set[str] = set()
        self.segment_modules: dict[str, set[str]] = {}
        self.segment_functions: dict[str, set[str]] = {}
        self.explicit_segment_parameters: dict[tuple[str, str], set[str]] = {}
        self.geometry_modules: dict[str, ModuleDeclaration] = {}
        self.shadowing_names: set[str] = set()
        self.children_modules: set[str] = set()
        self.uses_children_helper = False
        self.uses_boolean_helper = False
        self.boolean_helper = "_scadtopy_boolean"
        self.uses_linear_extrude_helper = False
        self.uses_text_helper = False
        self.uses_color_group_helper = False
        self.color_group_helper = "_scadtopy_color_group"
        self.uses_color_helper = False
        self.color_helper = "_scadtopy_color"
        self.uses_mesh_import_helper = False
        self.mesh_import_helper = "_scadtopy_load_mesh"
        self.uses_dxf_import_helper = False
        self.dxf_import_helper = "_scadtopy_load_dxf"
        self.uses_translate_helper = False
        self.translate_helper = "_scadtopy_translate"
        self.uses_scale_helper = False
        self.scale_helper = "_scadtopy_scale"
        self.uses_rotate_helper = False
        self.rotate_helper = "_scadtopy_rotate"
        self.uses_mirror_helper = False
        self.mirror_helper = "_scadtopy_mirror"
        self.uses_add_helper = False
        self.add_helper = "_scadtopy_add"
        self.uses_subtract_helper = False
        self.subtract_helper = "_scadtopy_subtract"
        self.uses_multiply_helper = False
        self.multiply_helper = "_scadtopy_multiply"
        self.uses_negate_helper = False
        self.negate_helper = "_scadtopy_negate"
        self.uses_divide_helper = False
        self.divide_helper = "_scadtopy_divide"
        self.uses_version_helper = False
        self.version_helper = "_scadtopy_version"
        self.uses_cone_helper = False
        self.cone_helper = "_scadtopy_cone"
        self.projected_transform_helper = "_scadtopy_projected_transform"
        self.uses_resize_helper = False
        self.uses_segments_helper = False
        self.uses_loop_iter_helper = False
        self.loop_iter_helper = "_scadtopy_loop_iter"
        self.uses_special_call_helper = False
        self.special_call_helper = "_scadtopy_call_special"
        self.segment_unset = "_scadtopy_unset"
        self.uses_duplicate_parameters = False
        self.uses_radius_choice_helper = False
        self.radius_choice_helper = "_scadtopy_choose_radius"
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
        nodes = pickle.loads(pickle.dumps(list(nodes)))
        _unwrap_commented_names(nodes)
        _rename_python_keywords(nodes)
        self.validate_required_arguments(nodes)
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
        self.has_use = any(isinstance(node, UseStatement) for node in nodes)
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
        self.uses_boolean_helper = False
        self.uses_linear_extrude_helper = False
        self.uses_text_helper = False
        self.uses_color_group_helper = False
        self.uses_color_helper = False
        self.uses_mesh_import_helper = False
        self.uses_dxf_import_helper = False
        self.uses_translate_helper = False
        self.uses_scale_helper = False
        self.uses_rotate_helper = False
        self.uses_mirror_helper = False
        self.uses_add_helper = False
        self.uses_subtract_helper = False
        self.uses_multiply_helper = False
        self.uses_negate_helper = False
        self.uses_divide_helper = False
        self.uses_version_helper = False
        self.uses_cone_helper = False
        self.uses_resize_helper = False
        self.uses_segments_helper = False
        self.uses_loop_iter_helper = False
        self.uses_special_call_helper = False
        self.uses_duplicate_parameters = False
        self.uses_radius_choice_helper = False
        self.uses_vec2 = False
        self.helpers = set()
        self.function_names = {
            node.name.name for node in nodes if isinstance(node, FunctionDeclaration)
        }
        self.duplicate_functions, self.duplicate_modules = _duplicate_parameter_names(
            nodes
        )
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
        if self.has_use:
            self.geometry_modules.clear()
        for node in nodes:
            if isinstance(node, UseStatement):
                self.load_used_names(node)
        self.boolean_helper = "_scadtopy_boolean"
        while self.boolean_helper in self.user_identifiers | self.imported_identifiers:
            self.boolean_helper += "_"
        self.color_group_helper = "_scadtopy_color_group"
        while self.color_group_helper in (
            self.user_identifiers | self.imported_identifiers
        ):
            self.color_group_helper += "_"
        self.color_helper = "_scadtopy_color"
        while self.color_helper in self.user_identifiers | self.imported_identifiers:
            self.color_helper += "_"
        self.cone_helper = "_scadtopy_cone"
        while self.cone_helper in self.user_identifiers | self.imported_identifiers:
            self.cone_helper += "_"
        self.mesh_import_helper = "_scadtopy_load_mesh"
        while (
            self.mesh_import_helper in self.user_identifiers | self.imported_identifiers
        ):
            self.mesh_import_helper += "_"
        self.dxf_import_helper = "_scadtopy_load_dxf"
        while (
            self.dxf_import_helper in self.user_identifiers | self.imported_identifiers
        ):
            self.dxf_import_helper += "_"
        self.translate_helper = "_scadtopy_translate"
        while (
            self.translate_helper in self.user_identifiers | self.imported_identifiers
        ):
            self.translate_helper += "_"
        self.scale_helper = "_scadtopy_scale"
        while self.scale_helper in self.user_identifiers | self.imported_identifiers:
            self.scale_helper += "_"
        self.rotate_helper = "_scadtopy_rotate"
        while self.rotate_helper in self.user_identifiers | self.imported_identifiers:
            self.rotate_helper += "_"
        self.mirror_helper = "_scadtopy_mirror"
        while self.mirror_helper in self.user_identifiers | self.imported_identifiers:
            self.mirror_helper += "_"
        self.add_helper = "_scadtopy_add"
        while self.add_helper in self.user_identifiers | self.imported_identifiers:
            self.add_helper += "_"
        self.subtract_helper = "_scadtopy_subtract"
        while self.subtract_helper in self.user_identifiers | self.imported_identifiers:
            self.subtract_helper += "_"
        self.multiply_helper = "_scadtopy_multiply"
        while self.multiply_helper in self.user_identifiers | self.imported_identifiers:
            self.multiply_helper += "_"
        self.negate_helper = "_scadtopy_negate"
        while self.negate_helper in self.user_identifiers | self.imported_identifiers:
            self.negate_helper += "_"
        self.divide_helper = "_scadtopy_divide"
        while self.divide_helper in self.user_identifiers | self.imported_identifiers:
            self.divide_helper += "_"
        self.version_helper = "_scadtopy_version"
        while self.version_helper in self.user_identifiers | self.imported_identifiers:
            self.version_helper += "_"
        self.projected_transform_helper = "_scadtopy_projected_transform"
        while self.projected_transform_helper in (
            self.user_identifiers | self.imported_identifiers
        ):
            self.projected_transform_helper += "_"
        self.loop_iter_helper = "_scadtopy_loop_iter"
        while (
            self.loop_iter_helper in self.user_identifiers | self.imported_identifiers
        ):
            self.loop_iter_helper += "_"
        self.special_call_helper = "_scadtopy_call_special"
        while (
            self.special_call_helper
            in self.user_identifiers | self.imported_identifiers
        ):
            self.special_call_helper += "_"
        self.radius_choice_helper = "_scadtopy_choose_radius"
        while self.radius_choice_helper in (
            self.user_identifiers | self.imported_identifiers
        ):
            self.radius_choice_helper += "_"
        (
            self.segment_modules,
            self.segment_functions,
            self.explicit_segment_parameters,
        ) = _segment_requirements(nodes, self.source_path, self.base_dir)
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
            "_scadtopy_default_fs = Config.get_default_min_edge_length()",
        ]
        saw_model = False
        part_count = 0
        exports: list[tuple[int, str]] = []
        last_statement_is_model = False

        for index, node in enumerate(nodes):
            if index == hoist_start:
                statements.extend(hoisted_declarations)
            if isinstance(node, Assignment):
                if _is_user_special_variable(node.name.name):
                    self.note_unsupported(
                        node, f"special variable {node.name.name} assignment is ignored"
                    )
                    last_statement_is_model = False
                    continue
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
                        model = self.normalize_absent(model)
                        statements.append(f"_parts.append({model})")
                        part_count += 1
                        last_statement_is_model = True
                    else:
                        last_statement_is_model = False
                else:
                    saw_model = True
                    model = self.module_call(node)
                    if model != "None":
                        model = self.normalize_absent(model)
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
            else:
                self.note_unsupported(node, f"{_node_name(node)} is not translated")
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
        if self.uses_version_helper:
            prefix += f"def {self.version_helper}(*args):\n" "    return [2021, 1]\n\n"
        if self.uses_boolean_helper:
            prefix += (
                f"def {self.boolean_helper}(operation, absent, *children):\n"
                "    import builtins\n"
                "    present = [child for child in children if child is not ...]\n"
                "    if not present:\n"
                "        return absent\n"
                "    geometry = [child for child in present if child is not None]\n"
                "    if builtins.len(geometry) > 1 and builtins.any(\n"
                "        builtins.type(child) is not builtins.type(geometry[0])\n"
                "        for child in geometry[1:]\n"
                "    ):\n"
                "        raise ValidationError('Mixed types in parameter: objs.')\n"
                "    if operation is difference and present[0] is None:\n"
                "        return None\n"
                "    if operation is intersect and builtins.len(geometry) != builtins.len(present):\n"
                "        return None\n"
                "    if builtins.len(geometry) == 1 and operation is not hull and "
                "builtins.type(geometry[0]) in (Obj2d, Obj3d):\n"
                "        return geometry[0]\n"
                "    return operation(*geometry) if geometry else None\n\n"
            )
        if self.uses_loop_iter_helper:
            prefix += (
                f"def {self.loop_iter_helper}(value):\n"
                "    import builtins\n"
                "    return value if builtins.isinstance(value, "
                "(builtins.list, builtins.tuple, builtins.range, builtins.str)) "
                "else [value]\n\n"
            )
        if self.uses_special_call_helper:
            prefix += (
                f"def {self.special_call_helper}(function, arguments, settings):\n"
                "    import inspect\n"
                "    names = ('$fn', '$fa', '$fs')\n"
                "    settings = dict(zip(names, settings))\n"
                "    original_parameters = getattr(function, '_scadtopy_parameter_names', None)\n"
                "    if original_parameters is not None:\n"
                "        supplied, named_seen, position, overridden = {}, set(), 0, set()\n"
                "        for name, value in arguments:\n"
                "            if name in names:\n"
                "                settings[name] = int(value) if name == '$fn' else value\n"
                "                overridden.add(name)\n"
                "                parameter = '_scadtopy_special_' + name[1:]\n"
                "                if parameter in original_parameters:\n"
                "                    named_seen.add(parameter)\n"
                "            elif name is None:\n"
                "                while position < len(original_parameters) and original_parameters[position] in named_seen:\n"
                "                    position += 1\n"
                "                if position >= len(original_parameters):\n"
                "                    import warnings\n"
                "                    warnings.warn('Too many unnamed arguments supplied', stacklevel=2)\n"
                "                    continue\n"
                "                supplied[original_parameters[position]] = value\n"
                "                position += 1\n"
                "            else:\n"
                "                if name in named_seen:\n"
                "                    import warnings\n"
                "                    warnings.warn(f'argument {name!r} supplied more than once', stacklevel=2)\n"
                "                elif name in supplied:\n"
                "                    import warnings\n"
                "                    warnings.warn(f'argument {name!r} overrides positional argument', stacklevel=2)\n"
                "                named_seen.add(name)\n"
                "                supplied[name] = value\n"
                "        parameters = inspect.signature(function).parameters\n"
                "        explicit = getattr(function, '_scadtopy_explicit_segments', ())\n"
                "        for name, value in settings.items():\n"
                "            parameter = '_scadtopy_special_' + name[1:]\n"
                "            if parameter in supplied and name in overridden:\n"
                "                import warnings\n"
                "                warnings.warn(f'argument {name!r} overrides positional argument', stacklevel=2)\n"
                "            if parameter in parameters and (parameter not in supplied or name in overridden) and (name not in explicit or name in overridden):\n"
                "                supplied[parameter] = value\n"
                "        return function(**supplied)\n"
                "    overridden = set()\n"
                "    positional, named = [], {}\n"
                "    for name, value in arguments:\n"
                "        if name in names:\n"
                "            settings[name] = int(value) if name == '$fn' else value\n"
                "            overridden.add(name)\n"
                "        elif name is None:\n"
                "            positional.append(value)\n"
                "        else:\n"
                "            if name in named:\n"
                "                raise TypeError(f'duplicate argument: {name}')\n"
                "            named[name] = value\n"
                "    signature = inspect.signature(function)\n"
                "    parameters = signature.parameters\n"
                "    bound = signature.bind_partial(*positional, **named).arguments\n"
                "    explicit = getattr(function, '_scadtopy_explicit_segments', ())\n"
                "    for name, value in settings.items():\n"
                "        parameter = '_scadtopy_special_' + name[1:]\n"
                "        if parameter in bound and name in overridden:\n"
                "            raise TypeError(f'multiple values for {name}')\n"
                "        if (parameter in parameters and parameter not in bound and\n"
                "                (name not in explicit or name in overridden)):\n"
                "            named[parameter] = value\n"
                "    return function(*positional, **named)\n\n"
            )
        if self.uses_add_helper:
            prefix += (
                f"def {self.add_helper}(left, right, _component=False):\n"
                "    import builtins\n"
                "    import numbers\n"
                "    if builtins.isinstance(left, (builtins.list, builtins.tuple)) and "
                "builtins.isinstance(right, (builtins.list, builtins.tuple)):\n"
                f"        return [{self.add_helper}(a, b, True) for a, b in builtins.zip(left, right)]\n"
                "    if _component and not (builtins.isinstance(left, numbers.Real) and "
                "not builtins.isinstance(left, builtins.bool) and "
                "builtins.isinstance(right, numbers.Real) and "
                "not builtins.isinstance(right, builtins.bool)):\n"
                "        raise builtins.TypeError('vector addition requires numeric or vector components')\n"
                "    return left + right\n\n"
            )
        if self.uses_subtract_helper:
            prefix += (
                f"def {self.subtract_helper}(left, right, _component=False):\n"
                "    import builtins\n"
                "    import numbers\n"
                "    if builtins.isinstance(left, (builtins.list, builtins.tuple)) and "
                "builtins.isinstance(right, (builtins.list, builtins.tuple)):\n"
                f"        return [{self.subtract_helper}(a, b, True) for a, b in builtins.zip(left, right)]\n"
                "    if _component and not (builtins.isinstance(left, numbers.Real) and "
                "not builtins.isinstance(left, builtins.bool) and "
                "builtins.isinstance(right, numbers.Real) and "
                "not builtins.isinstance(right, builtins.bool)):\n"
                "        raise builtins.TypeError('vector subtraction requires numeric or vector components')\n"
                "    return left - right\n\n"
            )
        if self.uses_negate_helper:
            prefix += (
                f"def {self.negate_helper}(value, _component=False):\n"
                "    import builtins\n"
                "    import numbers\n"
                "    if builtins.isinstance(value, (builtins.list, builtins.tuple)):\n"
                f"        return [{self.negate_helper}(item, True) for item in value]\n"
                "    if _component and (not builtins.isinstance(value, numbers.Real) or "
                "builtins.isinstance(value, builtins.bool)):\n"
                "        raise builtins.TypeError('vector negation requires numeric or vector components')\n"
                "    return -value\n\n"
            )
        if self.uses_multiply_helper:
            prefix += (
                f"def {self.multiply_helper}(left, right):\n"
                "    import builtins\n"
                "    import numbers\n"
                "    sequence = (builtins.list, builtins.tuple)\n"
                "    def numeric(value):\n"
                "        return builtins.isinstance(value, numbers.Real) and not builtins.isinstance(value, builtins.bool)\n"
                "    def scale(value, factor):\n"
                "        if builtins.isinstance(value, sequence):\n"
                "            return [scale(item, factor) for item in value]\n"
                "        if not numeric(value):\n"
                "            raise builtins.TypeError('vector multiplication requires numeric components')\n"
                "        return value * factor\n"
                "    def shape(value):\n"
                "        if not value or builtins.all(numeric(item) for item in value):\n"
                "            return (builtins.len(value),)\n"
                "        if not builtins.all(builtins.isinstance(row, sequence) for row in value):\n"
                "            raise builtins.TypeError('matrix multiplication requires numeric vectors or rectangular matrices')\n"
                "        width = builtins.len(value[0])\n"
                "        if not builtins.all(builtins.len(row) == width and builtins.all(numeric(item) for item in row) for row in value):\n"
                "            raise builtins.TypeError('matrix multiplication requires numeric vectors or rectangular matrices')\n"
                "        return (builtins.len(value), width)\n"
                "    left_vector = builtins.isinstance(left, sequence)\n"
                "    right_vector = builtins.isinstance(right, sequence)\n"
                "    if not left_vector and not right_vector:\n"
                "        return left * right\n"
                "    if not left_vector or not right_vector:\n"
                "        value, factor = (left, right) if left_vector else (right, left)\n"
                "        if not numeric(factor):\n"
                "            raise builtins.TypeError('vector multiplication requires a numeric scalar')\n"
                "        return scale(value, factor)\n"
                "    left_shape, right_shape = shape(left), shape(right)\n"
                "    if left_shape[-1] != right_shape[0]:\n"
                "        raise builtins.ValueError('vector or matrix multiplication dimensions do not match')\n"
                "    def dot(a, b):\n"
                "        return builtins.sum(x * y for x, y in builtins.zip(a, b))\n"
                "    if builtins.len(left_shape) == 1:\n"
                "        if builtins.len(right_shape) == 1:\n"
                "            return dot(left, right)\n"
                "        return [dot(left, column) for column in builtins.zip(*right)]\n"
                "    if builtins.len(right_shape) == 1:\n"
                "        return [dot(row, right) for row in left]\n"
                "    columns = builtins.list(builtins.zip(*right))\n"
                "    return [[dot(row, column) for column in columns] for row in left]\n\n"
            )
        if self.uses_divide_helper:
            prefix += (
                f"def {self.divide_helper}(left, right, _component=False):\n"
                "    import builtins\n"
                "    import numbers\n"
                "    if builtins.isinstance(right, (builtins.list, builtins.tuple)):\n"
                "        if not builtins.isinstance(left, numbers.Real) or "
                "builtins.isinstance(left, builtins.bool):\n"
                "            raise builtins.TypeError('vector division requires a numeric dividend')\n"
                f"        return [{self.divide_helper}(left, value, True) for value in right]\n"
                "    if builtins.isinstance(left, (builtins.list, builtins.tuple)):\n"
                "        if not builtins.isinstance(right, numbers.Real) or "
                "builtins.isinstance(right, builtins.bool):\n"
                "            raise builtins.TypeError('vector division requires a numeric divisor')\n"
                f"        return [{self.divide_helper}(value, right, True) for value in left]\n"
                "    if _component and (not builtins.isinstance(left, numbers.Real) or "
                "builtins.isinstance(left, builtins.bool) or "
                "not builtins.isinstance(right, numbers.Real) or "
                "builtins.isinstance(right, builtins.bool)):\n"
                "        raise builtins.TypeError('vector division requires numeric or vector components')\n"
                "    return left / right\n\n"
            )
        if self.uses_mesh_import_helper:
            prefix += (
                f"def {self.mesh_import_helper}(filename, file_type):\n"
                "    import builtins\n"
                "    import trimesh\n"
                "    from piecad import obj3d_from_vertices_and_faces\n"
                "    with builtins.open(filename, 'rb') as stream:\n"
                "        mesh = trimesh.exchange.load.load(stream, file_type=file_type, "
                "force='mesh', process=True, validate=False)\n"
                "    return obj3d_from_vertices_and_faces(mesh.vertices, mesh.faces)\n\n"
            )
        if self.uses_dxf_import_helper:
            prefix += (
                f"def {self.dxf_import_helper}(filename):\n"
                "    import builtins\n"
                "    from piecad import path\n"
                "    return builtins.type(path()).from_dxf(filename).to_obj2d()\n\n"
            )
        if self.expression_definitions:
            prefix += "\n\n".join(self.expression_definitions) + "\n\n"
        segment_defaults = "".join(
            f"    if {name} is {self.segment_unset}:\n"
            f"        {name} = _scadtopy_default_{name}\n"
            for name in ("fn", "fa", "fs")
        )
        if (
            self.uses_segments_helper
            or self.uses_linear_extrude_helper
            or self.uses_duplicate_parameters
        ):
            prefix += f"{self.segment_unset} = object()\n\n"
        if self.uses_segments_helper or self.uses_linear_extrude_helper:
            prefix += (
                f"def _scadtopy_calculate_segments(radius, fn={self.segment_unset}, "
                f"fa={self.segment_unset}, fs={self.segment_unset}):\n"
                f"{segment_defaults}"
                "    if fn > 0:\n"
                "        return max(fn, 3)\n"
                "    return Config.calc_segs(radius, fa, fs)\n\n"
            )
        if self.uses_radius_choice_helper:
            prefix += (
                f"def {self.radius_choice_helper}(radius, diameter):\n"
                "    import builtins\n"
                "    import numbers\n"
                "    def numeric(value):\n"
                "        return builtins.isinstance(value, numbers.Real) and "
                "not builtins.isinstance(value, builtins.bool)\n"
                "    if numeric(diameter):\n"
                "        if numeric(radius):\n"
                "            import warnings\n"
                "            warnings.warn('Ignoring radius because diameter is defined', stacklevel=2)\n"
                "        return diameter / 2\n"
                "    return radius if numeric(radius) else 1\n\n"
            )
        if self.uses_cone_helper:
            prefix += (
                f"def {self.cone_helper}(height, r1, r2, center, fn, fa, fs):\n"
                "    from piecad import cone\n"
                "    segments = _scadtopy_calculate_segments(max(abs(r1), abs(r2)), fn, fa, fs)\n"
                "    if r1 == 0 and r2 > 0:\n"
                "        result = cone(height, r2, r1, segments=segments, center=center).rotate([180, 0, 0])\n"
                "        return result if center else result.translate([0, 0, height])\n"
                "    return cone(height, r1, r2, segments=segments, center=center)\n\n"
            )
        if self.uses_translate_helper:
            prefix += (
                f"def {self.translate_helper}(obj, vector):\n"
                "    import builtins\n"
                "    if obj is None or obj is ...:\n"
                "        return None\n"
                "    if builtins.isinstance(obj, Obj2d):\n"
                "        vector = vector[:2]\n"
                "    elif builtins.isinstance(obj, Obj3d) and builtins.isinstance(vector, "
                "(builtins.list, builtins.tuple)) and builtins.len(vector) == 2:\n"
                "        vector = [*vector, 0]\n"
                "    return obj.translate(vector)\n\n"
            )
        if self.uses_scale_helper:
            prefix += (
                f"def {self.scale_helper}(obj, factors):\n"
                "    import builtins\n"
                "    import numbers\n"
                "    if obj is None or obj is ...:\n"
                "        return None\n"
                "    if builtins.isinstance(factors, numbers.Real) and "
                "not builtins.isinstance(factors, builtins.bool):\n"
                "        factors = [factors] * (2 if builtins.isinstance(obj, Obj2d) else 3)\n"
                "    elif builtins.isinstance(factors, (builtins.list, builtins.tuple)):\n"
                "        if builtins.isinstance(obj, Obj2d) and builtins.len(factors) == 3:\n"
                "            factors = factors[:2]\n"
                "        elif builtins.isinstance(obj, Obj3d) and builtins.len(factors) == 2:\n"
                "            factors = [*factors, 1]\n"
                "    return obj.scale(factors)\n\n"
            )
        if self.uses_rotate_helper or self.uses_mirror_helper:
            prefix += (
                f"def {self.projected_transform_helper}(obj, a, b, c, d, operation):\n"
                "    if a * d - b * c == 0:\n"
                "        import warnings\n"
                "        from piecad import difference\n"
                "        warnings.warn(f'{operation}() removes 2D geometry with a singular transform', stacklevel=2)\n"
                "        return difference(obj, obj)\n"
                "    return obj.transform(((a, b, 0), (c, d, 0)))\n\n"
            )
        if self.uses_rotate_helper:
            prefix += (
                f"def {self.rotate_helper}(obj, angle, axis=None):\n"
                "    import builtins\n"
                "    import math\n"
                "    from piecad import Obj2d, Obj3d\n"
                "    if obj is None or obj is ...:\n"
                "        return obj\n"
                "    if not builtins.isinstance(obj, (Obj2d, Obj3d)):\n"
                "        raise builtins.TypeError('rotate() requires an Obj2d or Obj3d')\n"
                "    def trig(degrees):\n"
                "        quarter = degrees % 360\n"
                "        if quarter in (0, 90, 180, 270):\n"
                "            return {0: (0, 1), 90: (1, 0), 180: (0, -1), 270: (-1, 0)}[quarter]\n"
                "        radians = math.radians(degrees)\n"
                "        return math.sin(radians), math.cos(radians)\n"
                "    if builtins.isinstance(angle, (builtins.list, builtins.tuple)):\n"
                "        if builtins.len(angle) > 3:\n"
                "            raise builtins.ValueError('rotate() angle vector must have at most 3 components')\n"
                "        angles = builtins.list(angle) + [0] * (3 - builtins.len(angle))\n"
                "        if builtins.isinstance(obj, Obj3d):\n"
                "            return obj.rotate(angles)\n"
                "        if angles[0] == 0 and angles[1] == 0:\n"
                "            return obj.rotate(angles[2])\n"
                "        sx, cx = trig(angles[0])\n"
                "        sy, cy = trig(angles[1])\n"
                "        sz, cz = trig(angles[2])\n"
                f"        return {self.projected_transform_helper}(obj, cy * cz, cz * sx * sy - cx * sz,\n"
                "            cy * sz, cx * cz + sx * sy * sz, 'rotate')\n"
                "    if axis is None:\n"
                "        return obj.rotate(angle if builtins.isinstance(obj, Obj2d) else [0, 0, angle])\n"
                "    if not builtins.isinstance(axis, (builtins.list, builtins.tuple)) or builtins.len(axis) not in (2, 3):\n"
                "        raise builtins.ValueError('rotate() axis must be a 2- or 3-vector')\n"
                "    x, y = axis[:2]\n"
                "    z = axis[2] if builtins.len(axis) == 3 else 0\n"
                "    magnitude = math.hypot(x, y, z)\n"
                "    if magnitude == 0:\n"
                "        return obj\n"
                "    if x == 0 and y == 0:\n"
                "        angle = angle if z > 0 else -angle\n"
                "        return obj.rotate(angle if builtins.isinstance(obj, Obj2d) else [0, 0, angle])\n"
                "    x, y, z = x / magnitude, y / magnitude, z / magnitude\n"
                "    sine, cosine = trig(angle)\n"
                "    t = 1 - cosine\n"
                "    matrix = ((t*x*x + cosine, t*x*y - sine*z, t*x*z + sine*y, 0),\n"
                "              (t*x*y + sine*z, t*y*y + cosine, t*y*z - sine*x, 0),\n"
                "              (t*x*z - sine*y, t*y*z + sine*x, t*z*z + cosine, 0))\n"
                "    if builtins.isinstance(obj, Obj2d):\n"
                f"        return {self.projected_transform_helper}(obj, matrix[0][0], matrix[0][1], matrix[1][0], matrix[1][1], 'rotate')\n"
                "    return obj.transform(matrix)\n\n"
            )
        if self.uses_mirror_helper:
            prefix += (
                f"def {self.mirror_helper}(obj, axis):\n"
                "    import builtins\n"
                "    import math\n"
                "    from piecad import Obj2d, Obj3d\n"
                "    if not builtins.isinstance(obj, (Obj2d, Obj3d)):\n"
                "        raise builtins.TypeError('mirror() requires an Obj2d or Obj3d')\n"
                "    if not builtins.isinstance(axis, (builtins.list, builtins.tuple)) or builtins.len(axis) not in (2, 3):\n"
                "        raise builtins.ValueError('mirror() axis must be a 2- or 3-vector')\n"
                "    if not builtins.all(builtins.type(value) in (builtins.int, builtins.float) and math.isfinite(value) for value in axis):\n"
                "        raise builtins.ValueError('mirror() axis must contain finite numbers')\n"
                "    x, y = axis[:2]\n"
                "    z = axis[2] if builtins.len(axis) == 3 else 0\n"
                "    scale = builtins.max(builtins.abs(x), builtins.abs(y), builtins.abs(z))\n"
                "    if scale == 0:\n"
                "        return obj\n"
                "    if x == 0 and y == 0 and builtins.isinstance(obj, Obj2d):\n"
                "        return obj\n"
                "    if builtins.sum(value != 0 for value in (x, y, z)) == 1:\n"
                "        flags = (x != 0, y != 0, z != 0)\n"
                "        return obj.mirror(flags[:2] if builtins.isinstance(obj, Obj2d) else flags)\n"
                "    x, y, z = x / scale, y / scale, z / scale\n"
                "    factor = 2 / (x*x + y*y + z*z)\n"
                "    matrix = ((1 - factor*x*x, -factor*x*y, -factor*x*z, 0),\n"
                "              (-factor*x*y, 1 - factor*y*y, -factor*y*z, 0),\n"
                "              (-factor*x*z, -factor*y*z, 1 - factor*z*z, 0))\n"
                "    if builtins.isinstance(obj, Obj2d):\n"
                f"        return {self.projected_transform_helper}(obj, matrix[0][0], matrix[0][1], matrix[1][0], matrix[1][1], 'mirror')\n"
                "    return obj.transform(matrix)\n\n"
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
                f"    return {self.boolean_helper}(union, ..., *picked)\n\n"
            )
        if self.uses_color_helper:
            prefix += (
                f"def {self.color_helper}(color, obj):\n"
                "    import builtins\n"
                "    if obj is None or obj is ...:\n"
                "        return None\n"
                "    if color is None:\n"
                "        return obj\n"
                "    if builtins.isinstance(color, (builtins.list, builtins.tuple)):\n"
                "        rgb = builtins.list(color[:3])\n"
                "        rgb.extend([1] * (3 - builtins.len(rgb)))\n"
                "        color = builtins.tuple(builtins.round(value * 255) for value in rgb)\n"
                "    elif builtins.isinstance(color, builtins.str):\n"
                "        if not color.startswith('#'):\n"
                "            color = color.lower()\n"
                "        elif builtins.len(color) in (4, 5, 9) and "
                "builtins.all(value in '0123456789abcdefABCDEF' for value in color[1:]):\n"
                "            color = ('#' + ''.join(value * 2 for value in color[1:4]) "
                "if builtins.len(color) in (4, 5) else color[:7])\n"
                "    return obj.color(color)\n\n"
            )
        if self.uses_color_group_helper:
            prefix += (
                f"def {self.color_group_helper}(color, *children):\n"
                "    import builtins\n"
                "    children = [child for child in children if child is not None and child is not ...]\n"
                "    if not children:\n"
                "        return None\n"
                "    if builtins.any(builtins.type(child) is not builtins.type(children[0]) "
                "for child in children):\n"
                "        raise builtins.ValueError('UNSUPPORTED: color() cannot combine 2D and 3D children')\n"
                "    geometry = union(*children) if builtins.len(children) > 1 else children[0]\n"
                f"    return {self.color_helper}(color, geometry)\n\n"
            )
        if self.uses_text_helper:
            prefix += (
                "def _scadtopy_text(content, size, halign, valign, font=None):\n"
                "    if font is None:\n"
                "        obj = text(size=size, text=content)\n"
                "    else:\n"
                "        import subprocess\n"
                "        import warnings\n"
                "        from piecad import _text, text_set_font\n"
                "        match = subprocess.run(['fc-match', '-f', '%{family}\\n%{style}\\n%{file}\\n', '--', font],\n"
                "                               check=True, capture_output=True, text=True)\n"
                "        family, style, path = match.stdout.splitlines()\n"
                "        fields = font.split(':')\n"
                "        requested = fields[0].strip()\n"
                "        requested_style = next((field[6:].strip() for field in fields[1:] "
                "if field.startswith('style=')), None)\n"
                "        family_matches = requested.casefold() in [name.strip().casefold() for name in family.split(',')]\n"
                "        style_matches = requested_style is None or requested_style.casefold() in "
                "[name.strip().casefold() for name in style.split(',')]\n"
                "        if not family_matches or not style_matches:\n"
                "            warnings.warn(f'Font {font!r} unavailable; using {family}:style={style}', stacklevel=2)\n"
                "        # Piecad exposes no public getter for its global font.\n"
                "        previous_font = _text._font\n"
                "        try:\n"
                "            text_set_font(path)\n"
                "            obj = text(size=size, text=content)\n"
                "        finally:\n"
                "            _text._font = previous_font\n"
                "    if obj.is_empty():\n"
                "        return obj\n"
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
        if name == "version_num":
            if count > 1:
                raise TranslationError("version_num() accepts at most one argument")
            if count:
                value = arguments[0]
                if isinstance(nodes[0], NamedArgument):
                    if nodes[0].name.name != "version":
                        raise TranslationError(
                            "version_num() accepts only the version argument"
                        )
                    value = value.split("=", 1)[1]
            else:
                self.uses_version_helper = True
                value = f"{self.version_helper}()"
            return f"(lambda v: v[0] * 10000 + v[1] * 100 + (v[2] if len(v) > 2 else 0))({value})"
        if name == "version":
            self.uses_version_helper = True
            values = [
                (
                    argument.split("=", 1)[1]
                    if isinstance(node, NamedArgument)
                    else argument
                )
                for node, argument in zip(nodes, arguments)
            ]
            return f"{self.version_helper}({', '.join(values)})"
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

    def validate_required_arguments(self, nodes: list[object]) -> None:
        def visit(
            node: object,
            declarations: dict[
                tuple[str, str], ModuleDeclaration | FunctionDeclaration
            ],
            bound: frozenset[str] = frozenset(),
        ) -> None:
            if isinstance(node, (list, tuple)):
                visible = declarations.copy()
                for item in node:
                    if isinstance(item, UseStatement):
                        for declaration in self.used_nodes(item):
                            if isinstance(
                                declaration, (ModuleDeclaration, FunctionDeclaration)
                            ):
                                kind = (
                                    "module"
                                    if isinstance(declaration, ModuleDeclaration)
                                    else "function"
                                )
                                visible[kind, declaration.name.name] = declaration
                for item in node:
                    if isinstance(item, (ModuleDeclaration, FunctionDeclaration)):
                        kind = (
                            "module"
                            if isinstance(item, ModuleDeclaration)
                            else "function"
                        )
                        visible[kind, item.name.name] = item
                bound = bound | {
                    item.name.name for item in node if isinstance(item, Assignment)
                }
                for item in node:
                    visit(item, visible, bound)
                return
            if isinstance(
                node, (ModuleDeclaration, FunctionDeclaration, FunctionLiteral)
            ):
                for parameter in node.parameters:
                    visit(parameter.default, declarations, bound)
                bound = bound | {parameter.name.name for parameter in node.parameters}
                body = (
                    node.children
                    if isinstance(node, ModuleDeclaration)
                    else node.body if isinstance(node, FunctionLiteral) else node.expr
                )
                visit(body, declarations, bound)
                return
            if isinstance(
                node,
                (
                    ModularFor,
                    ModularIntersectionFor,
                    ListCompFor,
                    LetOp,
                    ModularLet,
                    ListCompLet,
                ),
            ):
                for assignment in node.assignments:
                    visit(assignment.expr, declarations, bound)
                    bound = bound | {assignment.name.name}
                visit(
                    node.children if isinstance(node, ModularLet) else node.body,
                    declarations,
                    bound,
                )
                return
            if isinstance(node, ModularModifierDisable):
                return
            key = None
            arguments: list[object] = []
            if isinstance(node, ModularCall):
                key = ("module", node.name.name)
                arguments = node.arguments
            elif (
                isinstance(node, PrimaryCall)
                and isinstance(node.left, Identifier)
                and node.left.name not in bound
            ):
                key = ("function", node.left.name)
                arguments = node.arguments
            declaration = declarations.get(key) if key is not None else None
            if declaration is not None and key is not None:
                parameters = [
                    parameter
                    for parameter in declaration.parameters
                    if not _is_user_special_variable(parameter.name.name)
                ]
                supplied: set[str] = set()
                position = 0
                for argument in arguments:
                    if isinstance(argument, NamedArgument):
                        supplied.add(argument.name.name)
                    elif isinstance(argument, PositionalArgument):
                        while (
                            position < len(parameters)
                            and parameters[position].name.name in supplied
                        ):
                            position += 1
                        if position < len(parameters):
                            supplied.add(parameters[position].name.name)
                            position += 1
                missing = [
                    parameter.name.name
                    for parameter in parameters
                    if parameter.default is None
                    and parameter.name.name not in _SEGMENT_VARIABLES
                    and parameter.name.name not in supplied
                ]
                if missing:
                    source_path = getattr(
                        getattr(node, "position", None), "origin", self.source_path
                    )
                    source = f"{source_path}: " if source_path else ""
                    raise TranslationError(
                        f"{source}{key[1]}() missing required argument(s): "
                        f"{', '.join(dict.fromkeys(missing))}{_position(node)}"
                    )
            if hasattr(node, "__dict__"):
                for field, value in vars(node).items():
                    if field != "position":
                        visit(value, declarations, bound)

        visit(nodes, {})

    def used_nodes(self, node: UseStatement) -> list[object]:
        if self.base_dir is None:
            return []
        seen: set[Path] = set()

        def collect(path: Path) -> list[object]:
            path = path.resolve()
            if path in seen or not path.is_file():
                return []
            seen.add(path)
            nodes = pickle.loads(pickle.dumps(getASTfromFile(str(path)) or []))
            _unwrap_commented_names(nodes)
            _rename_python_keywords(nodes)
            for child in list(nodes):
                if isinstance(child, UseStatement):
                    module = Path(child.filepath.val).stem
                    if module.isidentifier() and not keyword.iskeyword(module):
                        nodes.extend(collect(path.parent / child.filepath.val))
            return nodes

        return collect(self.base_dir / node.filepath.val)

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
        duplicate_functions, duplicate_modules = _duplicate_parameter_names(nodes)
        self.duplicate_functions |= duplicate_functions
        self.duplicate_modules |= duplicate_modules
        self.shadowing_names |= {
            declaration.name.name
            for declaration in nodes
            if isinstance(declaration, (FunctionDeclaration, ModuleDeclaration))
            and declaration.name.name in _PIECAD_EXPORTS
        }

    def use_statement(self, node: UseStatement) -> str:
        module = Path(node.filepath.val).stem
        missing = (
            self.base_dir is None or not (self.base_dir / node.filepath.val).is_file()
        )
        missing_message = (
            f"module not found: use <{node.filepath.val}> source was not found "
            "relative to the source directory"
            if missing
            else ""
        )
        missing_comment = f"# {missing_message}{_position(node)}\n" if missing else ""
        if not module.isidentifier() or keyword.iskeyword(module):
            message = f"use {node.filepath.val!r}: {module!r} is not a valid Python module name"
            self.warn(
                node,
                (
                    f"{missing_message}; UNSUPPORTED: {message}"
                    if missing
                    else f"UNSUPPORTED: {message}"
                ),
            )
            return f"{missing_comment}# UNSUPPORTED: {message}{_position(node)}"
        module = _python_module_name(module)
        self.warn(
            node,
            (f"{missing_message}; " if missing else "")
            + f"use <{node.filepath.val}> emits a Python import only; "
            f"translate the used .scad file separately to {module}.py",
        )
        private_names = sorted(
            {
                self.py_name(declaration.name.name)
                for declaration in self.used_nodes(node)
                if isinstance(declaration, (FunctionDeclaration, ModuleDeclaration))
                and self.py_name(declaration.name.name).startswith("_")
            }
        )
        private_import = (
            f"\nfrom {module} import {', '.join(private_names)}"
            if private_names
            else ""
        )
        return f"{missing_comment}from {module} import *{private_import}"

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
        rendered, duplicate_bindings = self.parameters(node.parameters)
        params = [rendered] if rendered else []
        needed = self.segment_modules.get(node.name.name, set(_SEGMENT_VARIABLES))
        explicit = self.explicit_segment_parameters.get(
            ("module", node.name.name), set()
        )
        params.extend(
            f"_scadtopy_special_{name[1:]}=None"
            for name in _SEGMENT_VARIABLES
            if name in needed and name not in explicit
        )
        if _uses_children(node.children):
            params.append("_children=()")
        lines = [f"def {self.py_name(node.name.name)}({', '.join(params)}):"]
        enclosing_expression_definitions = self.expression_definitions
        self.expression_definitions = []
        assignments: list[str] = [
            f"    if {slot} is not {self.segment_unset}:\n" f"        {name} = {slot}"
            for name, slot in duplicate_bindings
        ]
        for name in _SEGMENT_VARIABLES:
            if name in needed and name not in explicit:
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
                if _is_user_special_variable(child.name.name):
                    self.note_unsupported(
                        child,
                        f"special variable {child.name.name} assignment is ignored",
                    )
                    continue
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
                self.note_unsupported(child, f"{_node_name(child)} is not translated")

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
                result = self.combine(parts) if parts else "None"
                lines.append(f"    return {self.normalize_absent(result)}")
            else:
                result = self.combine([value for _, value in geometry])
                lines.append(f"    return {self.normalize_absent(result)}")
        else:
            lines.append("    return None")
        if duplicate_bindings:
            lines.append(
                f"{self.py_name(node.name.name)}._scadtopy_parameter_names = "
                f"{self.original_parameter_names(node.parameters)!r}"
            )
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
        rendered, duplicate_bindings = self.parameters(node.parameters)
        params = [rendered] if rendered else []
        needed = self.segment_functions.get(node.name.name, set())
        explicit = self.explicit_segment_parameters.get(
            ("function", node.name.name), set()
        )
        inherited = self.segment_tokens()
        params.extend(
            f"_scadtopy_special_{name[1:]}=None"
            for name in _SEGMENT_VARIABLES
            if name in needed and name not in explicit
        )
        setup = [
            f"if _scadtopy_special_{name[1:]} is None:\n"
            f"    _scadtopy_special_{name[1:]} = "
            f"{f'_scadtopy_default_{name[1:]}' if value == f'_scadtopy_special_{name[1:]}' else value}"
            for name, value in zip(_SEGMENT_VARIABLES, inherited)
            if name in needed and name not in explicit
        ]
        enclosing_expression_definitions = self.expression_definitions
        self.expression_definitions = []
        previous_overrides = self.segment_overrides
        self.segment_overrides = tuple(
            f"_scadtopy_special_{name[1:]}" if name in needed | explicit else value
            for name, value in zip(_SEGMENT_VARIABLES, inherited)
        )
        expression = node.expr
        prefix_statements: list[str] = []
        if isinstance(expression, LetOp):
            names = {assignment.name.name for assignment in expression.assignments}
            assigned: set[str] = set()
            safe = len(names) == len(expression.assignments) and not any(
                name.startswith("$") for name in names
            )
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
        self.segment_overrides = previous_overrides
        return [
            f"def {self.py_name(node.name.name)}({', '.join(params)}):",
            *(
                f"    {line}"
                for name, slot in duplicate_bindings
                for line in (
                    f"if {slot} is not {self.segment_unset}:",
                    f"    {name} = {slot}",
                )
            ),
            *(f"    {line}" for statement in setup for line in statement.splitlines()),
            *(
                f"    {line}"
                for definition in definitions
                for line in definition.splitlines()
            ),
            *(f"    {statement}" for statement in prefix_statements),
            f"    return {value}",
            *(
                [
                    f"{self.py_name(node.name.name)}._scadtopy_parameter_names = "
                    f"{self.original_parameter_names(node.parameters)!r}"
                ]
                if duplicate_bindings
                else []
            ),
            *(
                [
                    f"{self.py_name(node.name.name)}._scadtopy_explicit_segments = "
                    f"{tuple(sorted(explicit))!r}"
                ]
                if explicit
                else []
            ),
        ]

    def original_parameter_names(self, parameters: list[object]) -> tuple[str, ...]:
        return tuple(
            (
                f"_scadtopy_special_{parameter.name.name[1:]}"
                if parameter.name.name in _SEGMENT_VARIABLES
                else parameter.name.name
            )
            for parameter in parameters
            if not _is_user_special_variable(parameter.name.name)
        )

    def parameters(self, parameters: list[object]) -> tuple[str, list[tuple[str, str]]]:
        rendered: list[str] = []
        duplicate_bindings: list[tuple[str, str]] = []
        names = [
            parameter.name.name
            for parameter in parameters
            if not _is_user_special_variable(parameter.name.name)
        ]
        has_duplicates = len(names) != len(set(names))
        seen: set[str] = set()
        reserved = self.user_identifiers | self.imported_identifiers
        seen_default = False
        for parameter in parameters:
            text = parameter.name.name
            if _is_user_special_variable(text):
                self.note_unsupported(
                    parameter, f"special variable {text} parameter is ignored"
                )
                continue
            if text in _SEGMENT_VARIABLES:
                text = f"_scadtopy_special_{text[1:]}"
            if text in seen:
                slot = f"_scadtopy_duplicate_parameter_{len(duplicate_bindings)}"
                while slot in reserved:
                    slot += "_"
                reserved = reserved | {slot}
                duplicate_bindings.append((text, slot))
                text = f"{slot}={self.segment_unset}"
                seen_default = True
                self.uses_duplicate_parameters = True
            elif parameter.default is not None:
                seen.add(text)
                seen_default = True
                text += f"={self.expression(parameter.default)}"
            elif seen_default or has_duplicates:
                seen.add(text)
                # Python forbids a required parameter after a defaulted one;
                # an omitted OpenSCAD parameter is undef.
                text += "=None"
                seen_default = True
            else:
                seen.add(text)
            rendered.append(text)
        return ", ".join(rendered), duplicate_bindings

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
        self.expression_bindings.extend(
            name for name in names if not _is_user_special_variable(name)
        )
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
                self.note_unsupported(
                    argument, "unknown echo argument is not translated"
                )
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

    def module_children(self, nodes: object, empty: str = "None") -> list[str]:
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
            empty_boolean = (
                isinstance(node, ModularCall)
                and node.name.name in _PIECAD_NAMES
                and node.name.name not in self.module_names
            )
            if geometry != "None" or empty_boolean:
                if assertions:
                    geometry = f"({', '.join(assertions)}, {geometry})[-1]"
                    assertions = []
                geometries.append(geometry)
        if assertions:
            if geometries:
                geometry = geometries.pop()
                geometries.append(f"({geometry}, {', '.join(assertions)})[0]")
            else:
                geometries.append(f"({', '.join(assertions)}, {empty})[-1]")
        return geometries

    def module_group(self, nodes: object, empty: str = "None") -> str:
        if any(isinstance(node, Assignment) for node in self.module_list(nodes)):
            call, _ = self.scoped_module_block(nodes, as_list=False, empty=empty)
            return call
        geometries = self.module_children(nodes, empty)
        if not geometries:
            return empty
        return self.combine(geometries)

    def scoped_module_block(
        self, nodes: object, as_list: bool, empty: str = "None"
    ) -> tuple[str, int]:
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
                if _is_user_special_variable(variable):
                    self.note_unsupported(
                        assignment, f"special variable {variable} assignment is ignored"
                    )
                    continue
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
                    [child for child in children if not isinstance(child, Assignment)],
                    empty,
                )
        finally:
            self.segment_overrides = previous_overrides

        result = (
            f"[{', '.join(geometries)}]"
            if as_list
            else self.combine(geometries) if geometries else empty
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
        previous_overrides = self.segment_overrides
        try:
            for assignment in assignments:
                target = self.loop_target(assignment)
                with self.bind_expression_names(names):
                    clauses.append(
                        f"for {target} in ({self.loop_iterable(assignment.expr)})"
                    )
                names.append(target)
                if assignment.name.name in _SEGMENT_VARIABLES:
                    settings = list(self.segment_tokens())
                    settings[_SEGMENT_VARIABLES.index(assignment.name.name)] = (
                        f"int({target})" if assignment.name.name == "$fn" else target
                    )
                    self.segment_overrides = tuple(settings)
            with self.bind_expression_names(names):
                value = self.module_group(body)
        finally:
            self.segment_overrides = previous_overrides
        clauses = " ".join(clauses)
        self.uses_boolean_helper = True
        absent = "..." if operation == "union" else "None"
        return f"{self.boolean_helper}({operation}, {absent}, *[{value} {clauses}])"

    def module_call(self, node: object) -> str:
        if (
            isinstance(node, ModularCall)
            and node.name.name in {*_PIECAD_NAMES, "color"}
            and any(
                isinstance(argument, NamedArgument)
                and argument.name.name in _SEGMENT_VARIABLES
                for argument in node.arguments
            )
        ):
            arguments: list[NamedArgument | PositionalArgument] = []
            for argument in self.ignore_user_special_arguments(node.arguments):
                if isinstance(argument, (NamedArgument, PositionalArgument)):
                    arguments.append(argument)
                else:
                    self.unsupported(argument)
            self.module_scope_counter += 1
            prefix = f"_scadtopy_segment_arg_{self.module_scope_counter}"
            reserved = self.user_identifiers | self.imported_identifiers
            while any(f"{prefix}_{i}" in reserved for i in range(len(arguments))):
                prefix += "_"
            parameters = [f"{prefix}_{i}" for i in range(len(arguments))]
            values = [self.expression(argument.expr) for argument in arguments]
            remaining: list[Argument] = []
            settings: list[Assignment] = []
            for argument, parameter in zip(arguments, parameters):
                value = Identifier(argument.position, parameter)
                if isinstance(argument, NamedArgument):
                    if argument.name.name in _SEGMENT_VARIABLES:
                        settings.append(
                            Assignment(argument.position, argument.name, value)
                        )
                    else:
                        remaining.append(
                            NamedArgument(argument.position, argument.name, value)
                        )
                elif isinstance(argument, PositionalArgument):
                    remaining.append(PositionalArgument(argument.position, value))
                else:
                    self.unsupported(argument)
            scoped_node = ModularCall(
                node.position, node.name, remaining, node.children
            )
            with self.bind_expression_names(parameters):
                body = self.let_expression(
                    settings, lambda: self.module_call(scoped_node)
                )
            return _tidy(
                f"(lambda {', '.join(parameters)}: {body})({', '.join(values)})"
            )
        if (
            isinstance(node, ModularCall)
            and node.name.name == "assign"
            and node.name.name not in self.module_names
        ):
            node.arguments = self.ignore_user_special_arguments(node.arguments)
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
                self.uses_boolean_helper = True
                return f"{self.boolean_helper}({_PIECAD_NAMES[node.name.name]}, None, *{block})"
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
                    self._module_call(
                        node, children_override=[self.combine([f"*{block}"])]
                    )
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
            return self.let_expression(
                node.assignments, lambda: self.module_group(node.children)
            )
        if isinstance(node, ModularIf):
            return f"(({self.module_group(node.true_branch, '...')}) if ({self.expression(node.condition)}) else ...)"
        if isinstance(node, ModularIfElse):
            return (
                f"(({self.module_group(node.true_branch, '...')}) if "
                f"({self.expression(node.condition)}) else "
                f"({self.module_group(node.false_branch, '...')}))"
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
                f"UNSUPPORTED: Show only ('!') is not allowed at line: {line if line is not None else '?'}"
            )
        if isinstance(node, (ModularModifierHighlight, ModularModifierBackground)):
            return self.module_call(node.child)
        if isinstance(node, ModularModifierDisable):
            return "None"
        if not isinstance(node, ModularCall):
            self.note_unsupported(node, f"{_node_name(node)} is not translated")
            return "None"

        name = node.name.name
        node.arguments = self.ignore_user_special_arguments(node.arguments)
        legacy_format = (
            name.removeprefix("import_")
            if name in {"import_stl", "import_off", "import_dxf"}
            and name not in self.module_names
            else None
        )
        legacy_mesh = legacy_format in {"stl", "off"}
        if name == "surface" and name not in self.module_names:
            self.note_unsupported(
                node, "surface() is not translated; geometry is omitted"
            )
            return "None"
        if (
            name not in self.module_names
            and name not in _CENTER_MODULES
            and name != "import"
            and not legacy_format
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
        arguments = (
            self.call_arguments(node.arguments)
            if name not in {"import", "color"} and not legacy_format
            else []
        )
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
        if (
            name not in self.module_names
            and source_children
            and all(child == "None" for child in children)
        ):
            if (
                name not in self.module_names
                and name not in _TRANSFORMS
                and name not in _PIECAD_NAMES
                and name
                not in {
                    "resize",
                    "multmatrix",
                    "linear_extrude",
                    "rotate_extrude",
                    "minkowski",
                    "offset",
                    "projection",
                    "render",
                    "group",
                    "color",
                    "text",
                    "import",
                    "import_stl",
                    "import_off",
                    "import_dxf",
                    "polyhedron",
                    "cube",
                    "sphere",
                    "cylinder",
                    "square",
                    "circle",
                    "polygon",
                    "child",
                    "children",
                }
            ):
                self.note_missing_module(node, name, True)
            return "None"
        if name not in _PIECAD_NAMES and name not in self.module_names:
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
                height = "100.0"
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
            if any(
                not isinstance(argument, NamedArgument)
                or argument.name.name != "convexity"
                for argument in node.arguments
            ):
                raise TranslationError(
                    f"{name}() does not accept arguments{_position(node)}"
                )
            ignored_arguments = [
                self.expression(argument.expr)
                for argument in node.arguments
                if isinstance(argument, NamedArgument)
            ]
            if not children:
                result = "None"
            else:
                result = self.boolean_call(_PIECAD_NAMES[name], children, "None")
            if ignored_arguments:
                return f"({', '.join(ignored_arguments)}, {result})[-1]"
            return result

        if name == "minkowski":
            if node.arguments:
                raise TranslationError(
                    f"minkowski() does not accept arguments{_position(node)}"
                )
            if len(children) < 2:
                geometry_children = [
                    child
                    for child in source_children
                    if not isinstance(child, (Assignment, ModularModifierDisable))
                ]
                if len(geometry_children) > len(children):
                    self.note_unsupported(
                        node,
                        "minkowski() omitted because child geometry was not translated",
                    )
                    return "None"
                return children[0] if children else "None"
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
            return self.normalize_absent(self.combine(children)) if children else "None"
        if name == "color":
            self.reject_unexpected_named_arguments(node, {"c", "alpha"})
            if not children:
                return "None"
            color = self.argument_expression(node.arguments, ("c",), 0)
            alpha = self.argument_expression(node.arguments, ("alpha",), 1)
            color_node = _uncommented_expression(color)
            alpha_node = _uncommented_expression(alpha)
            evaluate_alpha = alpha is not None and not (
                _literal_number(alpha_node) is not None
                or isinstance(
                    alpha_node, (BooleanLiteral, StringLiteral, UndefinedLiteral)
                )
            )
            no_color = color is None or isinstance(color_node, UndefinedLiteral)
            if no_color and not evaluate_alpha:
                return self.normalize_absent(self.combine(children))
            dimensions = {
                2 if child.name.name in {"square", "circle", "polygon"} else 3
                for child in source_children
                if isinstance(child, ModularCall)
                and child.name.name
                in {"square", "circle", "polygon", "cube", "sphere", "cylinder"}
                and child.name.name not in self.module_names
            }
            if len(dimensions) > 1:
                self.note_unsupported(
                    node, "color() cannot combine 2D and 3D child geometry"
                )
                return "None"
            parameters = []
            values = []
            color_parameter = "_scadtopy_color_arg"
            alpha_parameter = "_scadtopy_alpha_arg"
            reserved = self.user_identifiers | self.imported_identifiers
            while color_parameter in reserved:
                color_parameter += "_"
            while alpha_parameter in reserved:
                alpha_parameter += "_"
            if evaluate_alpha:
                for argument in node.arguments:
                    if not isinstance(argument, (NamedArgument, PositionalArgument)):
                        self.unsupported(argument)
                    if argument.expr is color:
                        parameters.append(color_parameter)
                    elif argument.expr is alpha:
                        parameters.append(alpha_parameter)
                    else:
                        continue
                    values.append(self.expression(argument.expr))
            if no_color:
                result = self.normalize_absent(self.combine(children))
            else:
                vector = _literal_numeric_vector(color_node, (3, 4))
                if evaluate_alpha:
                    color_value = color_parameter
                elif isinstance(
                    color_node, StringLiteral
                ) and not color_node.val.startswith("#"):
                    color_value = repr(color_node.val.lower())
                else:
                    color_value = self.expression(color)
                direct = not evaluate_alpha and (
                    vector is not None
                    or (
                        isinstance(color_node, StringLiteral)
                        and (
                            not color_node.val.startswith("#")
                            or len(color_node.val) == 7
                        )
                    )
                )
                if (
                    len(children) == 1
                    and direct
                    and not self.geometry_may_be_absent(children[0])
                ):
                    if vector is not None:
                        color_value = repr(
                            tuple(round(value * 255) for value in vector[:3])
                        )
                    result = f"{_receiver(children[0])}.color({color_value})"
                elif len(children) == 1:
                    self.uses_color_helper = True
                    result = f"{self.color_helper}({color_value}, {children[0]})"
                else:
                    self.uses_color_helper = True
                    self.uses_color_group_helper = True
                    result = f"{self.color_group_helper}({color_value}, {', '.join(children)})"
            if evaluate_alpha:
                return (
                    f"(lambda {', '.join(parameters)}: {result})({', '.join(values)})"
                )
            return result
        if name == "text":
            self.reject_children(node, children)
            text = self.argument_value(node.arguments, ("text",), 0)
            size = self.argument_value(node.arguments, ("size",), 1)
            font = self.argument_value(node.arguments, ("font",), 2)
            halign = self.argument_value(node.arguments, ("halign",), -1)
            valign = self.argument_value(node.arguments, ("valign",), -1)
            unsupported_arguments = {
                argument.name.name
                for argument in node.arguments
                if isinstance(argument, NamedArgument)
            } - {"text", "size", "font", "halign", "valign"}
            positional = [a for a in node.arguments if not isinstance(a, NamedArgument)]
            if len(positional) > 3:
                unsupported_arguments.add("positional arguments after font")
            if unsupported_arguments:
                self.note_unsupported(
                    node,
                    f"text() parameter(s) {', '.join(sorted(unsupported_arguments))} "
                    "are not translated; text geometry is omitted",
                )
                return "None"
            if text is None:
                self.note_unsupported(
                    node, "text() without a text argument is not translated"
                )
                return "None"
            size = size or "10"
            if halign is None and valign is None and font is None:
                return f"text(size={size}, text={text})"
            self.uses_text_helper = True
            return (
                f"_scadtopy_text({text}, {size}, "
                f"{halign or repr('left')}, {valign or repr('baseline')}, {font or 'None'})"
            )
        if name == "import" or legacy_format:
            self.reject_children(node, children)
            file_node = self.argument_expression(node.arguments, ("file",), 0)
            legacy_file = self.argument_expression(node.arguments, ("filename",), -1)
            if (
                file_node is None
                or isinstance(_uncommented_expression(file_node), UndefinedLiteral)
            ) and (
                legacy_file is None
                or isinstance(_uncommented_expression(legacy_file), UndefinedLiteral)
            ):
                raise TranslationError(
                    f"{name}() requires a file name{_position(node)}"
                )
            positional_parameters = ("file", "layer", "convexity")
            if legacy_mesh:
                positional_parameters += ("origin", "scale")
            for position, parameter in enumerate(positional_parameters):
                named = sum(
                    isinstance(argument, NamedArgument)
                    and argument.name.name == parameter
                    for argument in node.arguments
                )
                positional_count = sum(
                    isinstance(argument, PositionalArgument)
                    for argument in node.arguments
                )
                if named > 1 or (named and positional_count > position):
                    raise TranslationError(
                        f"{name}() got multiple values for {parameter!r}{_position(node)}"
                    )
            for parameter in ("filename", "center"):
                if (
                    sum(
                        isinstance(argument, NamedArgument)
                        and argument.name.name == parameter
                        for argument in node.arguments
                    )
                    > 1
                ):
                    raise TranslationError(
                        f"{name}() got multiple values for {parameter!r}{_position(node)}"
                    )
            unsupported_arguments = {
                argument.name.name
                for argument in node.arguments
                if isinstance(argument, NamedArgument)
                and argument.name.name
                not in (
                    {
                        "file",
                        "filename",
                        "convexity",
                        "center",
                        "layer",
                        "layername",
                        "origin",
                        "scale",
                    }
                    if legacy_mesh
                    else {"file", "filename", "convexity", "center"}
                )
                and not (
                    argument.name.name in {"layer", "layername"}
                    and isinstance(
                        _uncommented_expression(argument.expr), UndefinedLiteral
                    )
                )
            }
            positional = [
                argument
                for argument in node.arguments
                if isinstance(argument, PositionalArgument)
            ]
            if (
                not legacy_mesh
                and len(positional) > 1
                and not isinstance(
                    _uncommented_expression(positional[1].expr), UndefinedLiteral
                )
            ):
                unsupported_arguments.add("positional layer")
            if len(positional) > len(positional_parameters):
                unsupported_arguments.add("positional arguments after convexity")
            if unsupported_arguments:
                self.note_unsupported(
                    node,
                    f"{name}() parameter(s) {', '.join(sorted(unsupported_arguments))} "
                    "are not supported by Piecad load()",
                )
                return "None"
            selected_literal = (
                legacy_file
                if file_node is None
                or isinstance(_uncommented_expression(file_node), UndefinedLiteral)
                else file_node
            )
            selected_literal = _uncommented_expression(selected_literal)
            if isinstance(selected_literal, StringLiteral):
                if not selected_literal.val:
                    raise TranslationError(
                        f"{name}() requires a file name{_position(node)}"
                    )
                extension = selected_literal.val.rpartition(".")[2].lower()
                if not legacy_format and extension not in _PIECAD_LOAD_EXTENSIONS:
                    self.note_unsupported(
                        node,
                        f"import() of .{extension} files is not supported by this translator",
                    )
                    return "None"
            convexity_node = self.argument_expression(node.arguments, ("convexity",), 2)
            evaluate_convexity = convexity_node is not None and not (
                _literal_number(_uncommented_expression(convexity_node)) is not None
                or isinstance(
                    _uncommented_expression(convexity_node),
                    (BooleanLiteral, StringLiteral, UndefinedLiteral),
                )
            )
            center_node = self.argument_expression(node.arguments, ("center",), -1)
            center_literal = _uncommented_expression(center_node)
            dynamic_center = center_node is not None and not (
                _literal_number(center_literal) is not None
                or isinstance(
                    center_literal, (BooleanLiteral, StringLiteral, UndefinedLiteral)
                )
            )
            values = []
            parameters = []
            for argument in node.arguments:
                if not isinstance(argument, (NamedArgument, PositionalArgument)):
                    self.unsupported(argument)
                if argument.expr is file_node:
                    parameters.append("_scadtopy_file")
                elif argument.expr is legacy_file:
                    parameters.append("_scadtopy_filename")
                elif evaluate_convexity and argument.expr is convexity_node:
                    parameters.append("_scadtopy_convexity")
                elif dynamic_center and argument.expr is center_node:
                    parameters.append("_scadtopy_center")
                elif legacy_mesh and not isinstance(
                    _uncommented_expression(argument.expr),
                    (BooleanLiteral, StringLiteral, NumberLiteral, UndefinedLiteral),
                ):
                    parameter = f"_scadtopy_ignored_{len(parameters)}"
                    while (
                        parameter in self.user_identifiers | self.imported_identifiers
                    ):
                        parameter += "_"
                    parameters.append(parameter)
                else:
                    continue
                values.append(self.expression(argument.expr))
            if file_node is not None and legacy_file is not None:
                selected = (
                    "_scadtopy_file if _scadtopy_file is not None "
                    "else _scadtopy_filename"
                )
            elif file_node is not None:
                selected = "_scadtopy_file"
            else:
                selected = "_scadtopy_filename"
            simple = len(parameters) == 1
            filename = values[0] if simple else selected
            svg_compat = False
            if legacy_format and not (
                isinstance(selected_literal, StringLiteral)
                and selected_literal.val.lower().endswith(f".{legacy_format}")
            ):
                if legacy_format == "dxf":
                    self.uses_dxf_import_helper = True
                    load_call = f"{self.dxf_import_helper}({filename})"
                else:
                    self.uses_mesh_import_helper = True
                    load_call = (
                        f"{self.mesh_import_helper}({filename}, {legacy_format!r})"
                    )
            else:
                svg_compat = (
                    not legacy_format
                    and (
                        not isinstance(selected_literal, StringLiteral)
                        or selected_literal.val.lower().endswith(".svg")
                    )
                )
                options = ", svg_openscad=True" if svg_compat else ""
                if svg_compat and isinstance(center_literal, BooleanLiteral) and center_literal.val:
                    options += ", center=True"
                elif svg_compat and dynamic_center:
                    options += ", center=_scadtopy_center is True"
                load_call = f"load({filename}{options})"
            if (
                not legacy_format
                and not svg_compat
                and isinstance(center_literal, BooleanLiteral)
                and center_literal.val
            ):
                load_call += ".center()"
            elif not legacy_format and not svg_compat and dynamic_center:
                load_call = f"({load_call}.center() if _scadtopy_center is True else {load_call})"
            if not simple:
                load_call = (
                    f"(lambda {', '.join(parameters)}: {load_call})"
                    f"({', '.join(values)})"
                )
            rendered_arguments = dict(zip(parameters, values))
            if isinstance(selected_literal, StringLiteral):
                warning_argument = f'"{selected_literal.val}"'
            elif file_node is not None and legacy_file is not None:
                warning_argument = (
                    f"{rendered_arguments['_scadtopy_file']} "
                    f"(or {rendered_arguments['_scadtopy_filename']} if undefined)"
                )
            elif file_node is not None:
                warning_argument = rendered_arguments["_scadtopy_file"]
            else:
                warning_argument = rendered_arguments["_scadtopy_filename"]
            self.warn(
                node,
                f"{name}({warning_argument}) is passed to Piecad unchanged and "
                "resolves relative to the Python process working directory, "
                "not the .scad file directory",
            )
            return load_call
        if name == "polyhedron":
            self.reject_children(node, children)
            self.reject_unexpected_named_arguments(
                node, {"points", "faces", "triangles", "convexity"}
            )
            positional = sum(
                isinstance(argument, PositionalArgument) for argument in node.arguments
            )
            if positional > 3:
                raise TranslationError(
                    f"Too many polyhedron() arguments{_position(node)}"
                )
            for position, parameter in enumerate(("points", "faces", "convexity")):
                named = sum(
                    isinstance(argument, NamedArgument)
                    and argument.name.name == parameter
                    for argument in node.arguments
                )
                if named > 1 or (named and positional > position):
                    raise TranslationError(
                        f"polyhedron() got multiple values for {parameter!r}{_position(node)}"
                    )
            if (
                sum(
                    isinstance(argument, NamedArgument)
                    and argument.name.name == "triangles"
                    for argument in node.arguments
                )
                > 1
            ):
                raise TranslationError(
                    f"polyhedron() got multiple values for 'triangles'{_position(node)}"
                )
            points = self.argument_value(node.arguments, ("points",), 0)
            faces = self.argument_value(node.arguments, ("faces",), 1)
            legacy_faces = self.argument_value(node.arguments, ("triangles",), -1)
            if faces is None:
                faces = legacy_faces
            elif legacy_faces is not None:
                face_argument = self.argument_expression(node.arguments, ("faces",), 1)
                legacy_argument = self.argument_expression(
                    node.arguments, ("triangles",), -1
                )
                ordered = [
                    argument.expr
                    for argument in node.arguments
                    if isinstance(argument, (NamedArgument, PositionalArgument))
                ]
                legacy_first = next(
                    value is legacy_argument
                    for value in ordered
                    if value is face_argument or value is legacy_argument
                )
                parameters = "triangles, faces" if legacy_first else "faces, triangles"
                values = (
                    f"{legacy_faces}, {faces}"
                    if legacy_first
                    else f"{faces}, {legacy_faces}"
                )
                faces = (
                    f"(lambda {parameters}: faces if faces is not None else triangles)"
                    f"({values})"
                )
            if points is not None and faces is not None:
                triangles = (
                    f"[tuple(int(index) for index in "
                    f"(face[0], face[i + 1], face[i])) "
                    f"for face in {faces} for i in range(1, len(face) - 1)]"
                )
                return f"polyhedron({points}, {triangles}, check='repair')"
            raise TranslationError(
                f"polyhedron() requires points and faces{_position(node)}"
            )

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
            self.uses_boolean_helper = True
            index = self.argument_expression(node.arguments, ("index",), 0)
            child_list = "_children" if self.in_module or name == "children" else "()"
            if index is not None:
                return f"_scadtopy_children({child_list}, {self.expression(index)})"
            if name == "child":
                return f"_scadtopy_children({child_list}, 0)"
            return f"_scadtopy_children({child_list})"
        if name in self.module_names:
            passes_children = bool(children and name in self.children_modules)
            if (
                _positional_after_named(node.arguments)
                or name in self.duplicate_modules
            ):
                extra = (
                    [("_children", f"[{', '.join(children)}]")]
                    if passes_children
                    else []
                )
                return self.special_function_call(
                    self.py_name(name), node.arguments, extra
                )
            arguments = self.module_call_arguments(
                name, node.arguments, passes_children
            )
            if passes_children:
                arguments.append(f"_children=[{', '.join(children)}]")
            return f"{self.py_name(name)}({', '.join(arguments)})"
        self.note_missing_module(node, name, bool(source_children))
        return "None"

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
            if self.geometry_may_be_absent(geometry):
                self.uses_translate_helper = True
                return f"{self.translate_helper}({_receiver(geometry)}, {rendered})"
            dimensions = self.geometry_dimension(node.children)
            parsed = ast.parse(rendered, mode="eval").body
            if dimensions == 2:
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
                if isinstance(parsed, ast.List) and len(parsed.elts) == 2:
                    rendered = ast.unparse(
                        ast.List(elts=[*parsed.elts, ast.Constant(0)], ctx=ast.Load())
                    )
                if isinstance(parsed, ast.List) and len(parsed.elts) in (2, 3):
                    return f"{_receiver(geometry)}.translate({rendered})"
            self.uses_translate_helper = True
            return f"{self.translate_helper}({_receiver(geometry)}, {rendered})"

        if name == "scale":
            self.reject_unexpected_named_arguments(node, {"v"})
            factors = self.argument_expression(node.arguments, ("v",), 0)
            if factors is None or len(node.arguments) != 1:
                raise TranslationError(
                    f"scale() requires one factor or vector{_position(node)}"
                )
            rendered = self.expression(factors)
            dimensions = self.geometry_dimension(node.children)
            if dimensions in (2, 3) and not self.geometry_may_be_absent(geometry):
                parsed = ast.parse(rendered, mode="eval").body
                if _literal_number(_uncommented_expression(factors)) is not None:
                    rendered = f"[{', '.join([rendered] * dimensions)}]"
                    return f"{_receiver(geometry)}.scale({rendered})"
                if isinstance(parsed, ast.List) and len(parsed.elts) in (2, 3):
                    if dimensions == 3 and len(parsed.elts) == 2:
                        rendered = ast.unparse(
                            ast.List(
                                elts=[*parsed.elts, ast.Constant(1)], ctx=ast.Load()
                            )
                        )
                    elif dimensions == 2 and len(parsed.elts) == 3:
                        if isinstance(parsed.elts[2], ast.Constant):
                            rendered = ast.unparse(
                                ast.List(elts=parsed.elts[:2], ctx=ast.Load())
                            )
                        else:
                            rendered = f"({rendered})[:2]"
                    return f"{_receiver(geometry)}.scale({rendered})"
            self.uses_scale_helper = True
            return f"{self.scale_helper}({_receiver(geometry)}, {rendered})"

        if name == "rotate":
            self.reject_unexpected_named_arguments(node, {"a", "v"})
            angle = self.argument_expression(node.arguments, ("a",), 0)
            axis = self.argument_expression(node.arguments, ("v",), 1)
            if angle is None or len(node.arguments) not in {1, 2}:
                raise TranslationError(f"rotate() requires an angle{_position(node)}")
            rendered_angle = self.expression(angle)
            dimensions = self.geometry_dimension(node.children)
            scalar_literal = isinstance(angle, NumberLiteral) or (
                isinstance(angle, UnaryMinusOp)
                and isinstance(angle.expr, NumberLiteral)
            )
            may_be_absent = self.geometry_may_be_absent(geometry)
            if scalar_literal and dimensions == 3 and not may_be_absent:
                axis_values = _literal_numeric_vector(axis, (3,))
                if (
                    axis_values is not None
                    and sum(value != 0 for value in axis_values) == 1
                ):
                    axis_index = next(
                        i for i, value in enumerate(axis_values) if value != 0
                    )
                    angles = ["0", "0", "0"]
                    sign = "-" if axis_values[axis_index] < 0 else ""
                    angles[axis_index] = f"{sign}({rendered_angle})"
                    return f"{_receiver(geometry)}.rotate([{', '.join(angles)}])"
            if (
                axis is None
                and scalar_literal
                and dimensions in (2, 3)
                and not may_be_absent
            ):
                angles = (
                    rendered_angle if dimensions == 2 else f"[0, 0, {rendered_angle}]"
                )
                return f"{_receiver(geometry)}.rotate({angles})"
            if (
                axis is None
                and isinstance(angle, ListComprehension)
                and len(angle.elements) == 3
                and dimensions == 3
                and not may_be_absent
            ):
                return f"{_receiver(geometry)}.rotate({rendered_angle})"
            self.uses_rotate_helper = True
            rendered_axis = f", {self.expression(axis)}" if axis is not None else ""
            return f"{self.rotate_helper}({_receiver(geometry)}, {rendered_angle}{rendered_axis})"

        if name == "mirror":
            self.reject_unexpected_named_arguments(node, {"v"})
            axis = self.argument_expression(node.arguments, ("v",), 0)
            if axis is None or len(node.arguments) != 1:
                raise TranslationError(
                    f"mirror() requires one axis vector{_position(node)}"
                )
            axis_values = _literal_numeric_vector(axis, (2, 3))
            dimensions = self.geometry_dimension(node.children)
            if axis_values is not None:
                axis_values += [0] * (3 - len(axis_values))
                nonzero = sum(value != 0 for value in axis_values)
                if nonzero == 0 or (
                    dimensions == 2 and axis_values[0] == axis_values[1] == 0
                ):
                    return geometry
                if nonzero == 1 and dimensions in (2, 3):
                    flags = [str(value != 0) for value in axis_values[:dimensions]]
                    return f"{_receiver(geometry)}.mirror(({', '.join(flags)}))"
            self.uses_mirror_helper = True
            return (
                f"{self.mirror_helper}({_receiver(geometry)}, {self.expression(axis)})"
            )

        raise AssertionError(f"Missing transform mapping for {name}")

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
            size = "1.0"
        centered = f", center={center}" if center is not None else ""
        return f"cube({size}{centered})"

    def cylinder(self, node: ModularCall) -> str:
        positional = [
            argument
            for argument in node.arguments
            if isinstance(argument, PositionalArgument)
        ]
        if len(positional) > 4:
            raise TranslationError(f"Too many cylinder() arguments{_position(node)}")
        tapered = len(positional) >= 3 and not (
            len(positional) == 3 and isinstance(positional[2].expr, BooleanLiteral)
        )
        height = self.argument_value(node.arguments, ("h", "height"), 0)
        radius = self.argument_value(
            node.arguments, ("r", "radius"), -1 if tapered else 1
        )
        r1 = self.argument_value(node.arguments, ("r1",), 1 if tapered else -1)
        r2 = self.argument_value(node.arguments, ("r2",), 2 if tapered else -1)
        diameter = self.argument_value(node.arguments, ("d", "diameter"), -1)
        d1 = self.argument_value(node.arguments, ("d1",), -1)
        d2 = self.argument_value(node.arguments, ("d2",), -1)
        center = self.argument_value(node.arguments, ("center",), 3 if tapered else 2)
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
        if diameter is not None:
            radius = f"({diameter}) / 2"
        if d1 is not None:
            r1 = f"({d1}) / 2"
        if d2 is not None:
            r2 = f"({d2}) / 2"
        if r1 is not None or r2 is not None:
            r1 = r1 or radius or "1.0"
            r2 = r2 or radius or "1.0"
            segment_radius = f"max(abs({r1}), abs({r2}))"
            try:
                literal_r1 = ast.literal_eval(r1)
                literal_r2 = ast.literal_eval(r2)
            except (ValueError, SyntaxError):
                literal_r1 = literal_r2 = None
            if (
                type(literal_r1) not in (int, float)
                or type(literal_r2) not in (int, float)
                or literal_r1 == 0
            ):
                self.uses_cone_helper = True
                self.uses_segments_helper = True
                settings = self.segment_settings(node.arguments)
                return (
                    f"{self.cone_helper}({height}, {r1}, {r2}, "
                    f"{center or 'False'}, {', '.join(settings)})"
                )
        else:
            radius = radius or "1.0"
            segment_radius = radius
        keywords = [f"segments={self.segment_count(node, segment_radius)}"]
        if center is not None:
            keywords.append(f"center={center}")
        if r1 is not None:
            return f"cone({height}, {r1}, {r2}, {', '.join(keywords)})"
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
        radius = self.argument_value(node.arguments, ("r", "radius"), 0)
        diameter = self.argument_value(node.arguments, ("d", "diameter"), -1)
        if radius is not None and diameter is not None:
            self.module_scope_counter += 1
            prefix = f"_scadtopy_radius_arg_{self.module_scope_counter}"
            reserved = self.user_identifiers | self.imported_identifiers
            while any(f"{prefix}_{i}" in reserved for i in range(len(node.arguments))):
                prefix += "_"
            names = [f"{prefix}_{i}" for i in range(len(node.arguments))]
            arguments: list[Argument] = []
            for argument, name in zip(node.arguments, names):
                value = Identifier(argument.position, name)
                if isinstance(argument, NamedArgument):
                    arguments.append(
                        NamedArgument(argument.position, argument.name, value)
                    )
                elif isinstance(argument, PositionalArgument):
                    arguments.append(PositionalArgument(argument.position, value))
                else:
                    self.unsupported(argument)
            scoped_node = ModularCall(
                node.position, node.name, arguments, node.children
            )
            radius = self.argument_value(arguments, ("r", "radius"), 0)
            diameter = self.argument_value(arguments, ("d", "diameter"), -1)
            self.uses_radius_choice_helper = True
            chosen = f"{prefix}_radius"
            while chosen in reserved or chosen in names:
                chosen += "_"
            call = (
                f"{target}(radius={chosen}, "
                f"segments={self.segment_count(scoped_node, chosen, positional_fn_index=1)})"
            )
            values = [self.expression(argument.expr) for argument in node.arguments]
            return (
                f"(lambda {', '.join(names)}: "
                f"(lambda {chosen}: {call})"
                f"({self.radius_choice_helper}({radius}, {diameter})))"
                f"({', '.join(values)})"
            )
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
        self,
        module_name: str,
        arguments: list[object],
        passes_children: bool,
        *,
        function: bool = False,
    ) -> list[str]:
        special_names = {
            "$fn": "_scadtopy_special_fn",
            "$fa": "_scadtopy_special_fa",
            "$fs": "_scadtopy_special_fs",
        }
        rendered: list[str] = []
        provided: set[str] = set()
        needed = (
            self.segment_functions.get(module_name, set(_SEGMENT_VARIABLES))
            if function
            else self.segment_modules.get(module_name, set(_SEGMENT_VARIABLES))
        )
        explicit = self.explicit_segment_parameters.get(
            ("function" if function else "module", module_name), set()
        )
        for argument in arguments:
            if isinstance(argument, PositionalArgument):
                rendered.append(self.expression(argument.expr))
            elif isinstance(argument, NamedArgument):
                name = argument.name.name
                if name in special_names:
                    value = self.expression(argument.expr)
                    if name == "$fn":
                        value = f"int({value})"
                    if name in needed | explicit:
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
            (
                module_name in self.function_names
                if function
                else module_name in self.local_module_names
            )
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
            and name not in explicit
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

    def special_function_call(
        self,
        function: str,
        arguments: list[object],
        extra_arguments: list[tuple[str, str]] | None = None,
    ) -> str:
        self.uses_special_call_helper = True
        entries = []
        for argument in arguments:
            if isinstance(argument, PositionalArgument):
                entries.append(f"(None, {self.expression(argument.expr)})")
            elif isinstance(argument, NamedArgument):
                entries.append(
                    f"({argument.name.name!r}, {self.expression(argument.expr)})"
                )
            else:
                self.unsupported(argument)
        if extra_arguments:
            entries.extend(f"({name!r}, {value})" for name, value in extra_arguments)
        settings = ", ".join(self.segment_tokens())
        return f"{self.special_call_helper}({function}, [{', '.join(entries)}], ({settings}))"

    def special_builtin_call(self, name: str, arguments: list[object]) -> str | None:
        ordinary = [
            argument
            for argument in arguments
            if not (
                isinstance(argument, NamedArgument)
                and argument.name.name in _SEGMENT_VARIABLES
            )
        ]
        used_names = {
            argument.name.name
            for argument in ordinary
            if isinstance(argument, NamedArgument)
        }
        parameters = []
        placeholders = []
        for index, argument in enumerate(ordinary):
            parameter = (
                argument.name.name
                if isinstance(argument, NamedArgument)
                else f"_scadtopy_argument_{index}"
            )
            while parameter in parameters or (
                isinstance(argument, PositionalArgument) and parameter in used_names
            ):
                parameter += "_"
            parameters.append(parameter)
            identifier = Identifier(argument.position, parameter)
            if isinstance(argument, NamedArgument):
                placeholders.append(
                    NamedArgument(argument.position, argument.name, identifier)
                )
            elif isinstance(argument, PositionalArgument):
                placeholders.append(PositionalArgument(argument.position, identifier))
            else:
                self.unsupported(argument)
        rendered = self.call_arguments(placeholders)
        value = self.builtin_call(name, placeholders, rendered)
        if value is None:
            return None
        return self.special_function_call(
            f"(lambda {', '.join(parameters)}: {value})", arguments
        )

    def ignore_user_special_arguments(self, arguments: list[object]) -> list[object]:
        retained = []
        for argument in arguments:
            if isinstance(argument, NamedArgument) and _is_user_special_variable(
                argument.name.name
            ):
                self.note_unsupported(
                    argument,
                    f"special variable {argument.name.name} argument is ignored",
                )
            else:
                retained.append(argument)
        return retained

    def loop_target(self, assignment: Assignment) -> str:
        name = assignment.name.name
        if name in _SEGMENT_VARIABLES:
            self.each_counter += 1
            target = f"_scadtopy_loop_{name[1:]}_{self.each_counter}"
            while target in self.user_identifiers | self.imported_identifiers:
                target += "_"
            return target
        if not _is_user_special_variable(name):
            return name
        self.note_unsupported(
            assignment, f"special variable {name} loop binding is ignored"
        )
        self.each_counter += 1
        target = f"_scadtopy_ignored_{self.each_counter}"
        while target in self.user_identifiers | self.imported_identifiers:
            target += "_"
        return target

    def loop_iterable(self, node: object) -> str:
        value = self.expression(node)
        if isinstance(node, (ListComprehension, RangeLiteral)):
            return value
        if _node_name(node) == "TernaryOp" and all(
            isinstance(branch, (ListComprehension, RangeLiteral))
            for branch in (node.true_expr, node.false_expr)
        ):
            return value
        self.uses_loop_iter_helper = True
        return f"{self.loop_iter_helper}({value})"

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
        if len(geometries) == 1 and not geometries[0].startswith("*"):
            return geometries[0]
        return self.boolean_call("union", geometries, "...")

    def geometry_may_be_absent(self, geometry: str) -> bool:
        try:
            tree = ast.parse(geometry.lstrip("*"), mode="eval")
        except SyntaxError:
            return True
        module_names = {self.py_name(name) for name in self.module_names}
        return any(
            isinstance(node, ast.Constant)
            and node.value in (None, Ellipsis)
            or isinstance(node, ast.Name)
            and (
                node.id in module_names
                or node.id.startswith("_scadtopy_geometry_")
                or node.id.startswith("_scadtopy_block_")
                or node.id in {self.boolean_helper, "_scadtopy_children"}
            )
            for node in ast.walk(tree)
        )

    def normalize_absent(self, geometry: str) -> str:
        try:
            tree = ast.parse(geometry, mode="eval")
        except SyntaxError:
            return geometry
        if not any(
            isinstance(node, ast.Constant)
            and node.value is Ellipsis
            or isinstance(node, ast.Name)
            and (
                node.id == "_scadtopy_children"
                or node.id.startswith("_scadtopy_block_")
                or node.id.startswith("_scadtopy_geometry_")
            )
            for node in ast.walk(tree)
        ):
            return geometry
        return f"(lambda part: None if part is ... else part)({geometry})"

    def boolean_call(self, operation: str, children: list[str], absent: str) -> str:
        if not any(self.geometry_may_be_absent(child) for child in children):
            if operation == "union" and len(children) == 1:
                return children[0]
            return f"{operation}({', '.join(children)})"
        self.uses_boolean_helper = True
        return f"{self.boolean_helper}({operation}, {absent}, {', '.join(children)})"

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
        if name in {"import_stl", "import_off"} and name not in self.module_names:
            return 3
        if name == "import_dxf" and name not in self.module_names:
            return 2
        if name == "assign" and name not in self.module_names:
            return self.geometry_dimension(
                nodes.children, active_modules, children_dimension
            )
        if name == "import":
            file_node = self.argument_expression(nodes.arguments, ("file",), 0)
            if file_node is None or isinstance(
                _uncommented_expression(file_node), UndefinedLiteral
            ):
                file_node = self.argument_expression(nodes.arguments, ("filename",), -1)
            file_node = _uncommented_expression(file_node)
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

    def note_unsupported(self, node: object, message: str) -> None:
        diagnostic = f"# UNSUPPORTED: {message}{_position(node)}"
        if diagnostic not in self.diagnostics:
            self.diagnostics.append(diagnostic)
            self.warn(node, f"UNSUPPORTED: {message}")

    def note_missing_module(self, node: object, name: str, children: bool) -> None:
        message = f"module not found: OpenSCAD module {name!r} could not be resolved"
        if children:
            message += "; child geometry is omitted"
        diagnostic = f"# {message}{_position(node)}"
        if diagnostic not in self.diagnostics:
            self.diagnostics.append(diagnostic)
            self.warn(node, message)

    def let_expression(
        self, assignments: list[Assignment], body: Callable[[], str]
    ) -> str:
        def bind(index: int) -> str:
            if index == len(assignments):
                return body()
            assignment = assignments[index]
            if _is_user_special_variable(assignment.name.name):
                self.note_unsupported(
                    assignment,
                    f"special variable {assignment.name.name} binding is ignored",
                )
                return bind(index + 1)
            value = self.expression(assignment.expr)
            target = self.loop_target(assignment)
            previous_overrides = self.segment_overrides
            if assignment.name.name in _SEGMENT_VARIABLES:
                settings = list(self.segment_tokens())
                settings[_SEGMENT_VARIABLES.index(assignment.name.name)] = (
                    f"int({target})" if assignment.name.name == "$fn" else target
                )
                self.segment_overrides = tuple(settings)
            try:
                with self.bind_expression_names([target]):
                    inner = bind(index + 1)
            finally:
                self.segment_overrides = previous_overrides
            return f"(lambda {target}: {inner})({value})"

        return bind(0)

    def list_comprehension(self, node: ListComprehension) -> str:
        elements = [
            _uncommented_expression(element)
            for element in node.elements
            if not isinstance(element, (CommentLine, CommentSpan, BlankLine))
        ]
        if not elements:
            return "[]"
        controls = (ListCompCFor, ListCompFor, ListCompIf, ListCompIfElse)
        if len(elements) == 1 and isinstance(elements[0], ListCompEach):
            element = elements[0]
            body = _uncommented_expression(element.body)
            if isinstance(body, (ListComprehension, RangeLiteral)):
                return self.expression(body)
            if isinstance(body, UndefinedLiteral):
                return "[]"
            if isinstance(body, (NumberLiteral, BooleanLiteral)):
                return f"[{self.expression(body)}]"
            if isinstance(body, StringLiteral):
                return f"[*{self.expression(body)}]"
            return self.vector_segment(element)
        if any(
            isinstance(element, ListCompLet)
            or (
                isinstance(element, ListCompEach)
                and not isinstance(
                    _uncommented_expression(element.body), ListComprehension
                )
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
        if (
            not clauses
            or _contains_function_literal(element)
            or any(a.name.name.startswith("$") for a in element.assignments)
        ):
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

    def c_for_segment(
        self, element: ListCompCFor, clauses: list[str], each_depth: int = 0
    ) -> str:
        initial_names = list(
            dict.fromkeys(assignment.name.name for assignment in element.inits)
        )
        update_names = [assignment.name.name for assignment in element.incrs]
        names = initial_names + update_names
        ignored = {name for name in names if _is_user_special_variable(name)}
        if ignored:
            self.note_unsupported(
                element,
                f"C-style comprehension with special variables "
                f"{', '.join(sorted(ignored))} is ignored",
            )
            return "[]"
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

        simple = self.simple_c_for_segment(element, clauses, each_depth)
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
            body = self.vector_segment(element.body, each_depth=each_depth)
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
        self, element: ListCompCFor, clauses: list[str], each_depth: int = 0
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
                element.body, [*clauses, f"for {name} in {iterator}"], each_depth
            )

    def vector_segment(
        self, element: object, clauses: list[str] | None = None, each_depth: int = 0
    ) -> str:
        element = _uncommented_expression(element)
        clauses = list(clauses or [])
        if isinstance(element, ListCompFor):
            names: list[str] = []
            previous_overrides = self.segment_overrides
            try:
                for assignment in element.assignments:
                    target = self.loop_target(assignment)
                    with self.bind_expression_names(names):
                        clauses.append(
                            f"for {target} in ({self.loop_iterable(assignment.expr)})"
                        )
                    names.append(target)
                    if assignment.name.name in _SEGMENT_VARIABLES:
                        settings = list(self.segment_tokens())
                        settings[_SEGMENT_VARIABLES.index(assignment.name.name)] = (
                            f"int({target})"
                            if assignment.name.name == "$fn"
                            else target
                        )
                        self.segment_overrides = tuple(settings)
                with self.bind_expression_names(names):
                    return self.vector_segment(element.body, clauses, each_depth)
            finally:
                self.segment_overrides = previous_overrides
        if isinstance(element, ListCompIf):
            if not any(clause.startswith("for ") for clause in clauses):
                dummy = "_scadtopy_dummy"
                while (
                    dummy in self.user_identifiers or dummy in self.imported_identifiers
                ):
                    dummy += "_"
                clauses.insert(0, f"for {dummy} in [None]")
            clauses.append(f"if ({self.expression(element.condition)})")
            return self.vector_segment(element.true_expr, clauses, each_depth)
        if isinstance(element, ListCompIfElse):
            true_expr = _uncommented_expression(element.true_expr)
            false_expr = _uncommented_expression(element.false_expr)
            controls = (
                ListCompEach,
                ListCompCFor,
                ListCompFor,
                ListCompIf,
                ListCompIfElse,
                ListCompLet,
            )
            if (
                each_depth
                or isinstance(true_expr, controls)
                or isinstance(false_expr, controls)
            ):
                condition = self.expression(element.condition)
                true_value = self.vector_segment(true_expr, each_depth=each_depth)
                false_value = self.vector_segment(false_expr, each_depth=each_depth)
                value = f"(({true_value}) if ({condition}) else ({false_value}))"
                if not clauses:
                    return value
                item = "_scadtopy_if_item"
                while (
                    item in self.user_identifiers or item in self.imported_identifiers
                ):
                    item += "_"
                return f"[{item} {' '.join(clauses)} for {item} in {value}]"
            value = (
                f"(({self.expression(true_expr)}) if "
                f"({self.expression(element.condition)}) else "
                f"({self.expression(false_expr)}))"
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
                return self.vector_segment(element.body, clauses, each_depth)
        if isinstance(element, ListCompLet):
            if clauses:
                self.each_counter += 1
                item = f"_scadtopy_let_item_{self.each_counter}"
                while item in self.user_identifiers | self.imported_identifiers:
                    item += "_"
                value = self.let_expression(
                    element.assignments,
                    lambda: self.vector_segment(element.body, each_depth=each_depth),
                )
                return f"[{item} {' '.join(clauses)} for {item} in {value}]"
            return self.let_expression(
                element.assignments,
                lambda: self.vector_segment(element.body, clauses, each_depth),
            )
        if isinstance(element, ListCompEach):
            return self.vector_segment(element.body, clauses, each_depth + 1)
        if isinstance(element, ListCompCFor):
            return self.c_for_segment(element, clauses, each_depth)
        value = self.expression(element)
        if each_depth:
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
            if each_depth == 1 and not clauses:
                return f"{name}({value})"
            for index in range(each_depth):
                item = f"{name}_item_{index}"
                while (
                    item in self.user_identifiers or item in self.imported_identifiers
                ):
                    item += "_"
                clauses.append(f"for {item} in {name}({value})")
                value = item
        return f"[{value} {' '.join(clauses)}]".rstrip()

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
            if _is_user_special_variable(node.name):
                self.note_unsupported(
                    node,
                    f"special variable {node.name} is ignored and treated as undef",
                )
                return "None"
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
            return self.let_expression(
                node.assignments, lambda: self.expression(node.body)
            )
        if isinstance(node, EchoOp):
            return self.echo_expression(node.arguments, self.expression(node.body))
        if isinstance(node, AssertOp):
            return self.assert_expression(node)
        if isinstance(node, FunctionLiteral):
            rendered, duplicate_bindings = self.parameters(node.parameters)
            parameters = [rendered] if rendered else []
            needed = _identifier_names(node.body) & set(_SEGMENT_VARIABLES)
            for name in _called_names(node.body):
                needed.update(self.segment_functions.get(name, set()))
            if _has_dynamic_function_call(node.body, set(self.segment_functions)):
                needed.update(_SEGMENT_VARIABLES)
            explicit = {
                parameter.name.name
                for parameter in node.parameters
                if parameter.name.name in _SEGMENT_VARIABLES
            }
            inherited = self.segment_tokens()
            parameters.extend(
                f"_scadtopy_special_{name[1:]}={value}"
                for name, value in zip(_SEGMENT_VARIABLES, inherited)
                if name in needed and name not in explicit
            )
            previous_overrides = self.segment_overrides
            self.segment_overrides = tuple(
                f"_scadtopy_special_{name[1:]}" if name in needed | explicit else value
                for name, value in zip(_SEGMENT_VARIABLES, inherited)
            )
            try:
                with self.bind_expression_names(
                    [
                        p.name.name
                        for p in node.parameters
                        if p.name.name not in _SEGMENT_VARIABLES
                    ]
                    + [
                        f"_scadtopy_special_{name[1:]}"
                        for name in _SEGMENT_VARIABLES
                        if name in needed | explicit
                    ]
                ):
                    body = self.expression(node.body)
            finally:
                self.segment_overrides = previous_overrides
            for name, slot in reversed(duplicate_bindings):
                body = (
                    f"(lambda {name}: {body})("
                    f"{slot} if {slot} is not {self.segment_unset} else {name})"
                )
            value = f"lambda {', '.join(parameters)}: {body}"
            attributes = {}
            if explicit:
                attributes["_scadtopy_explicit_segments"] = tuple(sorted(explicit))
            if duplicate_bindings:
                attributes["_scadtopy_parameter_names"] = self.original_parameter_names(
                    node.parameters
                )
            if attributes:
                return (
                    "(lambda _scadtopy_function: "
                    "(_scadtopy_function.__dict__.update("
                    f"{attributes!r}), "
                    f"_scadtopy_function)[1])({value})"
                )
            return value
        if isinstance(node, RenderExpression):
            return self.module_group(node.children)
        if isinstance(node, ListComprehension):
            return self.list_comprehension(node)
        if isinstance(node, PrimaryCall):
            arguments_nodes = self.ignore_user_special_arguments(node.arguments)
            has_special = any(
                isinstance(argument, NamedArgument)
                and argument.name.name in _SEGMENT_VARIABLES
                for argument in arguments_nodes
            )
            if not isinstance(node.left, Identifier):
                callable_expr = _receiver(self.expression(node.left))
                return self.special_function_call(callable_expr, arguments_nodes)
            name = node.left.name
            if name.startswith("$"):
                callable_expr = self.expression(node.left)
                if callable_expr == "None":
                    return "None"
                return self.special_function_call(callable_expr, arguments_nodes)
            if name in self.function_names and (
                _positional_after_named(arguments_nodes)
                or name in self.duplicate_functions
            ):
                return self.special_function_call(self.py_name(name), arguments_nodes)
            if has_special and name not in self.segment_functions:
                builtin = self.special_builtin_call(name, arguments_nodes)
                if builtin is not None:
                    return builtin
                if name in _MATH_FUNCTIONS:
                    self.uses_math = True
                function = _MATH_FUNCTIONS.get(name, self.py_name(name))
                return self.special_function_call(function, arguments_nodes)
            arguments = (
                self.module_call_arguments(name, arguments_nodes, False, function=True)
                if name in self.segment_functions
                else self.call_arguments(arguments_nodes)
            )
            if name not in self.function_names and name not in self.segment_functions:
                if name in _MATH_FUNCTIONS:
                    self.uses_math = True
                    return f"{_MATH_FUNCTIONS[name]}({', '.join(arguments)})"
                builtin = self.builtin_call(name, arguments_nodes, arguments)
                if builtin is not None:
                    return builtin
                if name == "norm" and len(arguments) == 1:
                    self.uses_math = True
                    return f"math.hypot(*{arguments[0]})"
                if name == "cross" and len(arguments_nodes) == 2:
                    return self.cross(arguments_nodes, arguments)
                if name not in _FUNCTION_BUILTINS:
                    return self.special_function_call(
                        self.py_name(name), arguments_nodes
                    )
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
        if isinstance(node, AdditionOp) and not (
            _literal_number(_uncommented_expression(node.left)) is not None
            or _literal_number(_uncommented_expression(node.right)) is not None
        ):
            self.uses_add_helper = True
            return f"{self.add_helper}({self.expression(node.left)}, {self.expression(node.right)})"
        if isinstance(node, SubtractionOp) and (
            _literal_number(_uncommented_expression(node.left)) is None
            and _literal_number(_uncommented_expression(node.right)) is None
        ):
            self.uses_subtract_helper = True
            return f"{self.subtract_helper}({self.expression(node.left)}, {self.expression(node.right)})"
        if name == "MultiplicationOp" and (
            _literal_number(_uncommented_expression(node.left)) is None
            or _literal_number(_uncommented_expression(node.right)) is None
        ):
            self.uses_multiply_helper = True
            return f"{self.multiply_helper}({self.expression(node.left)}, {self.expression(node.right)})"
        if name == "DivisionOp" and (
            _literal_number(_uncommented_expression(node.left)) is None
            or _literal_number(_uncommented_expression(node.right)) is None
        ):
            self.uses_divide_helper = True
            return f"{self.divide_helper}({self.expression(node.left)}, {self.expression(node.right)})"
        if name in _BINARY_OPERATORS:
            return (
                f"(({self.expression(node.left)}) {_BINARY_OPERATORS[name]} "
                f"({self.expression(node.right)}))"
            )
        if name in {"UnaryMinusOp", "LogicalNotOp", "BitwiseNotOp"}:
            operand = self.expression(node.expr)
            if (
                name == "UnaryMinusOp"
                and _literal_number(_uncommented_expression(node.expr)) is None
            ):
                self.uses_negate_helper = True
                return f"{self.negate_helper}({operand})"
            operator = {
                "UnaryMinusOp": "-",
                "LogicalNotOp": "not ",
                "BitwiseNotOp": "~",
            }[name]
            return f"({operator}{operand})"
        if name == "TernaryOp":
            return (
                f"(({self.expression(node.true_expr)}) if "
                f"({self.expression(node.condition)}) else "
                f"({self.expression(node.false_expr)}))"
            )
        self.note_unsupported(node, f"{name} expression is not translated")
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
