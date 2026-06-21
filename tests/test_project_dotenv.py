"""project_dotenv：.env 覆寫 placeholder。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from project_dotenv import finmind_token_from_env, load_project_dotenv, shell_export_dotenv


class TestProjectDotenv(unittest.TestCase):
    def test_load_overrides_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "FINMIND_TOKEN=real_jwt_token_from_file\n",
                encoding="utf-8",
            )
            os.environ["FINMIND_TOKEN"] = "your_token_here"
            load_project_dotenv(env_path, override=True)
            self.assertEqual(os.environ["FINMIND_TOKEN"], "real_jwt_token_from_file")

    def test_shell_export_quotes_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text('GMAIL_APP_PASSWORD=qspz inuj absc sgtl\n', encoding="utf-8")
            block = shell_export_dotenv(env_path)
            self.assertEqual(
                block,
                "export GMAIL_APP_PASSWORD='qspz inuj absc sgtl'",
            )

    def test_finmind_token_from_env_reloads_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("FINMIND_TOKEN=from_file\n", encoding="utf-8")
            os.environ["FINMIND_TOKEN"] = "your_token_here"
            with patch("project_dotenv.PROJECT_ROOT", Path(tmp)):
                token = finmind_token_from_env()
            self.assertEqual(token, "from_file")


if __name__ == "__main__":
    unittest.main()
