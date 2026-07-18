"""Test package marker.

Makes `tests` a real package so test modules are imported as
`tests.<module>` (pytest prepend mode then puts the repo root on
sys.path) and `from tests.conftest import ...` works under the
`pytest` console script, not only under `python -m pytest`.
"""
