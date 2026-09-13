"""Windows kernel lease checks; no mouse/keyboard or app control."""
import subprocess
import sys
from uuid import uuid4

import pytest
from deepdesk.live_control_desktop import DesktopLease

pytestmark=pytest.mark.skipif(sys.platform!='win32',reason='Windows kernel primitives')


def test_cross_process_lease_is_exclusive_and_released():
    name='Local\\Elren.LiveControl.Test.'+uuid4().hex
    child_code="""
import sys
from deepdesk.live_control_desktop import DesktopLease
try:
    lease=DesktopLease(name=sys.argv[1])
except RuntimeError:
    print('blocked')
else:
    print('acquired')
    lease.close()
"""
    def child():
        return subprocess.run([sys.executable,'-c',child_code,name],capture_output=True,text=True,timeout=10,check=True).stdout.strip()
    lease=DesktopLease(name=name)
    try:
        assert child()=='blocked'
    finally:
        lease.close()
    lease.close()  # Idempotent cleanup must not increase semaphore count twice.
    assert child()=='acquired'


def test_same_process_two_instances_cannot_own_one_desktop():
    name='Local\\Elren.LiveControl.Test.'+uuid4().hex
    lease=DesktopLease(name=name)
    try:
        with pytest.raises(RuntimeError,match='owns desktop'):
            DesktopLease(name=name)
    finally:
        lease.close()
