# ADR-0001: Standalone NX workspace wrapping the Python toolchain

## Status

Accepted — 2026-07-18

## Context

Work order TSK-SPK-NX-001 requires bringing `speakeasy` into the operator's NX
governance paradigm. `speakeasy` is a native macOS menu-bar dictation app: Python
3.13, `pyproject.toml` (setuptools), a `pytest` suite (26 unit tests + an
`integration` marker needing model files/audio hardware), and a handful of shell/
Python e2e scripts under `scripts/`. It has no JavaScript/TypeScript surface and no
existing build graph.

The rest of the operator's NX estate lives in `WebDesign-Workspace/nx-monorepo-
workspace`, a JS/TS monorepo. That workspace's charter and lifecycle (web app
release cadence, shared design-system packages, CI wired to that repo) do not
match this project's shape or release rhythm.

## Decision

Initialize a **standalone NX workspace at the `speakeasy` repo root**, rather than
folding the project into the existing nx-monorepo-workspace.

- `package.json` + `nx.json` + `project.json` added at the repo root, alongside
  (not replacing) `pyproject.toml`.
- One NX project, `speakeasy`, with `root: "."`.
- All targets use the plain **`nx:run-commands`** executor to wrap the existing,
  already-working Python toolchain — no NX/Python plugin (`@nxlv/python` or
  similar) was installed. This keeps the workspace dependency-light, avoids
  taking on a plugin whose conventions (its own venv/poetry/uv management,
  its own test-graph assumptions) would fight the project's existing `pip install
  -e '.[dev]'` + `.venv` workflow, and is the least invasive path that still gets
  the project under `nx run <target>` / `nx show projects` / affected-graph
  governance.
- Targets defined in `project.json`:
  - `test` → `.venv/bin/python -m pytest -q`
  - `test-integration` → `.venv/bin/python -m pytest -m integration -q`
  - `lint` → runs `ruff` if importable in `.venv`, else falls back to
    `python -m compileall speakeasy` as a syntax-only placeholder. **Deviation
    note:** at the time of this ADR the project's `.venv` does not have `ruff`
    installed, so `lint` currently exercises the `compileall` placeholder path.
    Installing `ruff` into `.venv` (`pip install ruff`) will make the target
    switch to real linting with no `project.json` change required — the target
    already detects and prefers it.
  - `serve` → `.venv/bin/python -m speakeasy`
  - `e2e` → `.venv/bin/python scripts/e2e_stickies.py raw`
  - All Python-invoking targets are run with `PYTHONPATH=.` and a repo-root
    `cwd` so they exercise the checked-out source tree they run in (important
    in git-worktree contexts, where a stale `pip install -e` editable pointer
    from a different checkout must not shadow the local `speakeasy/` package).

## Alternatives considered

1. **Move `speakeasy` into `nx-monorepo-workspace` as a new project.** Rejected.
   `speakeasy` is a native macOS app shipping local ML models (Parakeet via
   sherpa-onnx) with its own release cadence (packaged `.app`, LaunchAgent
   install, model downloads to `~/.speakeasy/models/`), fully decoupled from the
   monorepo's web release train. Folding it in would couple unrelated deploy
   lifecycles and drag the JS/TS toolchain (npm workspaces, shared tsconfig,
   web CI) into a project that has none of those concerns.
2. **`@nxlv/python` (or similar) NX Python plugin.** Considered but not adopted
   for this pass. It would add inferred targets and its own environment-
   management opinions on top of an already-working `.venv` + `pyproject.toml`
   setup, for marginal benefit over `nx:run-commands` given this project has a
   single Python project and no cross-project Python dependency graph to infer.
   Left as a future option if a second Python NX project appears and a real
   dependency graph needs deriving.
3. **`npx nx@latest init` (interactive/inferred setup).** Considered but a
   manually-authored `nx.json`/`project.json` was chosen instead: it is
   equivalent in effect, avoids an interactive wizard in a scripted/worktree
   context, and keeps the diff minimal and legible.

## Consequences

- `npx nx run speakeasy:test`, `npx nx run speakeasy:test-integration`,
  `npx nx run speakeasy:lint`, `npx nx run speakeasy:serve`, `npx nx run
  speakeasy:e2e`, and `npx nx show projects` are now the governed entry points
  for this project, callable identically from CI or any other NX-aware tooling
  in the operator's estate.
- `node_modules/` and NX's `.nx/cache` + `.nx/workspace-data` are gitignored;
  `package-lock.json` is committed for reproducible installs.
- No changes were made to `speakeasy/`, `tests/`, `scripts/`, or the contents of
  `pyproject.toml` — NX sits alongside the existing Python toolchain, wrapping
  it rather than replacing it.
- Follow-up (not part of this work order): install `ruff` into `.venv` and drop
  the `compileall` fallback note once done.
