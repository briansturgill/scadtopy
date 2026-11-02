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
            "_scadtopy_translate((sphere(radius=4.0, segments=32.0)), "
            "[1.0, 2.0, 3.0]))",
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
        tapered = self.translate("cylinder(h=12, r1=2, r2=4);")

        self.assertIn("_model = circle(radius=(10.0) / 2, segments=24.0)", circle)
        self.assertIn("_model = cylinder(12.0, 3.0, center=True)", cylinder)
        self.assertIn("_model = elliptical_cylinder(12.0, (2.0, 4.0))", tapered)
        self.assertNotIn("_model = eliptical_cylinder", cylinder)
        compile(circle, "<generated-circle>", "exec")
        compile(cylinder, "<generated-cylinder>", "exec")
        compile(tapered, "<generated-tapered-cylinder>", "exec")

    def test_top_level_fn_sets_piecad_default_segments(self) -> None:
        generated = self.translate("$fn = 48; sphere(5);")

        self.assertIn("Config.set_default_segments(int(48.0))", generated)
        self.assertNotIn("$fn =", generated)
        self.assertIn("_model = sphere(5.0)", generated)
        compile(generated, "<generated-default-segments>", "exec")

        from piecad import Config

        original_segments = Config._default_segments
        try:
            exec(generated, {})
            self.assertEqual(Config._default_segments, 48)
        finally:
            Config.set_default_segments(original_segments)

    def test_warns_for_other_top_level_special_settings(self) -> None:
        generated = self.translate("$fa = 3; $fs = 2; $preview = true; cube(1);")

        self.assertIn("# UNSUPPORTED: $fa = 3.0", generated)
        self.assertIn("# UNSUPPORTED: $fs = 2.0", generated)
        self.assertIn("# UNSUPPORTED: $preview = True", generated)
        self.assertNotIn("$fa =", generated.split("# UNSUPPORTED:", 1)[0])
        compile(generated, "<generated-special-settings>", "exec")

    def test_passes_through_comments_and_parser_blank_lines(self) -> None:
        generated = self.translate(
            "// header\n\n$fa = 3; // setting note\n\n"
            "// paragraph one\n\n// paragraph two\ncube(1); // geometry note"
        )

        self.assertIn("# header", generated)
        self.assertIn("# UNSUPPORTED: $fa = 3.0  # setting note", generated)
        self.assertIn("# paragraph one", generated)
        self.assertIn("# paragraph two", generated)
        self.assertIn("_model = cube(1.0)  # geometry note", generated)
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
                faces=[[0,1,2],[0,3,1],[0,2,3],[1,3,2]]
            );
            """
        )

        self.assertIn("face[0], face[i + 1], face[i]", generated)
        self.assertIn("check='repair'", generated)
        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertFalse(namespace["_model"].is_empty())
        self.assertGreater(namespace["_model"].volume(), 0)

    def test_translates_t_scad_geometry_end_to_end(self) -> None:
        generated = self.translate(
            """
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
            """
        )

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
            model = namespace["_model"]
            self.assertFalse(model.is_empty())
            self.assertGreater(model.volume(), 0)
        finally:
            Config.set_default_segments(original_segments)

        self.assertIn('ECHO: "done"', output.getvalue())

    def test_translates_children_and_unions_module_bodies(self) -> None:
        generated = self.translate(
            """
            module repeated(count=3) {
                for(i=[0:count-1]) translate([i*10,0,0]) children();
            }
            module pair() { cube(1); sphere(1); }
            module first() { children(0); }
            repeated(3) { cube(1); sphere(1); }
            translate([0,20,0]) pair();
            translate([0,40,0]) first() { cube(2); sphere(5); }
            """
        )

        self.assertIn("def repeated(count=3.0, _children=()):", generated)
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
        self.assertFalse(namespace["_model"].is_empty())

    def test_translates_children_selectors_and_count(self) -> None:
        generated = self.translate(
            """
            module pick() { children([0:1]); translate([20,0,0]) children([2]); }
            module count() { translate([0,0,$children]) children(0); }
            module outer() { module inner() { children(1); } inner() { cube(1); cube(3); } }
            pick() { cube(1); cube(2); cube(3); }
            count() { cube(1); cube(1); }
            outer();
            """
        )

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
        generated = self.translate(
            """
            offset(r=2) square([10,10],center=true);
            translate([30,0,0]) offset(delta=1) square(4);
            """
        )

        self.assertNotIn("FIXME", generated)
        self.assertIn('.offset(2.0, "round")', generated)
        self.assertIn('.offset(1.0, "miter", 1000.0)', generated)
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
        self.assertEqual(namespace["_model"].bounding_box()[3], 5.0)

    def test_translates_minkowski_with_piecad_method(self) -> None:
        generated = self.translate("minkowski() { cube(1); sphere(1); };")

        self.assertIn("_model = (cube(1.0)).minkowski_sum(sphere(1.0))", generated)
        compile(generated, "<generated-minkowski>", "exec")

        namespace: dict[str, object] = {}
        exec(generated, namespace)
        self.assertFalse(namespace["_model"].is_empty())

    def test_translates_projection_shadow_and_cut(self) -> None:
        shadow = self.translate("projection() cube(2);")
        cut = self.translate("projection(cut=true) cube(2, center=true);")
        translated = self.translate(
            "translate([10, 20, 30]) projection() cube(2);"
        )
        mixed = self.translate("cube(1); projection() cube(2);")

        self.assertIn(".project()", shadow)
        self.assertIn(".slice(0)", cut)
        self.assertIn("_scadtopy_translate", translated)
        self.assertIn("_scadtopy_combine_models", mixed)
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
        self.assertEqual(shadow_namespace["_model"].bounding_box(), (0, 0, 2, 2))
        self.assertEqual(
            translated_namespace["_model"].bounding_box(), (10, 20, 12, 22)
        )
        self.assertEqual(
            [type(model).__name__ for model in mixed_namespace["_model"]],
            ["Obj3d", "Obj2d"],
        )

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

        self.assertIn("_scadtopy_translate((cube(1.0)), [1.0, 0.0, 0.0])", generated)
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
