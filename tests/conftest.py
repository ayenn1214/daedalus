"""Test session settings.

Qt / VTK objects still alive when the interpreter shuts down can crash it (segmentation fault, exit code 139) after
every test has passed — CI then reports a failure. Leave straight away once pytest is done, with pytest's own exit
status. Set DAEDALUS_TEST_HARD_EXIT=0 to shut down the normal way."""
import os
import sys

import pytest

_status = [0]


def pytest_sessionfinish(session, exitstatus):
    _status[0] = int(exitstatus)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    if os.environ.get("DAEDALUS_TEST_HARD_EXIT", "1") != "0":
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(_status[0])
