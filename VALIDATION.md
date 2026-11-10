# Validation record — 2026-11-10

Executed locally on Windows with Python 3.14.7, using an isolated virtual environment:

- Editable installation `pip install -e '.[dev]'`: passed.
- `ruff check .`: passed.
- `python -m compileall -q src`: passed.
- `python -m unittest discover -s tests -v`: 7 offline safety checks passed.
- `sphere-agent --config config.example.toml check`: passed, without network/model/game access.
- `pip check`: no broken requirements.

The checks cover read-only enforcement, pause gating, unresolved commit intent freezing, original
terminal identity mismatch retention, successful original-action reconciliation without commit,
rejection of unsupported raw write tools, token redaction and same-ledger process exclusion.

No real Provider request, actual Spherewright MCP process connection, Unity/game action, save/load,
building or flight was attempted. The checks are code-quality evidence only. Real-game acceptance
belongs to the user and follows MANUAL-ACCEPTANCE.md. Python 3.11–3.13 support is declared by the
source's language baseline but was not separately executed. Frozen dependencies record this Windows
Python 3.14 environment; `pyproject.toml` is the portable dependency specification.
