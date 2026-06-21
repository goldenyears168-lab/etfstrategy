"""notify_job_result CLI."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
import sys

sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import notify_job_result as njr  # noqa: E402


class TestNotifyJobResult(unittest.TestCase):
    def test_skips_when_env_flag_off(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
            f.write(b"ok\n")
            log_path = Path(f.name)
        try:
            with patch("notify_job_result.load_project_dotenv"):
                with patch.dict(os.environ, {"RUN_EVENING_HOLDINGS_EMAIL": "0"}, clear=False):
                    with patch("notify_job_result.send_job_result") as mock_send:
                        rc = njr.main(
                            [
                                "--subject-prefix",
                                "test",
                                "--exit-code",
                                "0",
                                "--log-path",
                                str(log_path),
                                "--env-flag",
                                "RUN_EVENING_HOLDINGS_EMAIL",
                            ]
                        )
                    self.assertEqual(rc, 0)
                    mock_send.assert_not_called()
        finally:
            log_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
