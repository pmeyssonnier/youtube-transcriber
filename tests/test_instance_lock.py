from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.instance_lock import AlreadyRunningError, InstanceLock


class InstanceLockTests(unittest.TestCase):
    def test_only_one_lock_holder_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.lock"
            first = InstanceLock(path)
            second = InstanceLock(path)
            first.acquire()
            try:
                with self.assertRaises(AlreadyRunningError):
                    second.acquire()
            finally:
                first.release()
            second.acquire()
            second.release()


if __name__ == "__main__":
    unittest.main()
