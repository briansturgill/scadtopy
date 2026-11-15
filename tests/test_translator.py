from __future__ import annotations

import io
import tempfile
import unittest
import warnings
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from openscad_parser.ast.builder import Position
from openscad_parser.ast.nodes import (
    Assignment,
    BooleanLiteral,
    Identifier,
    ListCompCFor,
    ListComprehension,
    NumberLiteral,
)
from scadtopy import cli as main_module
from scadtopy import translate_file
from scadtopy.translator import TranslationError
from scadtopy.cli import main
from scadtopy.translator import _Translator


class TranslatorTests(unittest.TestCase):
    def translate(self, source: str) -> str:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "model.scad"
            path.write_text(source, encoding="utf-8")
            return translate_file(path)

    def test_translates_primitives_transforms_and_boolean_operations(self) -> None:
        generated = self.translate("""
            difference() {
                cube([10, 20, 30], center=true);
                translate([1, 2, 3]) sphere(r=4, $fn=32);
            }
            """)

        self.assertIn(
            "difference(cube([10.0, 20.0, 30.0], center=True), "
            "sphere(radius=4.0, "
            "segments=_scadtopy_calculate_segments("
            "4.0, int(32.0), _scadtopy_default_fa, _scadtopy_default_fs))."
            "translate([1.0, 2.0, 3.0]))",
            generated,
        )
        compile(generated, "<generated>", "exec")

    def test_translates_modules_and_variables(self) -> None:
        generated = self.translate("""
            size = 8;
            module box(width=size) {
                cube([width, width, width]);
            }
            box(12);
            """)

        self.assertIn("size = 8.0", generated)
        self.assertIn(
            "def box(width=size, _scadtopy_special_fn=None, "
            "_scadtopy_special_fa=None, _scadtopy_special_fs=None):",
            generated,
        )
        self.assertIn("return cube([width, width, width])", generated)
        self.assertIn(
            "box(12.0, _scadtopy_special_fn=_scadtopy_default_fn, "
            "_scadtopy_special_fa=_scadtopy_default_fa, "
            "_scadtopy_special_fs=_scadtopy_default_fs)",
            generated,
        )
        compile(generated, "<generated>", "exec")

    def test_maps_diameters_and_centered_cylinders(self) -> None:
        circle = self.translate("circle(d=10, $fn=24);")
        cylinder = self.translate("cylinder(12, 3, true);")
        tapered = self.translate("cylinder(h=12, r1=2, r2=4);")

        self.assertIn(
            "circle(radius=10.0 / 2, segments=_scadtopy_calculate_segments("
            "10.0 / 2, int(24.0), _scadtopy_default_fa, _scadtopy_default_fs))",
            circle,
        )
        self.assertIn(
            "cylinder(12.0, 3.0, segments=_scadtopy_calculate_segments("
            "3.0, _scadtopy_default_fn, _scadtopy_default_fa, "
            "_scadtopy_default_fs), center=True)",
            cylinder,
        )
        self.assertIn(
            "elliptical_cylinder(12.0, (2.0, 4.0), segments=",
            tapered,
        )
        self.assertNotIn("eliptical_cylinder", cylinder)
        compile(circle, "<generated-circle>", "exec")
        compile(cylinder, "<generated-cylinder>", "exec")
        compile(tapered, "<generated-tapered-cylinder>", "exec")

    def test_default_fn_uses_openscad_auto_segments(self) -> None:
        from piecad import Config, sphere

        generated = self.translate(
            "observed = $fn; $fa = 6; $fs = 1; sphere(5);"
            "module shape() { sphere(5); } shape();"
        )
        namespace: dict[str, object] = {}
        with (
            patch.object(Config, "get_default_segments", return_value=100),
            patch("piecad.sphere", wraps=sphere) as sphere_call,
        ):
            exec(generated, namespace)

        self.assertEqual(namespace["observed"], 0)
        self.assertEqual(
            [call.kwargs["segments"] for call in sphere_call.call_args_list],
            [32, 32],
        )

    def test_top_level_fn_sets_default_segments_global(self) -> None:
        generated = self.translate("$fn = 48; sphere(5);")

        self.assertIn("_scadtopy_default_fn = int(48.0)", generated)
        self.assertNotIn("$fn =", generated)
        self.assertIn(
            "sphere(5.0, segments=_scadtopy_calculate_segments("
            "5.0, _scadtopy_default_fn, _scadtopy_default_fa, "
            "_scadtopy_default_fs))",
            generated,
        )
        compile(generated, "<generated-default-segments>", "exec")

        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertEqual(namespace["_scadtopy_default_fn"], 48)

    def test_warns_for_other_top_level_special_settings(self) -> None:
        generated = self.translate("$fa = 3; $fs = 2; $preview = true; cube(1);")

        self.assertIn("_scadtopy_default_fa = 3.0", generated)
        self.assertIn("_scadtopy_default_fs = 2.0", generated)
        self.assertIn("# UNSUPPORTED: $preview = True", generated)
        self.assertNotIn("$fa =", generated)
        compile(generated, "<generated-special-settings>", "exec")

    def test_comments_out_unsupported_openscad_special_variables(self) -> None:
        generated = self.translate(
            "a = $t; b = $preview; c = $vpr; d = $vpt; e = $vpd; "
            "cube($t); translate($vpt) square(2);"
        )

        for variable in ("$t", "$preview", "$vpr", "$vpt", "$vpd"):
            self.assertIn(f"# UNSUPPORTED: {variable} are not translated", generated)
        self.assertNotIn("cube($t)", generated)
        self.assertNotIn("translate($vpt)", generated)
        compile(generated, "<generated-unsupported-specials>", "exec")

    def test_comments_out_unsupported_special_variables_in_definitions(self) -> None:
        generated = self.translate(
            "function animation() = $t; "
            "module animated() { cube($preview); sphere(1); } "
            "module with_default(value=$vpr) { cube(1); }"
        )

        self.assertIn(
            "def animation(*args, **kwargs):\n"
            "    # UNSUPPORTED: $t are not translated\n"
            "    return None",
            generated,
        )
        self.assertIn("# UNSUPPORTED: $preview are not translated", generated)
        self.assertNotIn("cube($preview)", generated)
        self.assertIn(
            "def with_default(*args, **kwargs):\n"
            "    # UNSUPPORTED: $vpr are not translated\n"
            "    return None",
            generated,
        )
        compile(generated, "<generated-unsupported-special-definitions>", "exec")

    def test_calculates_segments_from_min_angle_and_segment_length(self) -> None:
        generated = self.translate(
            "$fa=6; $fs=1; function min_angle()=$fa; "
            "sphere(r=5, $fn=0, $fa=min_angle(), $fs=$fs);"
        )

        self.assertIn("def min_angle():\n    return _scadtopy_default_fa", generated)
        self.assertIn(
            "segments=_scadtopy_calculate_segments("
            "5.0, int(0.0), min_angle(), _scadtopy_default_fs)",
            generated,
        )
        namespace: dict[str, object] = {}
        from piecad import Config

        original_angle = Config.get_default_min_angle()
        original_length = Config.get_default_min_segment_length()
        try:
            exec(generated, namespace)
            self.assertGreater(namespace["_parts"][0].num_verts(), 0)
            self.assertEqual(Config.calculate_default_segments(5, 6, 1), 32)
        finally:
            Config.set_default_min_angle(original_angle)
            Config.set_default_min_segment_length(original_length)

    def test_scoped_segment_settings_in_modules(self) -> None:
        generated = self.translate(
            "module lowres() { $fn=0; $fa=6; $fs=1; sphere(5); } lowres();"
        )

        self.assertIn(
            "def lowres(_scadtopy_special_fn=None, _scadtopy_special_fa=None, "
            "_scadtopy_special_fs=None):",
            generated,
        )
        self.assertIn("    _scadtopy_special_fn = int(0.0)", generated)
        self.assertIn("    _scadtopy_special_fa = 6.0", generated)
        self.assertIn("    _scadtopy_special_fs = 1.0", generated)
        self.assertIn(
            "_scadtopy_calculate_segments(5.0, _scadtopy_special_fn, "
            "_scadtopy_special_fa, _scadtopy_special_fs)",
            generated,
        )
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertGreater(namespace["_parts"][0].num_verts(), 0)

    def test_passes_segment_settings_through_user_modules(self) -> None:
        generated = self.translate(
            "module part() { sphere(2); } "
            "module wrapper() { part(); } "
            "wrapper($fn=8, $fa=6, $fs=1);"
        )

        self.assertIn("_scadtopy_special_fn=None", generated)
        self.assertIn("_scadtopy_special_fa=None", generated)
        self.assertIn("_scadtopy_special_fs=None", generated)
        self.assertIn(
            "wrapper(_scadtopy_special_fn=int(8.0), _scadtopy_special_fa=6.0, "
            "_scadtopy_special_fs=1.0)",
            generated,
        )
        self.assertIn(
            "part(_scadtopy_special_fn=_scadtopy_special_fn, "
            "_scadtopy_special_fa=_scadtopy_special_fa, "
            "_scadtopy_special_fs=_scadtopy_special_fs)",
            generated,
        )
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertGreater(namespace["_parts"][0].num_verts(), 0)

    def test_passes_through_comments_and_parser_blank_lines(self) -> None:
        generated = self.translate(
            "// header\n\n$fa = 3; // setting note\n\n"
            "// paragraph one\n\n// paragraph two\ncube(1); // geometry note"
        )

        self.assertIn("# header", generated)
        self.assertIn("_scadtopy_default_fa = 3.0  # setting note", generated)
        self.assertIn("# paragraph one", generated)
        self.assertIn("# paragraph two", generated)
        self.assertIn("_parts.append(cube(1.0))  # geometry note", generated)
        self.assertIn("# paragraph one\n\n# paragraph two", generated)
        compile(generated, "<generated-comments>", "exec")

    def test_passes_through_module_comments_and_blank_lines(self) -> None:
        generated = self.translate(
            "module shape() { // opening\n// first\n\n// second\ncube(1);\n}"
        )

        self.assertIn("# opening", generated)
        self.assertIn("# first", generated)
        self.assertIn("# second", generated)
        compile(generated, "<generated-module-comments>", "exec")

    def test_translates_polygon_paths(self) -> None:
        generated = self.translate(
            "polygon(points=[[0,0],[2,0],[1,2]], paths=[[0,1,2]]);"
        )

        self.assertIn("for index in reversed(path)", generated)
        self.assertIn("for path in [[0.0, 1.0, 2.0]]", generated)
        compile(generated, "<generated-polygon>", "exec")
        exec(generated, {})

    def test_translates_inclusive_range_literals(self) -> None:
        generated = self.translate("""
            implicit = [1:3];
            ascending = [0:2:6];
            descending = [3:-1:0];
            fractional = [0:0.1:0.3];
            fractional_descending = [0:-0.1:-0.3];
            incomplete = [0:2:5];
            incomplete_descending = [5:-2:0];
            singleton = [2:0.5:2];
            empty = [5:0];
            empty_descending = [0:-1:5];
            """)

        self.assertIn(
            "frange(1.0, 3.0, 1.0, inclusive=True)",
            generated,
        )
        self.assertNotIn("_scadtopy_start", generated)
        self.assertNotIn("_scadtopy_range", generated)
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertEqual(namespace["implicit"], [1.0, 2.0, 3.0])
        self.assertEqual(namespace["ascending"], [0.0, 2.0, 4.0, 6.0])
        self.assertEqual(namespace["descending"], [3.0, 2.0, 1.0, 0.0])
        self.assertEqual(namespace["fractional"], [0.0, 0.1, 0.2, 0.30000000000000004])
        self.assertEqual(
            namespace["fractional_descending"],
            [0.0, -0.1, -0.2, -0.30000000000000004],
        )
        self.assertEqual(namespace["incomplete"], [0.0, 2.0, 4.0])
        self.assertEqual(namespace["incomplete_descending"], [5.0, 3.0, 1.0])
        self.assertEqual(namespace["singleton"], [2.0])
        self.assertEqual(namespace["empty"], [])
        self.assertEqual(namespace["empty_descending"], [])

    def test_range_rejects_zero_step(self) -> None:
        generated = self.translate("values = [1:0:3];")
        with self.assertRaises(ValueError):
            exec(generated, {})

    def test_range_with_tiny_negative_distance_is_empty(self) -> None:
        generated = self.translate("function values(a, step, b) = [a:step:b];")
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertEqual(namespace["values"](5e-324, 1, 0), [])

    def test_range_arguments_are_evaluated_once_in_python_order(self) -> None:
        generated = self.translate("""
            function value(x) = echo(x) x;
            values = [value(0):value(2):value(6)];
            """)
        output = io.StringIO()
        namespace: dict[str, object] = {}
        with redirect_stdout(output):
            exec(generated, namespace)
        self.assertEqual(output.getvalue(), "0.0\n6.0\n2.0\n")
        self.assertEqual(namespace["values"], [0, 2, 4, 6])

    def test_translates_functions_let_assert_and_function_literals(self) -> None:
        generated = self.translate("""
            function twice(value=2) = value * 2;
            result = let(base=twice()) base + 1;
            callback = function(value) value + 3;
            checked = assert(result == 5) callback(4);
            """)

        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertEqual(namespace["result"], 5.0)
        self.assertEqual(namespace["checked"], 7.0)
        self.assertEqual(namespace["callback"](2), 5)

    def test_calls_through_callable_expressions(self) -> None:
        generated = self.translate("""
            functions = [function(x, y=2) x + y, function(x) x * 2];
            function make() = function(x) x + 1;
            indexed = functions[0](x=3);
            dynamic = functions[1 + 0](4);
            returned = make()(5);
            conditional = (true ? functions[0] : functions[1])(6);
            indexed_conditional = (true ? [functions[0]] : [functions[1]])[0](8);
            literal = (function(x) x * 3)(7);
            """)

        self.assertNotIn("FIXME", generated)
        compile(generated, "<generated-callables>", "exec")
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertEqual(namespace["indexed"], 5)
        self.assertEqual(namespace["dynamic"], 8)
        self.assertEqual(namespace["returned"], 6)
        self.assertEqual(namespace["conditional"], 8)
        self.assertEqual(namespace["indexed_conditional"], 10)
        self.assertEqual(namespace["literal"], 21)

    def test_expression_assertion_is_lazy_and_fails_explicitly(self) -> None:
        generated = self.translate("result = assert(false) missing_function();")

        self.assertIn("assert False", generated)
        self.assertNotIn("_scadtopy_assert(", generated)
        with self.assertRaises(AssertionError):
            exec(generated, {})

    def test_assertions_use_python_statements_in_modules_and_expressions(self) -> None:
        generated = self.translate("""
            assert(true, "top");
            function checked(x) = let(y = x + 1) assert(y > 0, "bad") y;
            function direct(x) = assert(x > 0, "direct") x;
            values = [for (i = [1, 2, 3]) assert(i > 0) i * 2];
            callback = function(x) assert(x > 0) x;
            module shape() {
                assert(true, "module");
                assert(true) cube(2);
            }
            shape();
            """)

        self.assertNotIn("def _scadtopy_assert(", generated)
        self.assertIn("assert True, 'top'", generated)
        self.assertIn("assert True, 'module'", generated)
        self.assertIn(
            "def checked(x):\n    y = x + 1.0\n    assert y > 0.0, 'bad'\n    return y",
            generated,
        )
        self.assertIn("def direct(x):\n    assert x > 0.0, 'direct'", generated)
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertEqual(namespace["checked"](2), 3)
        self.assertEqual(namespace["values"], [2, 4, 6])
        self.assertEqual(namespace["callback"](4), 4)
        with self.assertRaisesRegex(AssertionError, "bad"):
            namespace["checked"](-2)
        with self.assertRaisesRegex(AssertionError, "direct"):
            namespace["direct"](0)
        with self.assertRaises(AssertionError):
            namespace["callback"](0)
        self.assertEqual(namespace["_parts"][0].bounding_box(), (0, 0, 0, 2, 2, 2))

    def test_function_let_assertion_preserves_outer_initializer_scope(self) -> None:
        generated = self.translate("""
            y = 4;
            function checked() = let(y = y + 1) assert(y > 0) y;
            result = checked();
            """)
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertEqual(namespace["result"], 5)

    def test_assertions_only_evaluate_selected_branches_and_failure_messages(
        self,
    ) -> None:
        generated = self.translate("""
            result = true ? 5 : assert(false) missing_function();
            checked = assert(true, missing_function()) 7;
            """)
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertEqual(namespace["result"], 5)
        self.assertEqual(namespace["checked"], 7)

    def test_nested_geometry_assertions_preserve_loop_scope_and_order(self) -> None:
        generated = self.translate("""
            module shape() {
                for (i = [1, 2]) {
                    assert(i > 0, "bad");
                    cube(i);
                    assert(i < 3, "bad");
                }
            }
            shape();
            """)
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertEqual(namespace["_parts"][0].bounding_box(), (0, 0, 0, 2, 2, 2))

    def test_module_assertion_runs_between_geometry_calls(self) -> None:
        from piecad import cube

        generated = self.translate("""
            module shape() {
                cube(1);
                assert(false, "stop");
                cube(2);
            }
            shape();
            """)
        with patch("piecad.cube", wraps=cube) as cube_call:
            with self.assertRaisesRegex(AssertionError, "stop"):
                exec(generated, {})
        cube_call.assert_called_once_with(1.0)

    def test_nested_assertions_keep_local_bindings(self) -> None:
        generated = self.translate("""
            function check(offset) =
                [for (i = [1, 2]) let(x = i + offset)
                    assert(x > 0) assert(x < 10) x];
            callback = function(x) let(y = x + 1) assert(y > 0) y;
            """)
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertEqual(namespace["check"](2), [3, 4])
        self.assertEqual(namespace["callback"](2), 3)
        with self.assertRaises(AssertionError):
            namespace["check"](-2)
        with self.assertRaises(AssertionError):
            namespace["check"](9)

    def test_nested_assertion_chain_uses_one_function(self) -> None:
        generated = self.translate("""
            values = [for (x = [1, 2]) assert(x > 0) assert(x < 3) echo(x) x];
            """)
        self.assertEqual(generated.count("def _scadtopy_assert_"), 1)
        self.assertIn("    print(x)\n    return x", generated)
        output = io.StringIO()
        namespace: dict[str, object] = {}
        with redirect_stdout(output):
            exec(generated, namespace)
        self.assertEqual(namespace["values"], [1, 2])
        self.assertEqual(output.getvalue(), "1.0\n2.0\n")

    def test_module_assertion_with_unsupported_special_variable_is_reported(
        self,
    ) -> None:
        generated = self.translate("module shape() { assert($t > 0); cube(1); }")
        self.assertIn("# UNSUPPORTED: $t are not translated", generated)
        compile(generated, "<generated-assert>", "exec")

    def test_expression_echo_prints_arguments_and_returns_body_value(self) -> None:
        generated = self.translate('result = echo("number", 3, status=true) 7;')

        output = io.StringIO()
        namespace: dict[str, object] = {}
        with redirect_stdout(output):
            exec(generated, namespace)

        self.assertIn("print('number', 3.0, 'status =', True)", generated)
        self.assertNotIn("_scadtopy_echo", generated)
        self.assertEqual(output.getvalue(), "number 3.0 status = True\n")
        self.assertEqual(namespace["result"], 7.0)

    def test_echo_and_assert_chains_become_statements(self) -> None:
        generated = self.translate("""
            function checked(x) = echo("first", x) assert(x > 0) echo("last") x;
            result = checked(2);
            echo("outer") assert(true) cube(1);
            """)
        self.assertIn("    print('first', x)\n    assert x > 0.0", generated)
        self.assertIn("    print('last')\n    return x", generated)
        self.assertIn("print('outer')\nassert True", generated)
        self.assertNotIn("_scadtopy_assert_", generated)
        self.assertNotIn("_scadtopy_echo", generated)
        output = io.StringIO()
        namespace: dict[str, object] = {}
        with redirect_stdout(output):
            exec(generated, namespace)
        self.assertEqual(output.getvalue(), "first 2.0\nlast\nouter\n")
        self.assertEqual(namespace["result"], 2)

    def test_nested_echo_preserves_lazy_branches_and_iteration(self) -> None:
        generated = self.translate("""
            values = [for (x = [1, 2]) echo(x) x * 2];
            skipped = true ? 3 : echo("wrong") 4;
            callback = function(x) echo(x) x + 1;
            """)
        self.assertNotIn("_scadtopy_echo", generated)
        output = io.StringIO()
        namespace: dict[str, object] = {}
        with redirect_stdout(output):
            exec(generated, namespace)
            self.assertEqual(namespace["callback"](5), 6)
        self.assertEqual(output.getvalue(), "1.0\n2.0\n5\n")
        self.assertEqual(namespace["values"], [2, 4])
        self.assertEqual(namespace["skipped"], 3)

    def test_echo_uses_builtin_print_when_user_shadows_it(self) -> None:
        generated = self.translate("""
            function print(x) = x + 1;
            values = [for (print = [1, 2]) echo(print) print];
            result = print(3);
            """)
        output = io.StringIO()
        namespace: dict[str, object] = {}
        with redirect_stdout(output):
            exec(generated, namespace)
        self.assertEqual(output.getvalue(), "1.0\n2.0\n")
        self.assertEqual(namespace["values"], [1, 2])
        self.assertEqual(namespace["result"], 4)

    def test_module_echo_runs_between_geometry_calls(self) -> None:
        from piecad import cube

        generated = self.translate("""
            module shape() { cube(1); echo("middle"); cube(2); }
            shape();
            """)
        events = []

        def track_cube(size: float) -> object:
            events.append(f"cube {size}")
            return cube(size)

        with (
            patch("piecad.cube", side_effect=track_cube),
            patch(
                "builtins.print",
                side_effect=lambda *args: events.append(" ".join(args)),
            ),
        ):
            exec(generated, {})
        self.assertEqual(events, ["cube 1.0", "middle", "cube 2.0"])

    def test_translates_list_comprehension_loops_and_conditions(self) -> None:
        generated = self.translate("""
            doubled = [for(value=[0:3]) value * 2];
            even = [for(value=[0:4]) if(value % 2 == 0) value];
            selected = [if(true) 3 else 4];
            """)

        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertEqual(namespace["doubled"], [0.0, 2.0, 4.0, 6.0])
        self.assertEqual(namespace["even"], [0.0, 2.0, 4.0])
        self.assertEqual(namespace["selected"], [3.0])

    def test_marks_unmapped_comprehension_and_translates_each(self) -> None:
        generated = self.translate("flattened = [each [1, 2, 3]];")

        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertEqual(namespace["flattened"], [1.0, 2.0, 3.0])

    def test_marks_c_style_comprehension_node_for_follow_up(self) -> None:
        position = Position("test.scad", 1, 1)
        node = Assignment(
            position=position,
            name=Identifier(position=position, name="values"),
            expr=ListComprehension(
                position=position,
                elements=[
                    ListCompCFor(
                        position=position,
                        inits=[],
                        condition=BooleanLiteral(position=position, val=True),
                        incrs=[],
                        body=NumberLiteral(position=position, val=1),
                    )
                ],
            ),
        )

        generated = _Translator().translate([node])

        self.assertIn("pass  # FIXME: C-style list comprehension", generated)
        compile(generated, "<generated-c-style-comprehension>", "exec")

    def test_translates_module_loops_conditionals_and_let(self) -> None:
        generated = self.translate("""
            module repeated(count) {
                for(value=[0:count-1]) translate([value, 0, 0]) cube(1);
            }
            if(true) repeated(3); else sphere(1);
            let(size=2) cube(size);
            """)

        compile(generated, "<generated-modules>", "exec")
        self.assertIn("union(*[", generated)
        self.assertIn("if True else", generated)
        self.assertIn("lambda size:", generated)

    def test_translates_remaining_module_nodes_and_render_expression(self) -> None:
        generated = self.translate("""
            rendered = render() { cube(1); };
            echo(1) cube(2);
            assert(true) cube(3);
            intersection_for(i=[0:1]) translate([i, 0, 0]) cube(1);
            #sphere(1);
            %cube(1);
            *cube(1);
            """)

        compile(generated, "<generated-module-nodes>", "exec")
        self.assertIn("rendered = cube(1.0)", generated)
        self.assertNotIn("FIXME: module echo", generated)
        self.assertNotIn("FIXME", generated)
        namespace: dict[str, object] = {}
        output = io.StringIO()
        with redirect_stdout(output):
            exec(generated, namespace)
        self.assertIsNotNone(namespace["_parts"][0])
        self.assertEqual(output.getvalue(), "1.0\n")

    def test_module_echo_prints_arguments_and_returns_child_geometry(self) -> None:
        generated = self.translate('echo("shape", count=2) cube(1);')

        output = io.StringIO()
        namespace: dict[str, object] = {}
        with redirect_stdout(output):
            exec(generated, namespace)

        self.assertEqual(output.getvalue(), "shape count = 2.0\n")
        self.assertEqual(namespace["_parts"][0].bounding_box(), (0, 0, 0, 1, 1, 1))

    def test_standalone_module_echo_does_not_create_a_model(self) -> None:
        generated = self.translate('echo("standalone");')

        output = io.StringIO()
        namespace: dict[str, object] = {}
        with redirect_stdout(output):
            exec(generated, namespace)

        self.assertEqual(output.getvalue(), "standalone\n")
        self.assertEqual(namespace["_parts"], [])

    def test_translates_polyhedron_faces_for_piecad(self) -> None:
        generated = self.translate("""
            polyhedron(
                points=[[0,0,0],[10,0,0],[0,10,0],[0,0,10]],
                faces=[[0,1,2],[0,3,1],[0,2,3],[1,3,2]]
            );
            """)

        self.assertIn("face[0], face[i + 1], face[i]", generated)
        self.assertIn("check='repair'", generated)
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertFalse(namespace["_parts"][0].is_empty())
        self.assertGreater(namespace["_parts"][0].volume(), 0)

    def test_translates_t_scad_geometry_end_to_end(self) -> None:
        generated = self.translate("""
            $fn = 24;
            pi2 = PI * 2;
            function root(value) = sqrt(value);
            assert(root(4) == 2);
            translate([0, 0, 0])
                linear_extrude(5)
                    polygon(
                        points=[[0,0],[20,0],[10,15]],
                        paths=[[0,1,2]]
                    );
            translate([30, 0, 0])
                polyhedron(
                    points=[[0,0,0],[10,0,0],[0,10,0],[0,0,10]],
                    faces=[[0,1,2],[0,3,1],[0,2,3],[1,3,2]]
                );
            translate([60, 0, 0]) cylinder(h=10, r1=1, r2=2);
            translate([90, 0, 0]) minkowski() { cube(1); sphere(1); }
            echo("done");
            """)

        self.assertIn("math.pi", generated)
        self.assertIn("math.sqrt(", generated)
        self.assertIn(".minkowski_sum(", generated)
        self.assertNotIn("child geometry for module 'minkowski'", generated)
        self.assertIn("for index in reversed(path)", generated)
        self.assertIn("face[i + 1], face[i]", generated)
        self.assertIn("elliptical_cylinder(", generated)
        compile(generated, "<generated-t-scad>", "exec")

        output = io.StringIO()
        namespace: dict[str, object] = {"__name__": "t_scad_test"}
        from piecad import Config

        original_segments = Config.get_default_segments()
        try:
            with redirect_stdout(output):
                exec(generated, namespace)
            model = namespace["_parts"][0]
            self.assertFalse(model.is_empty())
            self.assertGreater(model.volume(), 0)
        finally:
            Config.set_default_segments(original_segments)

        self.assertIn("done", output.getvalue())

    def test_translates_children_and_unions_module_bodies(self) -> None:
        generated = self.translate("""
            module repeated(count=3) {
                for(i=[0:count-1]) translate([i*10,0,0]) children();
            }
            module pair() { cube(1); sphere(1); }
            module first() { children(0); }
            repeated(3) { cube(1); sphere(1); }
            translate([0,20,0]) pair();
            translate([0,40,0]) first() { cube(2); sphere(5); }
            """)

        self.assertIn(
            "def repeated(count=3.0, _scadtopy_special_fn=None, "
            "_scadtopy_special_fa=None, _scadtopy_special_fs=None, _children=()):",
            generated,
        )
        self.assertIn("_children=[", generated)
        self.assertNotIn("children() substitution", generated)
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        # Three copies of a cube+sphere union spaced 10 apart.
        x1, _, _, x2, _, _ = namespace["repeated"](
            3, _children=[namespace["cube"](1), namespace["sphere"](1)]
        ).bounding_box()
        self.assertAlmostEqual(x2 - x1, 22, delta=1.1)
        self.assertEqual(
            namespace["first"](_children=[namespace["cube"](2)]).bounding_box(),
            (0, 0, 0, 2, 2, 2),
        )
        self.assertFalse(namespace["_parts"][0].is_empty())

    def test_translates_children_selectors_and_count(self) -> None:
        generated = self.translate("""
            module pick() { children([0:1]); translate([20,0,0]) children([2]); }
            module count() { translate([0,0,$children]) children(0); }
            module outer() { module inner() { children(1); } inner() { cube(1); cube(3); } }
            pick() { cube(1); cube(2); cube(3); }
            count() { cube(1); cube(1); }
            outer();
            """)

        self.assertNotIn("FIXME", generated)
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        kids = [namespace["cube"](1), namespace["cube"](2), namespace["cube"](3)]
        box = namespace["pick"](_children=kids).bounding_box()
        self.assertAlmostEqual(box[3], 23)
        box = namespace["count"](_children=kids[:2]).bounding_box()
        self.assertAlmostEqual(box[2], 2)
        self.assertAlmostEqual(namespace["outer"]().bounding_box()[3], 3)
        with self.assertWarns(UserWarning):
            namespace["_scadtopy_children"](kids, 7)

    def test_translates_offset_radial_and_delta(self) -> None:
        generated = self.translate("""
            offset(r=2) square([10,10],center=true);
            translate([30,0,0]) offset(delta=1) square(4);
            """)

        self.assertNotIn("FIXME", generated)
        self.assertIn(
            ".offset(2.0, 'round', segments=_scadtopy_calculate_segments("
            "2.0, _scadtopy_default_fn, _scadtopy_default_fa, "
            "_scadtopy_default_fs))",
            generated,
        )
        self.assertIn(".offset(1.0, 'miter', 1000.0)", generated)
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        box = namespace["square"](10, center=True).offset(2, "round").bounding_box()
        self.assertAlmostEqual(box[2], 7)

    def test_user_function_named_like_piecad_name_does_not_shadow(self) -> None:
        generated = self.translate(
            "function square(x) = x * x;\n"
            "translate([square(2),0,0]) cube(1);\n"
            "linear_extrude(height=1) square(3);\n"
        )

        self.assertIn("def square_(x):", generated)
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertEqual(namespace["square_"](3), 9)
        self.assertEqual(namespace["_parts"][0].bounding_box()[3], 5.0)

    def test_forward_function_and_module_declarations(self) -> None:
        generated = self.translate("""
            width = 4;
            diameter = square(2);
            translate([diameter, 0, 0]) cube(1);
            future_shape();
            module future_shape(size=width) { cube(size * square(3)); }
            function square(value) = value * value;
            """)

        self.assertLess(
            generated.index("def future_shape("),
            generated.index("diameter ="),
        )
        self.assertLess(
            generated.index("def square_("),
            generated.index("diameter ="),
        )
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        parts = namespace["_parts"]
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0].bounding_box(), (4.0, 0.0, 0.0, 5.0, 1.0, 1.0))
        self.assertEqual(parts[1].bounding_box(), (0.0, 0.0, 0.0, 36.0, 36.0, 36.0))

    def test_ignores_convexity_and_nonexistent_center_arguments(self) -> None:
        generated = self.translate(
            "polygon(points=[[0,0],[4,0],[0,4]], convexity=2);\n"
            "translate([1,2,3], center=true) cube(1);\n"
            "circle(center=true, r=2);\n"
            "sphere(r=1, center=true);\n"
            "scale([2,2,2], center=true) cube(1);\n"
            "module custom(center=false) { cube(center ? 2 : 1, center=center); }\n"
            "custom(center=true);\n"
        )

        self.assertNotIn("FIXME", generated)
        self.assertNotIn("convexity", generated)
        self.assertNotIn("circle(center", generated)
        self.assertNotIn("sphere(center", generated)
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        parts = namespace["_parts"]
        self.assertEqual(parts[1].bounding_box(), (1.0, 2.0, 3.0, 2.0, 3.0, 4.0))
        self.assertAlmostEqual(parts[2].bounding_box()[0], -2.0, places=1)
        self.assertEqual(parts[4].bounding_box(), (0.0, 0.0, 0.0, 2.0, 2.0, 2.0))
        # A user module's own `center` parameter must still be passed through.
        self.assertEqual(parts[5].bounding_box(), (-1.0, -1.0, -1.0, 1.0, 1.0, 1.0))

    def test_cylinder_height_defaults_to_one(self) -> None:
        generated = self.translate("cylinder(r=2);")

        self.assertNotIn("FIXME", generated)
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertAlmostEqual(namespace["_parts"][0].bounding_box()[5], 1.0)

    def test_required_parameter_after_default_gets_undef_default(self) -> None:
        generated = self.translate(
            "module m(a, b=2, c) { cube(a + b); }\n"
            "function f(a=1, b) = a;\n"
            "m(1); echo(f());\n"
        )

        self.assertIn("def m(a, b=2.0, c=None", generated)
        self.assertIn("def f(a=1.0, b=None)", generated)
        compile(generated, "<generated-parameters>", "exec")

    def test_comment_before_named_argument_name(self) -> None:
        generated = self.translate(
            "module box(size=1) { cube(size); }\n" "box(/* the size */ size = 3);\n"
        )

        self.assertNotIn("FIXME", generated)
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertEqual(
            namespace["_parts"][0].bounding_box(), (0.0, 0.0, 0.0, 3.0, 3.0, 3.0)
        )

    def test_forward_module_dependency_is_hoisted(self) -> None:
        generated = self.translate("""
            module outer_shape() { inner_shape(); }
            outer_shape();
            module inner_shape() { cube(2); }
            """)

        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertEqual(
            namespace["_parts"][0].bounding_box(),
            (0.0, 0.0, 0.0, 2.0, 2.0, 2.0),
        )

    def test_translates_twisted_linear_extrude_and_rotate_extrude(self) -> None:
        generated = self.translate("""
            linear_extrude(height=10, twist=90, slices=10, scale=2) square([8, 2], center=true);
            linear_extrude(height=10, twist=-90, slices=10, scale=2) square([8, 2], center=true);
            translate([50,0,0]) rotate_extrude(angle=270) translate([15,0,0]) circle(2);
            translate([0,50,0]) rotate_extrude() translate([5,0]) circle(1);
            """)

        self.assertNotIn("FIXME", generated)
        self.assertNotIn("_scadtopy_linear_extrude", generated)
        self.assertIn(
            "extrude_transforming(rectangle([8.0, 2.0], center=True)"
            ".mirror((True, False)), 10.0, 10, 90.0, (2.0, 2.0))"
            ".mirror((True, False, False))",
            generated,
        )
        self.assertIn(
            "extrude_transforming(rectangle([8.0, 2.0], center=True), "
            "10.0, 10, 90.0, (2.0, 2.0))",
            generated,
        )
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        cw, ccw = namespace["_parts"][:2]
        self.assertAlmostEqual(cw.bounding_box()[5], 10)
        # Opposite twist directions are mirror images across the X axis.
        self.assertAlmostEqual(cw.bounding_box()[1], -ccw.bounding_box()[4], places=3)
        self.assertFalse(cw.is_empty())

    def test_linear_extrude_direct_scale_and_center(self) -> None:
        generated = self.translate("""
            linear_extrude(height=10, center=true, scale=[2, 3]) square(2);
            linear_extrude(height=4, slices=5) square(1);
            """)

        self.assertNotIn("_scadtopy_linear_extrude", generated)
        self.assertIn(
            "extrude_transforming(square(2.0), 10.0, 1, 0, (2.0, 3.0))"
            ".translate([0, 0, -10.0 / 2])",
            generated,
        )
        self.assertIn("extrude_transforming(square(1.0), 4.0, 5, 0, (1, 1))", generated)
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertEqual(namespace["_parts"][0].bounding_box(), (0, 0, -5, 4, 6, 5))

    def test_linear_extrude_dynamic_arguments_keep_adapter(self) -> None:
        generated = self.translate("""
            module part(angle, count, factor) {
                linear_extrude(height=10, twist=angle, slices=count, scale=factor)
                    square([8, 2], center=true);
            }
            part(90, 10, 2);
            part(-90, 10, [2, 3]);
            """)

        self.assertIn("_scadtopy_linear_extrude(", generated)
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertEqual(len(namespace["_parts"]), 2)
        self.assertTrue(all(not part.is_empty() for part in namespace["_parts"]))

    def test_linear_extrude_default_twist_slices_keep_adapter(self) -> None:
        generated = self.translate(
            "linear_extrude(height=10, twist=90, slices=0) square(2);"
        )

        self.assertIn("_scadtopy_linear_extrude(", generated)
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertFalse(namespace["_parts"][0].is_empty())

    def test_linear_extrude_vector_scale_evaluates_elements_once(self) -> None:
        generated = self.translate("""
            function factor() = echo("scale") 2;
            linear_extrude(height=10, scale=[factor(), 3]) square(2);
            """)

        self.assertNotIn("_scadtopy_linear_extrude", generated)
        output = io.StringIO()
        with redirect_stdout(output):
            exec(generated, {})
        self.assertEqual(output.getvalue(), "scale\n")

    def test_linear_extrude_special_settings_apply_to_slices_and_profile(self) -> None:
        generated = self.translate(
            "module profile() { circle(2); } "
            "linear_extrude(height=10, twist=360, $fn=12, $fa=6, $fs=1) "
            "profile();"
        )

        self.assertIn(
            "profile(_scadtopy_special_fn=int(12.0), _scadtopy_special_fa=6.0, "
            "_scadtopy_special_fs=1.0)",
            generated,
        )
        self.assertNotIn("_scadtopy_linear_extrude", generated)
        self.assertIn(".mirror((True, False)), 10.0, 12, 360.0, (1, 1))", generated)
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertGreater(namespace["_parts"][0].num_verts(), 0)

    def test_rotate_extrude_uses_profile_radius_and_special_settings(self) -> None:
        generated = self.translate(
            "rotate_extrude($fa=6, $fs=1) " "translate([5,10]) circle(2);"
        )

        self.assertIn(
            "max(abs(_scadtopy_profile.bounding_box()[0]), "
            "abs(_scadtopy_profile.bounding_box()[2]))",
            generated,
        )
        self.assertIn(
            "_scadtopy_calculate_segments(max(abs(_scadtopy_profile",
            generated,
        )

    def test_translates_text_alignment(self) -> None:
        generated = self.translate(
            'text("Hig", size=10, halign="center", valign="top");\n'
            'translate([0,20,0]) text("Hig", 10, halign="right", valign="bottom");\n'
        )

        self.assertNotIn("FIXME", generated)
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        helper = namespace["_scadtopy_text"]
        box = helper("Hig", 10, "center", "top").bounding_box()
        self.assertAlmostEqual(box[0], -box[2])
        self.assertAlmostEqual(box[3], 0)
        box = helper("Hig", 10, "right", "bottom").bounding_box()
        self.assertAlmostEqual(box[2], 0)
        self.assertAlmostEqual(box[1], 0)
        plain = namespace["text"](size=10, text="Hig").bounding_box()
        left = helper("Hig", 10, "left", "baseline").bounding_box()
        self.assertEqual(plain, left)

    def test_collects_each_part_for_viewing(self) -> None:
        generated = self.translate("cube(1);\ntranslate([5,0,0]) sphere(2);\n")

        self.assertIn("_parts = []", generated)
        self.assertIn("view(_part)", generated)
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        parts = namespace["_parts"]
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[1].bounding_box()[0], 3.0)

    def test_translate_of_2d_shape_drops_z_component(self) -> None:
        generated = self.translate(
            "translate([1,2,3]) square(2);\ntranslate([1,2,3]) cube(2);\n"
        )

        self.assertIn("square(2.0).translate([1.0, 2.0])", generated)
        self.assertIn("cube(2.0).translate([1.0, 2.0, 3.0])", generated)
        self.assertNotIn("_scadtopy_translate", generated)
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertEqual(len(namespace["_parts"]), 2)

    def test_translate_runtime_type_handles_user_modules_returning_2d(self) -> None:
        generated = self.translate(
            "module flat() square(2); translate([1, 2, 3]) flat();"
        )
        self.assertNotIn("_scadtopy_translate", generated)
        self.assertIn(".translate([1.0, 2.0])", generated)
        namespace: dict[str, object] = {}
        exec(generated, namespace)

        model = namespace["_parts"][0]
        self.assertEqual(model.bounding_box(), (1.0, 2.0, 3.0, 4.0))

    def test_translate_preserves_dynamic_dimensions_and_vector_evaluation(self) -> None:
        generated = self.translate("""
            module shape(flat) { if (flat) square(2); else cube(2); }
            function value() = echo("z") 3;
            vector = [1, 2, 3];
            translate(vector) square(2);
            translate([1, 2, value()]) square(2);
            translate(vector) shape(true);
            translate(vector) shape(false);
            """)
        self.assertIn("square(2.0).translate(vector[:2])", generated)
        self.assertIn("_scadtopy_translate(shape(", generated)
        output = io.StringIO()
        namespace: dict[str, object] = {}
        with redirect_stdout(output):
            exec(generated, namespace)
        self.assertEqual(output.getvalue(), "z\n")
        self.assertEqual(
            [part.bounding_box() for part in namespace["_parts"]],
            [(1, 2, 3, 4), (1, 2, 3, 4), (1, 2, 3, 4), (1, 2, 3, 3, 4, 5)],
        )

    def test_translate_known_composite_dimensions(self) -> None:
        generated = self.translate("""
            translate([1,2,3]) union() { square(1); circle(1); }
            translate([1,2,3]) projection() cube(2);
            translate([1,2,3]) linear_extrude(2) square(1);
            """)
        self.assertNotIn("_scadtopy_translate", generated)
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertEqual(len(namespace["_parts"][0].bounding_box()), 4)
        self.assertEqual(len(namespace["_parts"][1].bounding_box()), 4)
        self.assertEqual(namespace["_parts"][2].bounding_box(), (1, 2, 3, 2, 3, 5))

    def test_translate_infers_nested_user_modules_and_matching_branches(self) -> None:
        generated = self.translate("""
            module flat(flag) { if (flag) square(2); else circle(2); }
            module wrapped() { flat(true); }
            translate([1,2,3]) wrapped();
            """)
        self.assertNotIn("_scadtopy_translate", generated)
        self.assertIn(".translate([1.0, 2.0])", generated)
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertEqual(namespace["_parts"][0].bounding_box(), (1, 2, 3, 4))

    def test_translate_infers_import_dimensions(self) -> None:
        from piecad import cube, square

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            generated = self.translate("""
                translate([1,2,3]) import("drawing.svg");
                translate([1,2,3]) import("part.stl");
                """)
        self.assertIn("load('drawing.svg').translate([1.0, 2.0])", generated)
        self.assertIn("load('part.stl').translate([1.0, 2.0, 3.0])", generated)
        with patch("piecad.load", side_effect=[square(1), cube(1)]):
            namespace: dict[str, object] = {}
            exec(generated, namespace)
        self.assertEqual(namespace["_parts"][0].bounding_box(), (1, 2, 2, 3))
        self.assertEqual(namespace["_parts"][1].bounding_box(), (1, 2, 3, 2, 3, 4))

    def test_runtime_geometry_type_selects_auto_save_format(self) -> None:
        cases = (
            ("module flat() square(2); flat();", "model.svg"),
            ("module solid() cube(2); solid();", "model.3mf"),
            (
                "module flat() square(2); module solid() cube(2); flat(); solid();",
                "model.3mf",
            ),
        )
        for source, expected_filename in cases:
            with self.subTest(expected_filename=expected_filename, source=source):
                generated = self.translate(source)
                with (
                    patch("piecad.save") as save,
                    patch("piecad.view"),
                    patch("piecad.view_all_now"),
                ):
                    exec(generated, {"__name__": "__main__"})

                self.assertEqual(save.call_args.args[0], expected_filename)

    def test_use_imports_translated_module_and_knows_its_modules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "lib.scad").write_text(
                "module peg() { cube(1); }", encoding="utf-8"
            )
            main_file = Path(directory) / "main.scad"
            main_file.write_text("use <lib.scad>\npeg();", encoding="utf-8")
            generated = translate_file(main_file)

        self.assertIn("from lib import *", generated)
        self.assertIn(
            "peg(_scadtopy_special_fn=_scadtopy_default_fn, "
            "_scadtopy_special_fa=_scadtopy_default_fa, "
            "_scadtopy_special_fs=_scadtopy_default_fs)",
            generated,
        )

    def test_warns_about_use_and_import_paths_with_source_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "main.scad"
            source.write_text(
                'use <lib.scad>;\nimport("assets/part.stl");', encoding="utf-8"
            )
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                generated = translate_file(source)

        self.assertEqual(len(caught), 2)
        self.assertTrue(all(warning.filename == str(source) for warning in caught))
        self.assertEqual([warning.lineno for warning in caught], [1, 2])
        messages = [str(warning.message) for warning in caught]
        self.assertIn("use <lib.scad>", messages[0])
        self.assertIn("translate the used .scad file separately", messages[0])
        self.assertIn('import("assets/part.stl")', messages[1])
        self.assertIn("Python process working directory", messages[1])
        self.assertIn("from lib import *", generated)
        self.assertIn("load('assets/part.stl')", generated)

    def test_include_is_expanded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "inc.scad").write_text("size = 3;", encoding="utf-8")
            main_file = Path(directory) / "main.scad"
            main_file.write_text("include <inc.scad>\ncube(size);", encoding="utf-8")
            generated = translate_file(main_file)

        self.assertIn("size = 3", generated)
        self.assertNotIn("FIXME", generated)

    def test_builtin_functions_map_to_piecad_and_math(self) -> None:
        generated = self.translate(
            "a = sin(30) + floor(2.5) + norm([3,4]);"
            "c = cross([1,0,0],[0,1,0]); d = cross([1,0],[0,1]);"
            "function ceil(x) = x; e = ceil(1.5);"
        )

        self.assertIn("sin(30.0)", generated)
        self.assertIn("math.floor(2.5)", generated)
        self.assertIn("math.hypot(*[3.0, 4.0])", generated)
        self.assertIn("from piecad.lin_math import Vec2", generated)
        self.assertIn("Vec3.cross(", generated)
        self.assertIn("Vec2.cross([1.0, 0.0], [0.0, 1.0])[2]", generated)
        self.assertIn("e = ceil(1.5)", generated)

    def test_sign_maps_to_piecad(self) -> None:
        generated = self.translate(
            "negative = sign(-3); zero = sign(0); positive = sign(3);"
        )

        self.assertIn("negative = sign(-3.0)", generated)
        self.assertIn("zero = sign(0.0)", generated)
        self.assertIn("positive = sign(3.0)", generated)
        self.assertNotIn("_scadtopy_sign", generated)
        compile(generated, "<generated-sign>", "exec")
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertEqual(
            (namespace["negative"], namespace["zero"], namespace["positive"]),
            (-1, 0, 1),
        )

    def test_vector_members_map_to_tuple_indexes(self) -> None:
        generated = self.translate(
            "v = [1, 2, 3]; a = v.x; b = v.y; c = v.z; d = [4, 5].y;"
        )

        self.assertIn("a = v[0]", generated)
        self.assertIn("b = v[1]", generated)
        self.assertIn("c = v[2]", generated)
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertEqual(
            (namespace["a"], namespace["b"], namespace["c"], namespace["d"]),
            (1.0, 2.0, 3.0, 5.0),
        )

    def test_group_and_modifiers(self) -> None:
        generated = self.translate(
            "group() { cube(1); sphere(1); } *cube(5); #cube(2); %sphere(2);"
        )

        self.assertIn(
            "_parts.append(union(cube(1.0), sphere(1.0, "
            "segments=_scadtopy_calculate_segments(1.0, _scadtopy_default_fn, "
            "_scadtopy_default_fa, _scadtopy_default_fs))))",
            generated,
        )
        self.assertIn("_parts.append(cube(2.0))", generated)
        self.assertIn(
            "_parts.append(sphere(2.0, segments=_scadtopy_calculate_segments("
            "2.0, _scadtopy_default_fn, _scadtopy_default_fa, "
            "_scadtopy_default_fs)))",
            generated,
        )
        self.assertNotIn("5.0", generated)
        self.assertNotIn("FIXME", generated)

    def test_show_only_modifier_is_fatal(self) -> None:
        with self.assertRaisesRegex(
            TranslationError, r"Show only \('!'\) is not allowed at line: 2"
        ):
            self.translate("cube(1);\n!cube(2);")

    def test_easy_builtins(self) -> None:
        generated = self.translate(
            "a = is_undef(1); b = concat([1],[2]); c = round(2.5); d = sign(-1);"
            'e = str(1, "x"); f = rands(0,1,2); g = lookup(1,[[0,0],[2,2]]);'
        )

        self.assertIn("a = 1.0 is None", generated)
        self.assertIn("b = [1.0] + [2.0]", generated)
        for helper in ("round", "str", "rands", "lookup"):
            self.assertIn(f"def _scadtopy_{helper}(", generated)
        self.assertIn("d = sign(-1.0)", generated)
        self.assertNotIn("_scadtopy_sign", generated)
        self.assertIn("import random", generated)

    def test_search_builtin_matches_openscad_search_forms(self) -> None:
        generated = self.translate("""
            first = search("a", "abcdabcd");
            missing = search("z", "abcd");
            all_chars = search("abc", "abcabc", 0);
            query_list = search([2, 4, 8], [4, 2, 4], 0);
            rows = search(9, [["a", 2], ["b", 9], ["c", 9]],
                _num_returns_per_match_=0, _index_col_num_=1);
            full_string = search(["abc"], ["abc", "a", "abc"]);
            bool_search = search(true, [true, false]);
            """)

        self.assertIn("def _scadtopy_search(", generated)
        compile(generated, "<generated-search>", "exec")
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertEqual(namespace["first"], [0])
        self.assertEqual(namespace["missing"], [])
        self.assertEqual(namespace["all_chars"], [[0, 3], [1, 4], [2, 5]])
        self.assertEqual(namespace["query_list"], [[1], [0, 2], []])
        self.assertEqual(namespace["rows"], [[1, 2]])
        self.assertEqual(namespace["full_string"], [0])
        self.assertIsNone(namespace["bool_search"])

    def test_search_rejects_missing_duplicate_and_unknown_arguments(self) -> None:
        with self.assertRaisesRegex(TranslationError, "requires match_value"):
            self.translate("result = search([1, 2]);")
        with self.assertRaisesRegex(TranslationError, "multiple values"):
            self.translate("result = search([1], [1], 0, _num_returns_per_match_=0);")
        with self.assertRaisesRegex(TranslationError, "Unsupported search"):
            self.translate("result = search([1], [1], unknown=0);")

    def test_import_maps_to_piecad_load_and_version_is_a_tuple(self) -> None:
        generated = self.translate(
            'import("part.stl"); import(file="part.3mf", convexity=2);'
            'import("drawing.svg"); import("drawing.dxf"); v = version();'
        )

        self.assertIn("_parts.append(load('part.stl'))", generated)
        self.assertIn("_parts.append(load('part.3mf'))", generated)
        self.assertIn("_parts.append(load('drawing.svg'))", generated)
        self.assertIn("_parts.append(load('drawing.dxf'))", generated)
        self.assertNotIn("import() of .svg files is not supported", generated)
        self.assertNotIn("import() of .dxf files is not supported", generated)
        self.assertIn("v = tuple(map(int, version().split('.')))", generated)

    def test_export_comment_saves_the_part_to_its_own_file(self) -> None:
        generated = self.translate(
            "cube(1);\ncube(2); // export PartA.stl\nsquare(3); // export Flat.svg\n"
            "sphere(1); // not an export\ncube(4); // export Mesh.off\n"
        )

        self.assertIn("save('PartA.stl', _parts[1])", generated)
        self.assertIn("save('Flat.svg', _parts[2])", generated)
        self.assertIn("save('Mesh.off', _parts[4])", generated)
        self.assertNotIn("_parts[0])", generated.replace("union(*_parts", ""))
        self.assertNotIn("not an export'", generated.split("__main__")[1])
        self.assertLess(generated.index("PartA.stl'"), generated.index("view("))

    def test_save_precedes_view(self) -> None:
        generated = self.translate("cube(1);")
        self.assertLess(generated.index("save("), generated.index("view("))

    def test_cli_formats_output_with_black(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "shape.scad"
            source.write_text("translate([1,2,3]) cube(1);", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                with patch("sys.argv", ["scadtopy", str(source)]):
                    main_module.main()
            generated = source.with_suffix(".py").read_text(encoding="utf-8")

        self.assertIn('save("shape.3mf"', generated)
        self.assertNotIn("'", generated)

    def test_translates_minkowski_with_piecad_method(self) -> None:
        generated = self.translate("minkowski() { cube(1); sphere(1); };")

        self.assertIn(
            "cube(1.0).minkowski_sum(sphere(1.0, "
            "segments=_scadtopy_calculate_segments(1.0, _scadtopy_default_fn, "
            "_scadtopy_default_fa, _scadtopy_default_fs)))",
            generated,
        )
        compile(generated, "<generated-minkowski>", "exec")

        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertFalse(namespace["_parts"][0].is_empty())

    def test_translates_projection_shadow_and_cut(self) -> None:
        shadow = self.translate("projection() cube(2);")
        cut = self.translate("projection(cut=true) cube(2, center=true);")
        translated = self.translate("translate([10, 20, 30]) projection() cube(2);")
        mixed = self.translate("cube(1); projection() cube(2);")

        self.assertIn(".project()", shadow)
        self.assertIn(".slice(0)", cut)
        self.assertIn(
            "cube(2.0).project().translate([10.0, 20.0])",
            translated,
        )
        self.assertNotIn("_scadtopy_combine_models", mixed)
        compile(shadow, "<generated-projection-shadow>", "exec")
        compile(cut, "<generated-projection-cut>", "exec")
        compile(translated, "<generated-projection-translated>", "exec")
        compile(mixed, "<generated-projection-mixed>", "exec")

        shadow_namespace: dict[str, object] = {}
        translated_namespace: dict[str, object] = {}
        mixed_namespace: dict[str, object] = {}
        exec(shadow, shadow_namespace)
        exec(translated, translated_namespace)
        exec(mixed, mixed_namespace)
        self.assertEqual(shadow_namespace["_parts"][0].bounding_box(), (0, 0, 2, 2))
        self.assertEqual(
            translated_namespace["_parts"][0].bounding_box(), (10, 20, 12, 22)
        )
        self.assertEqual(
            [type(model).__name__ for model in mixed_namespace["_parts"]],
            ["Obj3d", "Obj2d"],
        )

    def test_translates_piecad_boolean_and_transform_methods(self) -> None:
        generated = self.translate("""
            union() {
                translate(v=[1, 0, 0]) cube(1);
                rotate(30, [0, 0, -1]) cube(1);
                scale(2) cube(1);
                mirror([0, 1, 0]) cube(1);
            }
            """)

        self.assertIn("cube(1.0).translate([1.0, 0.0, 0.0])", generated)
        self.assertIn(".rotate([0, 0, -30.0])", generated)
        self.assertIn(".scale([2.0, 2.0, 2.0])", generated)
        self.assertIn(".mirror((False, True, False))", generated)
        compile(generated, "<generated>", "exec")

    def test_translates_multmatrix_with_three_or_four_rows(self) -> None:
        matrix_3x4 = self.translate(
            "multmatrix([[1,0,0,10], [0,1,0,20], [0,0,1,30]]) cube(1);"
        )
        matrix_4x4 = self.translate(
            "m = [[1,0,0,10], [0,1,0,20], [0,0,1,30], [9,8,7,6]]; "
            "multmatrix(m=m) cube(1);"
        )

        self.assertIn(".transform([[1.0, 0.0, 0.0, 10.0]", matrix_3x4)
        self.assertIn("[0.0, 0.0, 1.0, 30.0]][:3])", matrix_3x4)
        self.assertIn(".transform(m[:3])", matrix_4x4)
        for generated in (matrix_3x4, matrix_4x4):
            compile(generated, "<generated-multmatrix>", "exec")
            namespace: dict[str, object] = {}
            exec(generated, namespace)
            self.assertEqual(
                namespace["_parts"][0].bounding_box(),
                (10.0, 20.0, 30.0, 11.0, 21.0, 31.0),
            )

    def test_multmatrix_requires_one_matrix_and_child(self) -> None:
        with self.assertRaisesRegex(TranslationError, "requires one matrix"):
            self.translate("multmatrix() cube(1);")
        with self.assertRaisesRegex(TranslationError, "requires a child geometry"):
            self.translate("multmatrix([[1,0,0,0],[0,1,0,0],[0,0,1,0]]);")

    def test_resize_matches_openscad_auto_and_fixed_sizes(self) -> None:
        generated = self.translate("""
            resize([7,0,0]) cube([1,2,0.5]);
            resize([7,0,0], auto=true) cube([1,2,0.5]);
            resize(newsize=[10,4,0], auto=[true,true,true]) cube([5,4,1]);
            resize([10,0,0], auto=[true,true,false]) cube([5,4,1]);
            resize([6], auto=[false,true]) square([2,4]);
            resize([4,0,0], auto=true) square([2,4]);
            resize([0,0,0], auto=true) cube([1,2,3]);
            resize([10,0,0]) translate([5,0,0]) cube([5,4,1]);
            """)
        self.assertIn("def _scadtopy_resize(", generated)
        compile(generated, "<generated-resize>", "exec")
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        expected = [
            (0, 0, 0, 7, 2, 0.5),
            (0, 0, 0, 7, 14, 3.5),
            (0, 0, 0, 10, 4, 2),
            (0, 0, 0, 10, 8, 1),
            (0, 0, 6, 12),
            (0, 0, 4, 8),
            (0, 0, 0, 1, 2, 3),
            (10, 0, 0, 20, 4, 1),
        ]
        for part, bounds in zip(namespace["_parts"], expected, strict=True):
            self.assertEqual(part.bounding_box(), bounds)

    def test_resize_validates_arguments(self) -> None:
        for source, message in (
            ("resize() cube(1);", "requires newsize"),
            ("resize([1,2,3]);", "requires child geometry"),
            ("resize([1,2,3], bad=true) cube(1);", "Unsupported resize"),
            ("resize([1,2,3], newsize=[1,2,3]) cube(1);", "multiple values"),
            ("resize([1,2,3], false, 1, 2) cube(1);", "Too many"),
        ):
            with self.subTest(source=source):
                with self.assertRaisesRegex(TranslationError, message):
                    self.translate(source)

    def test_resize_uses_union_of_child_geometry(self) -> None:
        generated = self.translate(
            "resize([10,0,0]) { cube(1); translate([4,0,0]) cube(1); }"
        )
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertEqual(namespace["_parts"][0].bounding_box(), (0, 0, 0, 10, 1, 1))
        self.assertNotIn("def _scadtopy_resize", self.translate("cube(1);"))

    def test_translates_centered_linear_extrusion_and_csg(self) -> None:
        generated = self.translate("""
            intersection() {
                hull() {
                    linear_extrude(height=10, center=true) square(4);
                    cube(2);
                }
                cube(8);
            }
            """)

        self.assertIn("intersect(hull(", generated)
        self.assertIn(
            ".extrude(10.0).translate([0, 0, -10.0 / 2])",
            generated,
        )
        compile(generated, "<generated>", "exec")

    def test_marks_unmapped_parser_nodes_for_follow_up(self) -> None:
        generated = self.translate(
            "for (i = [0:2]) cube(1); bevel(2); use <library.scad>;"
        )

        self.assertIn("pass  # FIXME", generated)
        compile(generated, "<generated-fixmes>", "exec")

    def test_marks_unknown_modules_for_follow_up(self) -> None:
        generated = self.translate("bevel(2);")

        self.assertIn("OpenSCAD module 'bevel' has no Piecad mapping", generated)
        self.assertIn("_parts = []", generated)

    def test_marks_unmapped_transform_parameters(self) -> None:
        with self.assertRaisesRegex(TranslationError, "Unsupported linear_extrude"):
            self.translate("linear_extrude(height=5, bogus=10) square(2);")

    def test_cli_writes_python_next_to_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "model.scad"
            output = source.with_suffix(".py")
            source.write_text("cube(5);", encoding="utf-8")

            with patch("sys.argv", ["scadtopy", str(source)]):
                self.assertEqual(main(), 0)

            generated = output.read_text(encoding="utf-8")
            self.assertIn("cube(5.0)", generated)
            compile(generated, str(output), "exec")


if __name__ == "__main__":
    unittest.main()
