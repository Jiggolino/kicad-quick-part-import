import pytest

from test_importer import Workspace


@pytest.fixture
def ws():
    workspace = Workspace()
    yield workspace
    workspace.close()
