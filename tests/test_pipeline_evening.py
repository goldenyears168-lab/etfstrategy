"""pipeline_evening 步驟計畫測試。"""

from __future__ import annotations

import unittest
from pathlib import Path

from pipeline_evening import EveningPipelineConfig, plan_evening_steps
from project_config import ETF_CODES_HOLDINGS


class PipelineEveningTests(unittest.TestCase):
    def _cfg(self, **env: str) -> EveningPipelineConfig:
        base = {
            "RUN_SCORE_ENGINE": "0",
            "RUN_NEWS_SYNC": "0",
            "RUN_CATALYST_ENGINE": "0",
            "RUN_EXPORT_AI_BUNDLE": "0",
            "RUN_MEMO": "0",
            "RUN_PERPLEXITY_SUMMARY": "0",
            "RUN_PERPLEXITY_VERIFY": "0",
        }
        base.update(env)
        return EveningPipelineConfig.from_environ(
            python=Path("/tmp/python"),
            src=Path("/tmp/src"),
            env=base,
        )

    def test_score_off_skips_score_step(self) -> None:
        steps = plan_evening_steps(self._cfg())
        score_steps = [s for s in steps if "investment score" in s.label]
        self.assertEqual(len(score_steps), 1)
        self.assertFalse(score_steps[0].enabled)
        self.assertIn("RUN_SCORE_ENGINE=0", score_steps[0].skip_reason or "")

    def test_score_on_includes_score_and_bundle(self) -> None:
        steps = plan_evening_steps(
            self._cfg(RUN_SCORE_ENGINE="1", RUN_EXPORT_AI_BUNDLE="1")
        )
        labels = [s.label for s in steps if s.enabled]
        self.assertIn("investment score", labels)
        self.assertIn("AI bundle export (JSON + 提示詞)", labels)
        self.assertIn("position review (持倉賣出雷達)", labels)

    def test_news_sync_requires_api_key(self) -> None:
        steps = plan_evening_steps(self._cfg(RUN_NEWS_SYNC="1"))
        news = next(s for s in steps if s.label.startswith("catalyst news"))
        self.assertFalse(news.enabled)
        self.assertIn("PERPLEXITY_API_KEY", news.skip_reason or "")

    def test_verify_reruns_score_when_enabled(self) -> None:
        steps = plan_evening_steps(
            self._cfg(
                RUN_SCORE_ENGINE="1",
                RUN_PERPLEXITY_VERIFY="1",
                PERPLEXITY_API_KEY="sk-test",
            )
        )
        enabled_labels = [s.label for s in steps if s.enabled]
        self.assertIn("catalyst verify (Perplexity)", enabled_labels)
        self.assertIn("investment score (post-verify)", enabled_labels)

    def test_etf_codes_in_argv(self) -> None:
        steps = plan_evening_steps(self._cfg(RUN_SCORE_ENGINE="1"))
        score = next(s for s in steps if s.label == "investment score" and s.enabled)
        joined = " ".join(score.argv)
        self.assertIn(",".join(ETF_CODES_HOLDINGS), joined)

    def test_show_report_uses_human_score_and_quiet_bundle(self) -> None:
        cfg = EveningPipelineConfig.from_environ(
            python=Path("/tmp/python"),
            src=Path("/tmp/src"),
            show_report=True,
            env={
                "RUN_SCORE_ENGINE": "1",
                "RUN_EXPORT_AI_BUNDLE": "1",
            },
        )
        steps = plan_evening_steps(cfg)
        score = next(s for s in steps if s.label == "investment score" and s.enabled)
        self.assertIn("--human", score.argv)
        bundle = next(
            s for s in steps if s.label == "AI bundle export (JSON + 提示詞)" and s.enabled
        )
        joined = " ".join(bundle.argv)
        self.assertIn("--quiet", joined)
        self.assertIn("--no-print-prompts", joined)


if __name__ == "__main__":
    unittest.main()
