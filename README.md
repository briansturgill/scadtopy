# scadtopy

Translate OpenSCAD files to Python code that builds geometry with [Piecad](https://pypi.org/project/piecad/). Parsing and AST generation are provided by [`openscad-parser`](https://pypi.org/project/openscad-parser/).

Install the project and its dependencies with:

```sh
python -m pip install -e .
```

Translate a model:

```sh
scadtopy model.scad
```

This writes `model.py`. Choose a different output path with `-o`:

```sh
scadtopy model.scad -o generated_model.py
```

The translator handles assignments, function and module definitions, expressions, numeric ranges and common list comprehensions, module loops and conditionals, common primitives, and Piecad boolean and transform operations. OpenSCAD `minkowski()` is mapped to chained Piecad `Obj3d.minkowski_sum()` calls, and `projection()` maps to Piecad `project()` or `slice()` for `cut=true`. The generated file starts with `_parts = []`, each top-level geometry statement does `_parts.append(part)`, and the `__main__` block runs `for _part in _parts: view(_part)`. Generated expressions are normalized through Python's parser so they read like handwritten Piecad (no redundant parentheses; literal `translate` vectors on 2D shapes are trimmed to x, y; vector `square` becomes `rectangle`). Parts are never combined, so 2D and 3D parts can coexist. `translate()` of a 2D shape drops the vector's z component, since Piecad's 2D `translate` takes only x and y. `text()` supports `text`, `size`, `halign` and `valign`: `left`/`baseline` are Piecad's defaults, and the other alignments translate the text by its bounding box. `linear_extrude` with `twist`, `slices` or `scale` uses Piecad `extrude_transforming` (twist direction is converted from OpenSCAD's clockwise convention by mirroring), and `rotate_extrude` maps to `revolve`. OpenSCAD `offset(r=...)` maps to Piecad `Obj2d.offset(r, "round")`; `offset(delta=...)` maps to a `"miter"` join (or `"square"` with `chamfer=true`). Piecad has no 2D `minkowski_sum`, and a round-join offset is the equivalent. User functions and modules whose names collide with Piecad exports (for example `square`) are emitted with a trailing underscore so they don't shadow Piecad. Modules that use `children()` or `$children` (including nested modules) receive a `_children` list from call sites; `children()`, `children(i)`, `children([i, j])`, and `children([a:b])` select and union them, warning on out-of-range indices. Polygon paths are reversed to convert OpenSCAD winding to Piecad's convention; polyhedron faces are triangulated, reversed, and passed through Piecad's mesh repair. A top-level `$fn` assignment sets Piecad's default circular segment count through `Config.set_default_segments`; other top-level `$` assignments are emitted as `# UNSUPPORTED:` comments. The generated `__main__` block first saves every 3D part with `save("name.3mf", union(*_parts))` (no directory, so Piecad writes to its Downloads directory or `PIECAD_SAVE_DIR`), then views each part. When 2D and 3D parts are mixed only the 3D parts are saved; a file with only 2D parts saves an `.svg`. `include <file>` is expanded by the parser, so the included file's contents appear inline. `use <name.scad>` becomes `from name import *`, and the used file is read to learn its module names (including `children()` use), so `name.py` must be the translation of `name.scad`. The command-line tool formats its output with `black`. `group()` is a union of its children. `*` drops its geometry silently, `#` and `%` are ignored but keep their geometry, and `!` is a fatal error (`Show only ('!') is not allowed at line: N`). `round`, `sign`, `concat` (with non-literal arguments), `str`, `rands`, `lookup`, and `search` emit small `_scadtopy_*` helpers that follow OpenSCAD semantics; `search()` supports per-value matching, string character searches, optional match limits, and list-of-lists column selection. `is_undef`, `is_list`, `is_string`, `is_bool`, `is_num`, `is_function` and `concat` of list literals become plain Python. `version()` becomes a tuple of the three numbers from Piecad's `version()` string. `import("file")` maps to Piecad `load("file")` for 3mf, glb, gltf, obj, ply, stl, svg and dxf files (the path is used as written, so it is relative to the working directory when the Python runs); `convexity` is ignored and unsupported formats such as amf become FIXMEs.  A trailing `// export Name.ext` comment on a geometry statement saves that part to its own file with `save('Name.ext', _parts[i])`, before the combined save and the viewer; any extension Piecad's `save` accepts works (3D formats go through trimesh, 2D parts need `.svg`). The part still appears in the combined file and the viewer. OpenSCAD comments and parser-preserved blank lines are carried into generated Python comments and spacing. Syntax with no reliable equivalent is emitted with an explicit `pass  # FIXME` marker (or a `None` expression placeholder) rather than silently discarded.
## OpenSCAD circular segment settings

Generated `translate()` calls inspect the returned Piecad object at runtime: `Obj2d` receives only the x/y offsets, while `Obj3d` receives all three offsets. Automatic saving likewise selects `.svg` or `.3mf` from the runtime geometry types, so user modules returning 2D or 3D objects are handled correctly.

`multmatrix(m)` maps 3D child geometry to Piecad's `Obj3d.transform()` using the first three rows of `m`; this supports OpenSCAD's 3x4 matrix form and its 4x4 form, whose final row is ignored.

`resize(newsize, auto)` uses a generated adapter for Piecad's 2D and 3D `resize()` methods. Zero dimensions are left unchanged unless selected by `auto`; automatically sized dimensions use the scale of the largest requested dimension. Short vectors are padded with zero sizes or false auto flags, and `convexity` is ignored.

Function values can be called through expressions, including functions selected from lists, returned by other calls, or chosen conditionally.

OpenSCAD ranges use Piecad `frange`, with inclusive endpoints and list results.

OpenSCAD assertions translate to Python `assert` statements, which are disabled when Python runs with `-O` or `-OO`.

Translation emits source-filename and line-number warnings for `use <file.scad>` (the used file must be translated separately) and supported `import("file")` paths (passed unchanged to Piecad and resolved from the Python process working directory).

Correction to the overview above: top-level `$fn`, `$fa`, and `$fs` assignments map to Piecad's segment-count, minimum-angle, and minimum-segment-length configuration. Circular primitives calculate their segment count from the radius and active settings; per-call overrides are passed through, including through user modules. `linear_extrude()` forwards these settings to its child profile and uses them to determine the twist divisions when `slices` is omitted.

When a top-level function or module is called before its declaration, declarations from that source point onward are emitted before the first forward call, matching OpenSCAD's order-independent declaration behavior. Statements before that call, including assignments used by module parameter defaults, retain their order.
