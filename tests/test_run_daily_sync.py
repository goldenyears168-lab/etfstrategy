"""run_daily_sync.py pipes daily_sync.sh to bash -s (launchd TCC safe)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import run_daily_sync as rds  # noqa: E402


class TestRunDailySync(unittest.TestCase):
    def test_pipes_script_to_bash_stdin(self) -> None:
        script = ROOT / "scripts" / "daily_sync.sh"
        with patch("run_daily_sync.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            rc = rds.main(["--help"])
        self.assertEqual(rc, 0)
        mock_run.assert_called_once()
        call = mock_run.call_args
        self.assertEqual(call.args[0], ["/bin/bash", "-s", "--", "--help"])
        self.assertEqual(call.kwargs["cwd"], ROOT)
        stdin = call.kwargs["stdin"]
        self.assertEqual(getattr(stdin, "name", None), str(ROOT / "scripts" / "daily_sync.sh"))

    def test_missing_script_returns_2(self) -> None:
        with patch.object(rds.Path, "is_file", return_value=False):
            rc = rds.main([])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
