"""Advisor-Executor協調エージェントによる飲食店SNS運用自動化のエントリポイント。

既存スキル insta-food-buzz（~/.claude/skills/insta-food-buzz/、トリガー評価
正解率100%達成済み）が確立したペルソナ・品質スコアリング基準・CTAフォーマットを
読み取り専用で取り込み、Instagram投稿・LINE公式アカウント配信文・画像生成AI用
英語プロンプトの3種ドラフトを、Advisor（Fable 5想定）によるレビューを経て
Worker（Sonnet 5想定）が生成するパイプラインを提供する。

insta-food-buzzスキル本体のファイルは一切変更しない。

実行方法:
    python3 main.py
"""

from __future__ import annotations

import sys

from content_service import (
    RestaurantBrief,
    format_result_as_markdown,
    generate_sns_drafts,
)
from agent_core import PipelineResult
from skill_knowledge import build_knowledge_digest, load_skill_knowledge

# 景表法・薬機法など、insta-food-buzzのスコープ外にある法務・ブランド規約。
# insta-food-buzzの知見（品質面）と合わせてAdvisorのレビュー基準となる。
LEGAL_AND_BRAND_GUIDELINE = (
    "・「日本一」「絶対」などの断定的最上級表現は禁止（景品表示法上のリスク）\n"
    "・健康効果を断定する表現は禁止（薬機法抵触リスク）\n"
    "・実在しない行列・実績数値の記載は禁止\n"
    "・季節限定・数量限定である場合は必ず明記する\n"
)

SAMPLE_BRIEF = RestaurantBrief(
    store_name="大濠うなぎ",
    store_genre="うなぎ専門店",
    menu_name="夏限定 特製うなぎ冷やし茶漬け",
    menu_description=(
        "炭火焼きのうなぎを氷水で締め、冷たい緑茶だしをかけた夏季限定の茶漬け。"
        "薬味は大葉・みょうが・白胡麻。仕上げに山葵を添える。"
    ),
    season_or_event="夏季限定（7月〜8月、なくなり次第終了）",
    brand_tone="本物の日本の味を伝える、上品で落ち着いたトーン。誇張表現は避ける。",
)


def generate_content(brief: RestaurantBrief) -> PipelineResult:
    """insta-food-buzzの知見を統合したパイプラインでSNS投稿ドラフト一式を生成する。

    Args:
        brief: 店舗・商品情報。

    Returns:
        PipelineResult: Advisor-Executorパイプラインの実行結果
            （計画・両レビュー結果・最終ドラフトを含む）。

    Raises:
        skill_knowledge.SkillKnowledgeError: 既存スキル資産が読み込めない場合。
        RuntimeError: ANTHROPIC_API_KEY が未設定の場合。
    """
    knowledge = load_skill_knowledge()
    digest = build_knowledge_digest(knowledge)
    return generate_sns_drafts(
        brief, brand_guideline=LEGAL_AND_BRAND_GUIDELINE, knowledge_digest=digest
    )


def generate_content_as_markdown(brief: RestaurantBrief) -> str:
    """generate_content を実行し、報告用Markdown文字列を返す。"""
    result = generate_content(brief)
    return format_result_as_markdown(brief, result)


def main() -> int:
    """サンプル案件でパイプラインを実行し、Markdownを標準出力へ表示する。"""
    try:
        print(generate_content_as_markdown(SAMPLE_BRIEF))
    except Exception as exc:  # noqa: BLE001 - CLIエントリポイントとして全例外を捕捉し終了コードに変換する
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
