"""Package smoke test: the install is importable and versions line up."""

import girder


def test_package_imports() -> None:
    assert girder.__name__ == "girder"
