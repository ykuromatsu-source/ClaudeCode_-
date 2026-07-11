"""agent_core.py の単体テスト（モックベース、実API呼び出し不要）。

ANTHROPIC_API_KEY が無い環境でも、Advisor-Executorパイプラインの制御
ロジック（Advisor呼び出し予算の厳格な遵守・品質レビュー後の自律修正
フロー・エラー時の1回リトライ）が正しく動作することを検証する。

実行方法:
    python3 -m unittest test_agent_core -v
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_core import (
    Advisor,
    AdvisorExecutorAgent,
    AdvisorVerdict,
    AgentBudget,
    Worker,
)

SAMPLE_SCHEMA = {
    "name": "submit_sns_draft",
    "description": "test",
    "input_schema": {"type": "object", "properties": {}, "required": []},
}


def _text_response(text: str) -> SimpleNamespace:
    """Anthropicのテキストレスポンスを模したフェイクオブジェクトを作る。"""
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


def _tool_response(name: str, input_: dict) -> SimpleNamespace:
    """Anthropicのtool_useレスポンスを模したフェイクオブジェクトを作る。"""
    return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=name, input=input_)])


class AdvisorExecutorPipelineTest(unittest.TestCase):
    """AdvisorExecutorAgentの制御ロジックを検証する。"""

    def test_full_pipeline_with_one_revision_stays_within_budget(self) -> None:
        """計画レビュー承認→品質レビュー要修正→自律修正の流れで、Advisor呼び出しが厳密に2回に収まることを確認する。"""
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            _text_response("計画: 冷たさと職人技を訴求する"),  # 1. worker.draft_plan
            _tool_response("submit_review", {"verdict": "approved", "feedback": "良い計画です"}),  # 2. advisor 計画レビュー
            _tool_response(
                "submit_sns_draft",
                {
                    "instagram_caption": "初稿キャプション",
                    "instagram_hashtags": ["#うなぎ"],
                    "line_message": "初稿LINE文",
                    "image_prompt_en": "unagi ochazuke, initial",
                },
            ),  # 3. worker.generate_content
            _tool_response(
                "submit_review",
                {"verdict": "needs_revision", "feedback": "季節限定である旨を明記してください"},
            ),  # 4. advisor 品質レビュー
            _tool_response(
                "submit_sns_draft",
                {
                    "instagram_caption": "修正済みキャプション（夏季限定と明記）",
                    "instagram_hashtags": ["#うなぎ", "#夏季限定"],
                    "line_message": "修正済みLINE文（夏季限定と明記）",
                    "image_prompt_en": "unagi ochazuke, summer limited",
                },
            ),  # 5. worker.revise_content
        ]

        budget = AgentBudget()
        agent = AdvisorExecutorAgent(
            worker=Worker(fake_client), advisor=Advisor(fake_client), budget=budget
        )
        result = agent.run(brief="店舗情報テスト", brand_guideline="季節限定は必ず明記", tool_schema=SAMPLE_SCHEMA)

        self.assertEqual(budget.advisor_calls_used, 2)
        self.assertEqual(result.plan_review.verdict, AdvisorVerdict.APPROVED)
        self.assertEqual(result.quality_review.verdict, AdvisorVerdict.NEEDS_REVISION)
        self.assertTrue(result.was_revised)
        self.assertIn("夏季限定", result.final_content["instagram_caption"])
        self.assertEqual(fake_client.messages.create.call_count, 5)

    def test_advisor_budget_exceeded_skips_quality_review_gracefully(self) -> None:
        """Advisor呼び出し予算が1回しかない場合、品質レビューはスキップされ初稿がそのまま採用されることを確認する。"""
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            _text_response("計画テキスト"),
            _tool_response("submit_review", {"verdict": "approved", "feedback": "OK"}),
            _tool_response(
                "submit_sns_draft",
                {
                    "instagram_caption": "唯一のドラフト",
                    "instagram_hashtags": ["#test"],
                    "line_message": "LINE本文",
                    "image_prompt_en": "prompt",
                },
            ),
        ]

        budget = AgentBudget(max_advisor_calls=1)  # 計画レビューだけで使い切る
        agent = AdvisorExecutorAgent(
            worker=Worker(fake_client), advisor=Advisor(fake_client), budget=budget
        )
        result = agent.run(brief="b", brand_guideline="g", tool_schema=SAMPLE_SCHEMA)

        self.assertIsNone(result.quality_review)
        self.assertFalse(result.was_revised)
        self.assertEqual(result.final_content["instagram_caption"], "唯一のドラフト")
        self.assertEqual(budget.advisor_calls_used, 1)
        self.assertEqual(fake_client.messages.create.call_count, 3)

    def test_worker_call_retries_once_on_transient_error(self) -> None:
        """Worker呼び出しが1回失敗しても、予算内であれば自動的に再試行して成功することを確認する。"""
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            RuntimeError("一時的なネットワークエラー"),  # 1回目失敗
            _text_response("リトライ後の計画"),  # 2回目成功
            _tool_response("submit_review", {"verdict": "approved", "feedback": "OK"}),
            _tool_response(
                "submit_sns_draft",
                {"instagram_caption": "c", "instagram_hashtags": [], "line_message": "l", "image_prompt_en": "p"},
            ),
            _tool_response("submit_review", {"verdict": "approved", "feedback": "OK"}),
        ]

        budget = AgentBudget()
        agent = AdvisorExecutorAgent(
            worker=Worker(fake_client), advisor=Advisor(fake_client), budget=budget
        )
        result = agent.run(brief="b", brand_guideline="g", tool_schema=SAMPLE_SCHEMA)

        self.assertEqual(result.plan, "リトライ後の計画")
        self.assertEqual(budget.error_retries_used, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
