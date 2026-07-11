"""content_service.py の単体テスト（モックベース、実API呼び出し・ファイルI/O不要）。

対話型ウィザードの入力ヘルパー（`prompt_*`）と、投稿ドラフトの自動バリデーション
（`validate_draft_text`）を対象とする。`unittest.mock.patch('builtins.input')` で
コンソール入力をシミュレートし、再プロンプト処理（不正入力時のループ）まで検証する。

実行方法:
    python3 -m unittest test_content_service -v
"""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from content_service import (
    BrandRules,
    prompt_menu_choice,
    prompt_optional_date,
    prompt_optional_month,
    prompt_positive_int,
    prompt_text_with_default,
    validate_draft_text,
)


def _build_instagram_markdown(caption: str, hashtags: list[str]) -> str:
    """`format_result_as_markdown` が組み立てる構造を模した、検閲対象の最小限のMarkdownを作る。"""
    hashtags_line = " ".join(hashtags)
    return (
        "# SNS投稿ドラフト: テスト店舗｜【メニュー訴求】 テストメニュー\n\n"
        "## Instagram\n"
        "**キャプション**\n\n"
        f"{caption}\n\n"
        "**ハッシュタグ**\n\n"
        f"{hashtags_line}\n\n"
        "## LINE公式アカウント\n\n"
        "テスト用のLINEメッセージです。\n"
    )


class ValidateDraftTextTest(unittest.TestCase):
    """`validate_draft_text` の検閲ロジック（NGワード・文字数・ハッシュタグ数）を検証する。"""

    def setUp(self) -> None:
        self.brand_rules = BrandRules(tone_and_manner="テスト用トーン")

    def test_clean_draft_passes_with_no_issues(self) -> None:
        """NGワードなし・文字数以内・ハッシュタグ30個以内の完璧なドラフトはPassする。"""
        markdown = _build_instagram_markdown(
            caption="炭火焼きのうなぎを、季節のだしと一緒にお楽しみください。",
            hashtags=["#大濠うなぎ", "#福岡グルメ", "#うなぎ"],
        )
        issues = validate_draft_text(markdown, self.brand_rules)
        self.assertEqual(issues, [])

    def test_negative_word_in_caption_is_detected(self) -> None:
        """本文にネガティブNGワード（既定: まずい/高い/最悪/遅い）が含まれるとFailする。"""
        markdown = _build_instagram_markdown(
            caption="正直に言うと、今日の一杯はまずい仕上がりでした。",
            hashtags=["#大濠うなぎ"],
        )
        issues = validate_draft_text(markdown, self.brand_rules)
        rules_hit = [issue.rule for issue in issues]
        self.assertIn("NGワード", rules_hit)
        self.assertTrue(any("まずい" in issue.message for issue in issues))

    def test_caption_over_2200_chars_is_detected(self) -> None:
        """キャプションが2,200字を超えるとFailする。"""
        markdown = _build_instagram_markdown(
            caption="あ" * 2201,
            hashtags=["#大濠うなぎ"],
        )
        issues = validate_draft_text(markdown, self.brand_rules)
        rules_hit = [issue.rule for issue in issues]
        self.assertIn("文字数", rules_hit)

    def test_hashtags_over_30_is_detected(self) -> None:
        """ハッシュタグが31個以上あるとFailする。"""
        markdown = _build_instagram_markdown(
            caption="通常のキャプションです。",
            hashtags=[f"#tag{i}" for i in range(31)],
        )
        issues = validate_draft_text(markdown, self.brand_rules)
        rules_hit = [issue.rule for issue in issues]
        self.assertIn("ハッシュタグ数", rules_hit)

    def test_hashtags_exactly_30_passes(self) -> None:
        """境界値: ちょうど30個のハッシュタグはPassする（30個は上限内）。"""
        markdown = _build_instagram_markdown(
            caption="通常のキャプションです。",
            hashtags=[f"#tag{i}" for i in range(30)],
        )
        issues = validate_draft_text(markdown, self.brand_rules)
        self.assertEqual(issues, [])


class PromptMenuChoiceTest(unittest.TestCase):
    """`prompt_menu_choice` の番号選択・再プロンプトを検証する。"""

    @patch("builtins.input", side_effect=["2"])
    def test_valid_selection_returns_matching_key(self, mock_input: unittest.mock.Mock) -> None:
        result = prompt_menu_choice("何をしますか？", [("a", "選択肢A"), ("b", "選択肢B")])
        self.assertEqual(result, "b")

    @patch("builtins.input", side_effect=["9", "abc", "1"])
    def test_out_of_range_and_non_numeric_input_reprompts(
        self, mock_input: unittest.mock.Mock
    ) -> None:
        result = prompt_menu_choice("何をしますか？", [("a", "選択肢A"), ("b", "選択肢B")])
        self.assertEqual(result, "a")
        self.assertEqual(mock_input.call_count, 3)


class PromptOptionalDateTest(unittest.TestCase):
    """`prompt_optional_date` の日付パース・空欄スキップ・再プロンプトを検証する。"""

    @patch("builtins.input", side_effect=["2026-07-15"])
    def test_valid_date_is_parsed(self, mock_input: unittest.mock.Mock) -> None:
        result = prompt_optional_date("投稿予定日")
        self.assertEqual(result, date(2026, 7, 15))

    @patch("builtins.input", side_effect=[""])
    def test_empty_input_returns_none(self, mock_input: unittest.mock.Mock) -> None:
        self.assertIsNone(prompt_optional_date("投稿予定日"))

    @patch("builtins.input", side_effect=["2026/07/15", "not-a-date", "2026-07-15"])
    def test_invalid_format_reprompts_until_valid(self, mock_input: unittest.mock.Mock) -> None:
        result = prompt_optional_date("投稿予定日")
        self.assertEqual(result, date(2026, 7, 15))
        self.assertEqual(mock_input.call_count, 3)


class PromptOptionalMonthTest(unittest.TestCase):
    """`prompt_optional_month` の年月パース・空欄スキップ・再プロンプトを検証する。"""

    @patch("builtins.input", side_effect=["2026-07"])
    def test_valid_month_is_returned(self, mock_input: unittest.mock.Mock) -> None:
        self.assertEqual(prompt_optional_month("年月"), "2026-07")

    @patch("builtins.input", side_effect=[""])
    def test_empty_input_returns_none(self, mock_input: unittest.mock.Mock) -> None:
        self.assertIsNone(prompt_optional_month("年月"))

    @patch("builtins.input", side_effect=["2026/07", "2026-07"])
    def test_invalid_format_reprompts_until_valid(self, mock_input: unittest.mock.Mock) -> None:
        result = prompt_optional_month("年月")
        self.assertEqual(result, "2026-07")
        self.assertEqual(mock_input.call_count, 2)


class PromptPositiveIntTest(unittest.TestCase):
    """`prompt_positive_int` の既定値・バリデーション・再プロンプトを検証する。"""

    @patch("builtins.input", side_effect=[""])
    def test_empty_input_returns_default(self, mock_input: unittest.mock.Mock) -> None:
        self.assertEqual(prompt_positive_int("生成する日数", 7), 7)

    @patch("builtins.input", side_effect=["3"])
    def test_valid_value_overrides_default(self, mock_input: unittest.mock.Mock) -> None:
        self.assertEqual(prompt_positive_int("生成する日数", 7), 3)

    @patch("builtins.input", side_effect=["-1", "abc", "5"])
    def test_non_positive_and_non_numeric_input_reprompts(
        self, mock_input: unittest.mock.Mock
    ) -> None:
        result = prompt_positive_int("生成する日数", 7)
        self.assertEqual(result, 5)
        self.assertEqual(mock_input.call_count, 3)


class PromptTextWithDefaultTest(unittest.TestCase):
    """`prompt_text_with_default` の既定値フォールバックを検証する。"""

    @patch("builtins.input", side_effect=[""])
    def test_empty_input_returns_default(self, mock_input: unittest.mock.Mock) -> None:
        self.assertEqual(
            prompt_text_with_default("出力先ファイルパス", "outputs/combined_export.md"),
            "outputs/combined_export.md",
        )

    @patch("builtins.input", side_effect=["outputs/custom.md"])
    def test_custom_value_overrides_default(self, mock_input: unittest.mock.Mock) -> None:
        self.assertEqual(
            prompt_text_with_default("出力先ファイルパス", "outputs/combined_export.md"),
            "outputs/custom.md",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
