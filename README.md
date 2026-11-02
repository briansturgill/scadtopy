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

The translator handles assignments, function and module definitions, expressions, numeric ranges and common list comprehensions, module loops and conditionals, common primitives, and Piecad boolean and transform operations. Syntax with no reliable equivalent is emitted with an explicit `pass  # FIXME` marker (or a `None` expression placeholder) rather than silently discarded.
