"""content_service.py の単体テスト（モックベース、実API呼び出し・ファイルI/O不要）。

対話型ウィザードの入力ヘルパー（`prompt_*`）、投稿ドラフトの自動バリデーション
（`validate_draft_text`）、およびエクスポネンシャル・バックオフのリトライデコレータ
（`retry_on_exception`）を対象とする。`unittest.mock.patch('builtins.input')` で
コンソール入力を、`unittest.mock.patch('time.sleep')` で待機時間をシミュレートし、
再プロンプト処理・リトライ挙動をミリ秒単位の高速なテストで検証する。

実行方法:
    python3 -m unittest test_content_service -v
"""

from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from content_service import (
    BrandRules,
    generate_video_script,
    prompt_menu_choice,
    prompt_optional_date,
    prompt_optional_month,
    prompt_positive_int,
    prompt_required_text,
    prompt_text_with_default,
    retry_on_exception,
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


class RetryOnExceptionTest(unittest.TestCase):
    """`retry_on_exception` のエクスポネンシャル・バックオフ挙動を検証する。

    `time.sleep` を `unittest.mock.patch` でモック化し、実際には1秒も待たずに
    ミリ秒単位でリトライ挙動（呼び出し回数・待機秒数の指数増加・最終的な成否）を
    検証する。
    """

    @patch("time.sleep")
    def test_succeeds_on_third_attempt_after_two_failures(self, mock_sleep) -> None:
        """2回連続で失敗し、3回目（1回目のリトライの次）で成功した場合、
        リトライを挟んで最終的な結果が正しく返ることを検証する。
        """
        call_count = 0

        @retry_on_exception(max_retries=3, initial_delay=2.0, backoff_multiplier=2.0)
        def flaky() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError(f"transient failure #{call_count}")
            return "success"

        result = flaky()

        self.assertEqual(result, "success")
        self.assertEqual(call_count, 3)
        # 2回失敗した分だけリトライ待機が発生し、待機秒数は2秒→4秒と指数関数的に増加する
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_has_calls([call(2.0), call(4.0)])

    @patch("time.sleep")
    def test_raises_after_exceeding_max_retries(self, mock_sleep) -> None:
        """初回実行を含めて4回（初回＋リトライ3回）すべて失敗し続けた場合、
        最終的に例外がそのまま呼び出し元へ伝播することを検証する。
        """
        call_count = 0

        @retry_on_exception(max_retries=3, initial_delay=2.0, backoff_multiplier=2.0)
        def always_fails() -> None:
            nonlocal call_count
            call_count += 1
            raise ConnectionError(f"persistent failure #{call_count}")

        with self.assertRaises(ConnectionError) as ctx:
            always_fails()

        self.assertIn("persistent failure #4", str(ctx.exception))
        # 初回実行 + 最大3回のリトライ = 合計4回呼び出される
        self.assertEqual(call_count, 4)
        # リトライは3回発生し、待機秒数は2秒→4秒→8秒と指数関数的に増加する
        self.assertEqual(mock_sleep.call_count, 3)
        mock_sleep.assert_has_calls([call(2.0), call(4.0), call(8.0)])

    @patch("time.sleep")
    def test_succeeds_immediately_without_retry_when_no_exception(self, mock_sleep) -> None:
        """初回実行が成功した場合、リトライ（待機）は一切発生しないことを検証する。"""

        @retry_on_exception()
        def always_succeeds() -> str:
            return "ok"

        self.assertEqual(always_succeeds(), "ok")
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    def test_only_retries_on_specified_exception_types(self, mock_sleep) -> None:
        """`exceptions` で絞り込んだ型以外の例外は、リトライせず即座に伝播することを検証する。"""

        @retry_on_exception(max_retries=3, exceptions=(ConnectionError,))
        def raises_value_error() -> None:
            raise ValueError("not a retryable error")

        with self.assertRaises(ValueError):
            raises_value_error()

        mock_sleep.assert_not_called()


def _text_response(text: str) -> SimpleNamespace:
    """Anthropicのテキストレスポンスを模したフェイクオブジェクトを作る。"""
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


class GenerateVideoScriptTest(unittest.TestCase):
    """`generate_video_script`（縦型ショート動画の3段構成台本生成）を検証する。

    実際のAnthropic APIは呼び出さず、`content_service.build_client` を
    `unittest.mock.patch` でモック化し、`time.sleep` もモック化することで、
    ネットワーク・待機時間の両方に依存しない高速なテストにする。
    """

    def _fake_markdown_script(self) -> str:
        return (
            "| タイムスタンプ / シーン区分 | 映像内容（Visual） | ナレーション/音声（Audio） | 画面テロップ（Telop） |\n"
            "|---|---|---|---|\n"
            "| 0〜3秒【フック】 | 湯気が立ち上る釜まぶし | 「まだ知らないの？」 | 博多の新定番 |\n"
            "| 3〜22秒【ボディ】 | 職人の手元をアップで | こだわりを畳みかける | ポイントを3語で |\n"
            "| 22〜30秒【オファー/CTA】 | 店舗外観 | 「プロフィールのリンクから」 | 保存してね📌 |\n\n"
            "- 🎵 トレンド音楽の指定枠: Lo-fi Hip Hop（BPM90前後）\n"
            "- 🎥 カメラワーク・カット割りの指示: 2〜3秒ごとにカットを切り替える\n"
        )

    @patch("content_service.build_client")
    def test_returns_generated_markdown_script(self, mock_build_client: MagicMock) -> None:
        """指定したテーマ・秒数に応じた動画台本（Markdownテキスト）が正常に
        生成されて返ってくることを検証する。
        """
        expected_script = self._fake_markdown_script()
        fake_client = MagicMock()
        fake_client.messages.create.return_value = _text_response(expected_script)
        mock_build_client.return_value = fake_client

        result = generate_video_script(
            theme="職人技のアピール",
            duration_seconds=30,
            store_name="大濠うなぎ",
            brand_rules=BrandRules(tone_and_manner="上品で落ち着いたトーン"),
        )

        self.assertEqual(result, expected_script.strip())
        self.assertIn("タイムスタンプ", result)
        self.assertIn("Telop", result)

        # システムプロンプトに目標秒数の3段構成が、ユーザーメッセージにテーマ・店舗名が
        # 反映されていることを確認する
        _, call_kwargs = fake_client.messages.create.call_args
        self.assertIn("30秒", call_kwargs["system"])
        user_message = call_kwargs["messages"][0]["content"]
        self.assertIn("職人技のアピール", user_message)
        self.assertIn("大濠うなぎ", user_message)

    @patch("time.sleep")
    @patch("content_service.build_client")
    def test_retries_on_transient_error_then_succeeds(
        self, mock_build_client: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """通信エラーが2回発生しても、`retry_on_exception` により自動リトライされ、
        3回目の呼び出しで最終的な台本が返ることを検証する。
        """
        expected_script = self._fake_markdown_script()
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            ConnectionError("transient network error"),
            ConnectionError("transient network error"),
            _text_response(expected_script),
        ]
        mock_build_client.return_value = fake_client

        result = generate_video_script(theme="新メニューの紹介", duration_seconds=15)

        self.assertEqual(result, expected_script.strip())
        self.assertEqual(fake_client.messages.create.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("time.sleep")
    @patch("content_service.build_client")
    def test_raises_after_exhausting_retries(
        self, mock_build_client: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """通信エラーが最大リトライ回数を超えて発生し続けた場合、最終的に例外が
        呼び出し元へ伝播することを検証する。
        """
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = ConnectionError("persistent network error")
        mock_build_client.return_value = fake_client

        with self.assertRaises(ConnectionError):
            generate_video_script(theme="店舗へのアクセス", duration_seconds=60)

        # 初回 + リトライ3回 = 合計4回呼び出される
        self.assertEqual(fake_client.messages.create.call_count, 4)


class PromptRequiredTextTest(unittest.TestCase):
    """`prompt_required_text` の空欄拒否・再プロンプトを検証する。"""

    @patch("builtins.input", side_effect=["職人技のアピール"])
    def test_valid_input_is_returned(self, mock_input: unittest.mock.Mock) -> None:
        self.assertEqual(prompt_required_text("動画のテーマ"), "職人技のアピール")

    @patch("builtins.input", side_effect=["", "  ", "新メニューの紹介"])
    def test_empty_input_reprompts_until_non_empty(self, mock_input: unittest.mock.Mock) -> None:
        result = prompt_required_text("動画のテーマ")
        self.assertEqual(result, "新メニューの紹介")
        self.assertEqual(mock_input.call_count, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
