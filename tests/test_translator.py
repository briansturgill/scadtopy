from __future__ import annotations

import io
import tempfile
import unittest
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
from scadtopy import translate_file
from scadtopy.cli import main
from scadtopy.translator import _Translator


class TranslatorTests(unittest.TestCase):
    def translate(self, source: str) -> str:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "model.scad"
            path.write_text(source, encoding="utf-8")
            return translate_file(path)

    def test_translates_primitives_transforms_and_boolean_operations(self) -> None:
        generated = self.translate(
            """
            difference() {
                cube([10, 20, 30], center=true);
                translate([1, 2, 3]) sphere(r=4, $fn=32);
            }
            """
        )

        self.assertIn(
            "_model = difference(cube(1, center=True).scale([10.0, 20.0, 30.0]), "
            "(sphere(radius=4.0, segments=32.0)).translate([1.0, 2.0, 3.0]))",
            generated,
        )
        compile(generated, "<generated>", "exec")

    def test_translates_modules_and_variables(self) -> None:
        generated = self.translate(
            """
            size = 8;
            module box(width=size) {
                cube([width, width, width]);
            }
            box(12);
            """
        )

        self.assertIn("size = 8.0", generated)
        self.assertIn("def box(width=size):", generated)
        self.assertIn("return cube(1).scale([width, width, width])", generated)
        self.assertIn("_model = box(12.0)", generated)
        compile(generated, "<generated>", "exec")

    def test_maps_diameters_and_centered_cylinders(self) -> None:
        circle = self.translate("circle(d=10, $fn=24);")
        cylinder = self.translate("cylinder(12, 3, true);")

        self.assertIn("_model = circle(radius=(10.0) / 2, segments=24.0)", circle)
        self.assertIn("_model = cylinder(12.0, 3.0, center=True)", cylinder)
        compile(circle, "<generated-circle>", "exec")
        compile(cylinder, "<generated-cylinder>", "exec")

    def test_translates_polygon_paths(self) -> None:
        generated = self.translate(
            "polygon(points=[[0,0],[2,0],[1,2]], paths=[[0,1,2]]);"
        )

        self.assertIn("for index in path", generated)
        self.assertIn("for path in [[0.0, 1.0, 2.0]]", generated)
        compile(generated, "<generated-polygon>", "exec")
        exec(generated, {})

    def test_translates_inclusive_range_literals(self) -> None:
        generated = self.translate(
            """
            implicit = [1:3];
            ascending = [0:2:6];
            descending = [3:-1:0];
            fractional = [0:0.1:0.3];
            empty = [5:0];
            """
        )

        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertEqual(namespace["implicit"], [1.0, 2.0, 3.0])
        self.assertEqual(namespace["ascending"], [0.0, 2.0, 4.0, 6.0])
        self.assertEqual(namespace["descending"], [3.0, 2.0, 1.0, 0.0])
        self.assertEqual(namespace["fractional"], [0.0, 0.1, 0.2, 0.30000000000000004])
        self.assertEqual(namespace["empty"], [])

    def test_translates_functions_let_assert_and_function_literals(self) -> None:
        generated = self.translate(
            """
            function twice(value=2) = value * 2;
            result = let(base=twice()) base + 1;
            callback = function(value) value + 3;
            checked = assert(result == 5) callback(4);
            """
        )

        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertEqual(namespace["result"], 5.0)
        self.assertEqual(namespace["checked"], 7.0)
        self.assertEqual(namespace["callback"](2), 5)

    def test_expression_assertion_is_lazy_and_fails_explicitly(self) -> None:
        generated = self.translate("result = assert(false) missing_function();")

        with self.assertRaises(AssertionError):
            exec(generated, {})

    def test_expression_echo_prints_arguments_and_returns_body_value(self) -> None:
        generated = self.translate(
            'result = echo("number", 3, status=true) 7;'
        )

        output = io.StringIO()
        namespace: dict[str, object] = {}
        with redirect_stdout(output):
            exec(generated, namespace)

        self.assertEqual(output.getvalue(), 'ECHO: "number", 3, status = true\n')
        self.assertEqual(namespace["result"], 7.0)

    def test_translates_list_comprehension_loops_and_conditions(self) -> None:
        generated = self.translate(
            """
            doubled = [for(value=[0:3]) value * 2];
            even = [for(value=[0:4]) if(value % 2 == 0) value];
            selected = [if(true) 3 else 4];
            """
        )

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
        generated = self.translate(
            """
            module repeated(count) {
                for(value=[0:count-1]) translate([value, 0, 0]) cube(1);
            }
            if(true) repeated(3); else sphere(1);
            let(size=2) cube(size);
            """
        )

        compile(generated, "<generated-modules>", "exec")
        self.assertIn("union(*[", generated)
        self.assertIn("if True else", generated)
        self.assertIn("lambda size:", generated)

    def test_translates_remaining_module_nodes_and_render_expression(self) -> None:
        generated = self.translate(
            """
            rendered = render() { cube(1); };
            echo(1) cube(2);
            assert(true) cube(3);
            intersection_for(i=[0:1]) translate([i, 0, 0]) cube(1);
            #sphere(1);
            %cube(1);
            !cube(1);
            *cube(1);
            """
        )

        compile(generated, "<generated-module-nodes>", "exec")
        self.assertIn("rendered = cube(1.0)", generated)
        self.assertNotIn("FIXME: module echo", generated)
        self.assertIn("pass  # FIXME: display modifier", generated)
        self.assertIn("pass  # FIXME: disabled geometry", generated)
        namespace: dict[str, object] = {}
        output = io.StringIO()
        with redirect_stdout(output):
            exec(generated, namespace)
        self.assertIsNotNone(namespace["_model"])
        self.assertEqual(output.getvalue(), "ECHO: 1\n")

    def test_module_echo_prints_arguments_and_returns_child_geometry(self) -> None:
        generated = self.translate('echo("shape", count=2) cube(1);')

        output = io.StringIO()
        namespace: dict[str, object] = {}
        with redirect_stdout(output):
            exec(generated, namespace)

        self.assertEqual(output.getvalue(), 'ECHO: "shape", count = 2\n')
        self.assertEqual(namespace["_model"].bounding_box(), (0, 0, 0, 1, 1, 1))

    def test_standalone_module_echo_does_not_create_a_model(self) -> None:
        generated = self.translate('echo("standalone");')

        output = io.StringIO()
        namespace: dict[str, object] = {}
        with redirect_stdout(output):
            exec(generated, namespace)

        self.assertEqual(output.getvalue(), 'ECHO: "standalone"\n')
        self.assertNotIn("_model", namespace)

    def test_translates_polyhedron_faces_for_piecad(self) -> None:
        generated = self.translate(
            """
            polyhedron(
                points=[[0,0,0],[10,0,0],[0,10,0],[0,0,10]],
                faces=[[0,2,1],[0,1,3],[0,3,2],[1,2,3]]
            );
            """
        )

        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertFalse(namespace["_model"].is_empty())

    def test_translates_piecad_boolean_and_transform_methods(self) -> None:
        generated = self.translate(
            """
            union() {
                translate(v=[1, 0, 0]) cube(1);
                rotate(30, [0, 0, -1]) cube(1);
                scale(2) cube(1);
                mirror([0, 1, 0]) cube(1);
            }
            """
        )

        self.assertIn(".translate([1.0, 0.0, 0.0])", generated)
        self.assertIn(".rotate([0, 0, -(30.0)])", generated)
        self.assertIn(".scale([2.0, 2.0, 2.0])", generated)
        self.assertIn(".mirror((False, True, False))", generated)
        compile(generated, "<generated>", "exec")

    def test_translates_centered_linear_extrusion_and_csg(self) -> None:
        generated = self.translate(
            """
            intersection() {
                hull() {
                    linear_extrude(height=10, center=true) square(4);
                    cube(2);
                }
                cube(8);
            }
            """
        )

        self.assertIn("_model = intersect(hull(", generated)
        self.assertIn(
            ".extrude(10.0).translate([0, 0, (-(10.0) / 2 if True else 0)])",
            generated,
        )
        compile(generated, "<generated>", "exec")

    def test_marks_unmapped_parser_nodes_for_follow_up(self) -> None:
        generated = self.translate(
            "for (i = [0:2]) cube(1); *sphere(2); use <library.scad>;"
        )

        self.assertIn("pass  # FIXME", generated)
        compile(generated, "<generated-fixmes>", "exec")

    def test_marks_unknown_modules_for_follow_up(self) -> None:
        generated = self.translate("bevel(2);")

        self.assertIn("OpenSCAD module 'bevel' has no Piecad mapping", generated)
        self.assertIn("_model = None", generated)

    def test_marks_unmapped_transform_parameters(self) -> None:
        generated = self.translate("linear_extrude(height=5, twist=10) square(2);")

        self.assertIn("pass  # FIXME: only height and center", generated)
        self.assertIn("_model = None", generated)
        compile(generated, "<generated-unsupported-extrusion>", "exec")

    def test_cli_writes_python_next_to_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "model.scad"
            output = source.with_suffix(".py")
            source.write_text("cube(5);", encoding="utf-8")

            with patch("sys.argv", ["scadtopy", str(source)]):
                self.assertEqual(main(), 0)

            generated = output.read_text(encoding="utf-8")
            self.assertIn("_model = cube(5.0)", generated)
            compile(generated, str(output), "exec")


if __name__ == "__main__":
    unittest.main()
