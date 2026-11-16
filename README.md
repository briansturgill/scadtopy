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

The translator handles assignments, function and module definitions, expressions, numeric ranges and common list comprehensions, module loops and conditionals, common primitives, and Piecad boolean and transform operations. OpenSCAD `minkowski()` is mapped to chained Piecad `Obj3d.minkowski_sum()` calls, and `projection()` maps to Piecad `project()` or `slice()` for `cut=true`. The generated file starts with `_parts = []`, each top-level geometry statement does `_parts.append(part)`, and the `__main__` block runs `for _part in _parts: view(_part)`. Generated expressions are normalized through Python's parser so they read like handwritten Piecad (no redundant parentheses; literal `translate` vectors on 2D shapes are trimmed to x, y; vector `square` becomes `rectangle`). Parts are never combined, so 2D and 3D parts can coexist. `translate()` of a 2D shape drops the vector's z component, since Piecad's 2D `translate` takes only x and y. `text()` supports `text`, `size`, `halign` and `valign`: `left`/`baseline` are Piecad's defaults, and the other alignments translate the text by its bounding box. `linear_extrude` with `twist`, `slices` or `scale` uses Piecad `extrude_transforming` (twist direction is converted from OpenSCAD's clockwise convention by mirroring), and `rotate_extrude` maps to `revolve`. OpenSCAD `offset(r=...)` maps to Piecad `Obj2d.offset(r, "round")`; `offset(delta=...)` maps to a `"miter"` join (or `"square"` with `chamfer=true`). Piecad has no 2D `minkowski_sum`, and a round-join offset is the equivalent. User functions and modules whose names collide with Piecad exports (for example `square`) are emitted with a trailing underscore so they don't shadow Piecad. Modules that use `children()` or `$children` (including nested modules) receive a `_children` list from call sites; `children()`, `children(i)`, `children([i, j])`, and `children([a:b])` select and union them, warning on out-of-range indices. Polygon paths are reversed to convert OpenSCAD winding to Piecad's convention; polyhedron faces are triangulated, reversed, and passed through Piecad's mesh repair. A top-level `$fn` assignment sets Piecad's default circular segment count through `Config.set_default_segments`; other top-level `$` assignments are emitted as `# UNSUPPORTED:` comments. The generated `__main__` block first saves every 3D part with `save("name.3mf", union(*_parts))` (no directory, so Piecad writes to its Downloads directory or `PIECAD_SAVE_DIR`), then views each part. When 2D and 3D parts are mixed only the 3D parts are saved; a file with only 2D parts saves an `.svg`. `include <file>` is expanded by the parser, so the included file's contents appear inline. `use <name.scad>` becomes `from name import *`, and the used file is read to learn its module names (including `children()` use), so `name.py` must be the translation of `name.scad`. The command-line tool formats its output with `black`. `group()` is a union of its children. `*` drops its geometry silently, `#` and `%` are ignored but keep their geometry, and `!` is a fatal error (`Show only ('!') is not allowed at line: N`). `round`, `sign`, `concat` (with non-literal arguments), `str`, `rands`, `lookup`, and `search` emit small `_scadtopy_*` helpers that follow OpenSCAD semantics; `search()` supports per-value matching, string character searches, optional match limits, and list-of-lists column selection. `is_undef`, `is_list`, `is_string`, `is_bool`, `is_num`, `is_function` and `concat` of list literals become plain Python. `version()` becomes a tuple of the three numbers from Piecad's `version()` string. `import("file")` maps to Piecad `load("file")` for 3mf, glb, gltf, obj, off, ply, stl, stl_ascii, svg and dxf files (the path is used as written, so it is relative to the working directory when the Python runs); `convexity` is ignored and unsupported formats such as amf are reported as `UNSUPPORTED`. A missing filename is a translation error. The legacy `filename` argument is accepted as a fallback for `file`. A trailing `// export Name.ext` comment on a geometry statement saves that part to its own file with `save('Name.ext', _parts[i])`, before the combined save and the viewer; any extension Piecad's `save` accepts works (3D formats go through trimesh, 2D parts need `.svg`). The part still appears in the combined file and the viewer. OpenSCAD comments and parser-preserved blank lines are carried into generated Python comments and spacing. Unsupported syntax produces a warning and an `UNSUPPORTED` comment; unsupported expressions are replaced with `None`.

Vector addition is componentwise, including nested vectors, and stops at the shorter vector, as in OpenSCAD. Incompatible vector components raise `TypeError`. Scalar addition and `concat()` retain their usual behavior.

`import()` also supports `center`, including runtime expressions. Layer selection and other unsupported import options are reported as `UNSUPPORTED`. The deprecated `import_stl()`, `import_off()`, and `import_dxf()` always read their respective formats regardless of the filename extension. For STL and OFF, layer, origin, scale, center, and convexity do not affect the imported geometry. For DXF, layer selection, origin, and scale are unsupported; center and convexity are ignored.

`surface()` is reported as unsupported and its geometry is omitted. When a `use` source cannot be found relative to the source directory, the translator reports it but retains the Python import; unresolved module calls are also reported as not found.

## OpenSCAD circular segment settings

Generated `translate()` calls use Piecad's `.translate()` directly when the geometry dimension is known. Otherwise, an adapter inspects the object at runtime: `Obj2d` receives only the x/y offsets, while `Obj3d` receives all three offsets. Automatic saving selects `.svg` or `.3mf` from the runtime geometry types, so user modules returning 2D or 3D objects are handled correctly.

Dimension inference follows known children through user modules at each call site. Modules that translate their own children still use the adapter when their callers may supply either 2D or 3D geometry.

`rotate()` accepts scalar angles, Euler-angle vectors, and arbitrary axis vectors, including runtime expressions. Rotations of 2D geometry use the corresponding XY projection; a singular projection produces empty geometry with a warning.

`mirror()` accepts 2D or 3D plane-normal vectors, including runtime expressions. A zero vector leaves geometry unchanged; 2D geometry uses the XY projection and warns if that projection is singular.

`polyhedron()` accepts the legacy `triangles` name for `faces` and ignores the preview-only `convexity` parameter. Missing points or faces are translation errors. Faces are fan-triangulated, which can produce incorrect geometry for concave faces.

The deprecated OpenSCAD `child()` selects the first child, while `child(i)` selects child `i`.

The deprecated `assign()` scopes its bindings over its child geometry. Its binding expressions are evaluated independently in the enclosing scope, unlike `let()`. Bindings that shadow generated Python or Piecad calls are reported as unsupported.

`multmatrix(m)` maps 3D child geometry to Piecad's `Obj3d.transform()` using the first three rows of `m`; this supports OpenSCAD's 3x4 matrix form and its 4x4 form, whose final row is ignored.

`resize(newsize, auto)` uses a generated adapter for Piecad's 2D and 3D `resize()` methods. Zero dimensions are left unchanged unless selected by `auto`; automatically sized dimensions use the scale of the largest requested dimension. Short vectors are padded with zero sizes or false auto flags, and `convexity` is ignored.

Function values can be called through expressions, including functions selected from lists, returned by other calls, or chosen conditionally.

Function-level `let` bindings become assignments when this preserves their scope. Common list comprehensions use Python comprehensions and list unpacking; scoped lambda expressions remain where needed to preserve shadowing or closure behavior. C-style comprehensions use `frange` for safe literal integer loops and a scoped generator otherwise. Duplicate bindings evaluate their expressions, produce warnings, and retain the first value. Bindings that cannot be represented as Python identifiers are reported as unsupported.

Comments before list-comprehension controls do not change their translation.

In list comprehensions, `each` expands lists, ranges, and strings, includes scalar values once, and omits `undef`. Nested comprehension controls apply `each` to each resulting value.

Unsupported `text()` parameters produce warnings and `UNSUPPORTED` comments; the affected text geometry is omitted.

`color()` combines and colors multiple 2D or multiple 3D children. Missing or `undef` colors leave geometry unchanged. Numeric vectors use OpenSCAD's 0–1 RGB scale, and alpha is discarded; short and alpha-bearing hex colors are converted to RGB. Color names are passed unchanged to Piecad. Mixed 2D/3D child groups are unsupported and are reported during translation when detectable, or at runtime otherwise.

Assignments inside nested geometry blocks use scoped Python helpers, preserving loop bindings, shadowing, and child order. When a block assigns the same variable more than once, the last expression is evaluated at the first assignment's position, with a warning.

OpenSCAD ranges use Piecad `frange`, with inclusive endpoints and list results.

Geometry `import()` accepts runtime filename expressions and passes them to Piecad `load()`, which determines the format at runtime. Layer selection and other unsupported import options produce warnings and `UNSUPPORTED` comments rather than silently importing different geometry.

OpenSCAD assertions translate to Python `assert` statements, which are disabled when Python runs with `-O` or `-OO`.

OpenSCAD `echo()` translates to Python `print()`, so output uses Python's formatting rather than OpenSCAD's console format.

Translation emits source-filename and line-number warnings for `use <file.scad>` (the used file must be translated separately) and supported `import()` paths (passed unchanged to Piecad and resolved from the Python process working directory).

`use` filenames whose stems are not valid Python module names are reported as `UNSUPPORTED` rather than imported.

Correction to the overview above: top-level `$fn`, `$fa`, and `$fs` assignments set the translated segment settings without changing Piecad's global configuration. `$fn` defaults to OpenSCAD's `0`, so segment counts are calculated from the radius, `$fa`, and `$fs` unless overridden. Per-call overrides are passed through, including through user modules. `linear_extrude()` forwards these settings to its child profile and uses them to determine the twist divisions when `slices` is omitted.

Segment calculations and the transformed-extrusion adapter read current default settings at call time; generated calls omit default arguments when they can do so without changing evaluation order.

User modules receive only the segment settings needed by their bodies or the modules they call.

Calls to local modules omit inherited global settings when safe; calls to modules imported via `use` pass the caller's settings explicitly.

Transformed `linear_extrude()` calls use `extrude_transforming()` directly when their arguments can be adapted statically; other cases use a generated runtime adapter.

When a top-level function or module is called before its declaration, declarations from that source point onward are emitted before the first forward call, matching OpenSCAD's order-independent declaration behavior. Statements before that call, including assignments used by module parameter defaults, retain their order.
