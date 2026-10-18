I am using jdepend in Java and wonder if there is a similar tool in Python which list the egress and ingress dependency on classes and functions


Great question! There's no single Python tool that maps 1:1 to JDepend's afferent/efferent coupling metrics, but there are several strong tools that cover the same ground — at different granularities. Here's an overview:

---

## Python Equivalents to JDepend

### 🔍 Module/Package Level (closest to JDepend)

**[pydeps](https://github.com/thebjorn/pydeps)** — the most JDepend-like tool
- Generates dependency graphs between Python modules and packages
- Supports filtering by noise level (degree of connections) and hop distance (`--max-bacon`), and can show raw dependency analysis output via `--show-deps` and `--show-raw-deps`
- Output as SVG/PNG visual graphs
```bash
pip install pydeps
pydeps mypackage --show-deps
```

**[modulegraph](https://pypi.org/project/modulegraph/)** — lower-level, programmatic
- Determines a dependency graph between Python modules primarily by bytecode analysis of import statements, using a more flexible internal representation than the standard `modulefinder`, with more extensive knowledge of special cases

---

### 📞 Function/Method Level (call graphs)

**[pyan3](https://github.com/davidfraser/pyan)** — closest for function-level ingress/egress
- Performs static analysis of Python code to determine a call dependency graph between functions and methods — distinct from runtime profiling, it uses AST and symtable analysis
- Can output to Graphviz, HTML, or integrate with Sphinx
```bash
pip install pyan3
pyan3 mymodule.py --dot | dot -Tsvg > callgraph.svg
```

---

### 📦 Import Graph (static analysis)

**[importlab](https://github.com/google/importlab)** (by Google)
- Takes one or more Python files as arguments and generates an import graph, ensuring that a file's dependencies are analyzed before it is — primarily useful for chaining static analysis tools
- Has a CLI with `--tree` flag for displaying the import tree

---

### 🧩 Custom Approach (most flexible)

For full JDepend-style afferent (Ca) / efferent (Ce) coupling metrics, many teams roll their own using Python's `ast` module + **[networkx](https://networkx.org/)**:

```python
import ast, networkx as nx

# Parse imports from each file, build directed graph
# Then query: G.in_degree(node)  → afferent (Ca)
#             G.out_degree(node) → efferent (Ce)
```

---

## Quick Comparison

| JDepend Concept | Python Tool |
|---|---|
| Package dependency graph | `pydeps` |
| Afferent/Efferent coupling | `pydeps` + custom `ast`/`networkx` |
| Method-level call graph | `pyan3` |
| Import ordering / graph | `importlab` |
| Cycle detection | `pydeps --show-cycles` |

**Recommended starting point:** `pydeps` for module-level analysis, `pyan3` if you need function-level call graphs. If you need precise Ca/Ce metrics like JDepend outputs numerically, a small custom script using `ast` + `networkx` gives you the most control.
