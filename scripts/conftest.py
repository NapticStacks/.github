"""Keep the fixture projects out of this repo's own test run.

`scripts/fixtures/*` contains deliberately broken suites (see
`fixtures/pytest_import_error`). They are DATA for `test_test_count.py`, which
collects them in a subprocess on purpose. Collecting them inline would make this
repo's own CI red.
"""

collect_ignore_glob = ["fixtures/*"]
