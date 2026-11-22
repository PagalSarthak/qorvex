import pytest

from tests import util


@pytest.fixture(scope="session")
def httpd():
    port, servers = util.start_server()
    try:
        yield port
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


@pytest.fixture(autouse=True)
def clean_state():
    util.STATE.reset()
    yield
    util.STATE.reset()


@pytest.fixture
def manager(tmp_path):
    mgr = util.make_manager(tmp_path)
    try:
        yield mgr
    finally:
        mgr.close()
