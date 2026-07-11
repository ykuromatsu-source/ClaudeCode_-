"""デモ実行スクリプト。

サンプルの飲食店情報（大濠うなぎ / 夏限定 特製うなぎ冷やし茶漬け）を入力し、
main.generate_content が組み立てる Advisor-Executor パイプライン
（Worker=Sonnet 5 / Advisor=Fable 5、insta-food-buzzの知見を統合済み）を実行し、
Markdown形式で最終ドラフトを出力する。

環境変数 ANTHROPIC_API_KEY が設定されていれば実際にAPIを呼び出す。
未設定の場合は、パイプラインの制御フロー（計画レビュー→生成→品質レビュー）と
出力フォーマットのみを決定的なダミー値で再現する「モックモード」で実行し、
その旨を明示する（AI生成であるかのように偽装しない）。

実行方法:
    python3 run_demo.py
    # 実APIを使う場合:
    export ANTHROPIC_API_KEY=sk-...
    python3 run_demo.py
"""

from __future__ import annotations

import os
import sys

from agent_core import AdvisorReview, AdvisorVerdict, PipelineResult
from content_service import format_result_as_markdown
from main import SAMPLE_BRIEF, generate_content


def _build_mock_result() -> PipelineResult:
    """ANTHROPIC_API_KEY未設定時に、パイプライン構造のみを再現するダミー結果を作る。

    実際のAI生成は一切行わない。Advisor-Executorのフロー
    （計画立案→計画レビュー→生成→品質レビュー→自律修正）と、
    format_result_as_markdown による出力フォーマットを検証するためだけの
    決定的なプレースホルダー値である。
    """
    plan = (
        "[モック] 炭火焼きの香ばしさと氷水締めの涼やかさという温度・食感の対比を軸に、"
        "上品で落ち着いたトーンで「夏季限定」を明確に打ち出す。"
    )
    plan_review = AdvisorReview(
        verdict=AdvisorVerdict.APPROVED,
        feedback="[モック] ブランドトーンとの整合、季節限定訴求ともに問題なし。承認。",
    )
    draft = {
        "instagram_caption": (
            "[モックドラフト：実際のAI生成ではありません]\n"
            "炭火の香りをまとったうなぎを、キリッと冷たい緑茶だしにくぐらせて。"
            "ひと口ごとに、香ばしさと涼やかさが交互にやってくる、夏だけの一杯です。"
            "なくなり次第終了。"
        ),
        "instagram_hashtags": [
            "#大濠うなぎ",
            "#福岡グルメ",
            "#うなぎ茶漬け",
            "#夏季限定メニュー",
            "#福岡うなぎ",
        ],
        "line_message": (
            "[モック] 🍵夏季限定「特製うなぎ冷やし茶漬け」始めました。"
            "炭火焼きの香ばしさ×冷たい緑茶だし。なくなり次第終了です。"
        ),
        "image_prompt_en": (
            "[MOCK] A bowl of chilled unagi ochazuke, char-grilled eel glistening over rice, "
            "cold green tea broth poured tableside, shiso and myoga garnish, elegant Japanese "
            "restaurant lighting, macro food photography, appetizing texture detail"
        ),
    }
    quality_review = AdvisorReview(
        verdict=AdvisorVerdict.APPROVED,
        feedback="[モック] 五感描写・限定性の訴求ともに基準を満たしている。",
    )
    return PipelineResult(
        plan=plan,
        plan_review=plan_review,
        draft=draft,
        quality_review=quality_review,
        final_content=draft,
        was_revised=False,
        advisor_calls_used=2,
        error_retries_used=0,
    )


def main() -> int:
    """デモを実行し、生成結果をMarkdownで標準出力へ表示する。"""
    api_key_present = bool(os.environ.get("ANTHROPIC_API_KEY"))

    if api_key_present:
        try:
            result = generate_content(SAMPLE_BRIEF)
            mode = "live（実API呼び出し）"
        except Exception as exc:  # noqa: BLE001 - 実行失敗時はモックへ安全にフォールバックする
            print(
                f"[警告] 実API呼び出しに失敗したためモックモードにフォールバックします: {exc}",
                file=sys.stderr,
            )
            result = _build_mock_result()
            mode = "mock（実API呼び出し失敗によるフォールバック）"
    else:
        print(
            "[情報] ANTHROPIC_API_KEY が未設定のため、モックモードで"
            "パイプラインの構造と出力フォーマットのみを検証します。"
            "実際のAI生成を行うには ANTHROPIC_API_KEY を設定してください。",
            file=sys.stderr,
        )
        result = _build_mock_result()
        mode = "mock（ANTHROPIC_API_KEY未設定）"

    print(f"<!-- 実行モード: {mode} -->\n")
    print(format_result_as_markdown(SAMPLE_BRIEF, result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
