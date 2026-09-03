"""Fixture suite: deliberately unimportable, to exercise collection_error."""

import a_module_that_does_not_exist_anywhere  # noqa: F401


def test_never_collected():
    assert True
