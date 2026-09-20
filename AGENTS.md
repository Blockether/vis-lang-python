# vis-lang-python

The Python language pack: syntax, ruff, run_tests, managed REPL.

- The engine is a development dependency only. Nothing here may be required at runtime by vis
  in a way that makes the dependency circular: vis pins this library, never the other way round.
- Public API lives in `com.blockether.vis.lang.python.core`; everything else is internal and may change.
- Tests are Lazytest, not `clojure.test`: `clojure -M:test`.
- Formatting is zprint with the repository's `.zprint.edn`; lint is clj-kondo.
- Keep the user-visible contract (tool names, parameters, results, activity presentation)
  identical to the engine version this code came from. Compatibility is the point of the split.
