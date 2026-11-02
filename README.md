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

The translator handles assignments, function and module definitions, expressions, numeric ranges and common list comprehensions, module loops and conditionals, common primitives, and Piecad boolean and transform operations. OpenSCAD `minkowski()` is mapped to chained Piecad `Obj3d.minkowski_sum()` calls, and `projection()` maps to Piecad `project()` or `slice()` for `cut=true`. Top-level 2D and 3D models are grouped separately when both are present. OpenSCAD `offset(r=...)` maps to Piecad `Obj2d.offset(r, "round")`; `offset(delta=...)` maps to a `"miter"` join (or `"square"` with `chamfer=true`). Piecad has no 2D `minkowski_sum`, and a round-join offset is the equivalent. User functions and modules whose names collide with Piecad exports (for example `square`) are emitted with a trailing underscore so they don't shadow Piecad. Modules that use `children()` or `$children` (including nested modules) receive a `_children` list from call sites; `children()`, `children(i)`, `children([i, j])`, and `children([a:b])` select and union them, warning on out-of-range indices. Polygon paths are reversed to convert OpenSCAD winding to Piecad's convention; polyhedron faces are triangulated, reversed, and passed through Piecad's mesh repair. A top-level `$fn` assignment sets Piecad's default circular segment count through `Config.set_default_segments`; other top-level `$` assignments are emitted as `# UNSUPPORTED:` comments. OpenSCAD comments and parser-preserved blank lines are carried into generated Python comments and spacing. Syntax with no reliable equivalent is emitted with an explicit `pass  # FIXME` marker (or a `None` expression placeholder) rather than silently discarded.
