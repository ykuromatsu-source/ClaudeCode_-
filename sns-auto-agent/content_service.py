"""飲食店向けSNS投稿ドラフト生成サービス。

Instagramキャプション＋ハッシュタグ、LINE公式アカウント配信文、
画像生成AI（Midjourney/DALL-E想定）用の英語プロンプトを一括生成する。
実際のモデル呼び出しは agent_core.AdvisorExecutorAgent に委譲する。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import anthropic

from agent_core import (
    AdvisorExecutorAgent,
    Advisor,
    PipelineResult,
    Worker,
)

# Workerが生成する構造化コンテンツのJSON Schema（Anthropicツール定義形式）。
# Advisorの品質レビュー・Workerの修正でも同じスキーマを使い回す。
CONTENT_TOOL_SCHEMA: dict[str, Any] = {
    "name": "submit_sns_draft",
    "description": "飲食店SNS投稿の各種ドラフトを提出する",
    "input_schema": {
        "type": "object",
        "properties": {
            "instagram_caption": {
                "type": "string",
                "description": "Instagram投稿本文（絵文字・改行を含む、日本語、150〜300字目安）",
            },
            "instagram_hashtags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "先頭に#を含むハッシュタグのリスト（10〜20個目安。大中小の規模感を混在させる）",
            },
            "line_message": {
                "type": "string",
                "description": "LINE公式アカウント配信文（簡潔・開封率重視・絵文字を適切に配置、100〜200字目安）",
            },
            "image_prompt_en": {
                "type": "string",
                "description": (
                    "画像生成AI（Midjourney/DALL-E）用の英語プロンプト。"
                    "料理や店内の魅力を最大限引き出す描写を含めること。"
                ),
            },
        },
        "required": [
            "instagram_caption",
            "instagram_hashtags",
            "line_message",
            "image_prompt_en",
        ],
    },
}


@dataclass
class RestaurantBrief:
    """投稿生成のインプットとなる店舗・商品情報。"""

    store_name: str
    store_genre: str
    menu_name: str
    menu_description: str
    season_or_event: str
    brand_tone: str

    def to_prompt_text(self) -> str:
        """Workerに渡す自然文形式に変換する。"""
        return (
            f"店舗名: {self.store_name}\n"
            f"ジャンル: {self.store_genre}\n"
            f"訴求メニュー: {self.menu_name}\n"
            f"メニュー説明: {self.menu_description}\n"
            f"季節/イベント: {self.season_or_event}\n"
            f"ブランドトーン: {self.brand_tone}\n"
        )


def build_client() -> anthropic.Anthropic:
    """環境変数 ANTHROPIC_API_KEY からAPIクライアントを構築する。

    Raises:
        RuntimeError: ANTHROPIC_API_KEY が設定されていない場合。
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY が設定されていません。"
            "実際にAPIを呼び出すには `export ANTHROPIC_API_KEY=sk-...` を設定してください。"
        )
    return anthropic.Anthropic(api_key=api_key)


def generate_sns_drafts(
    brief: RestaurantBrief, brand_guideline: str, knowledge_digest: str = ""
) -> PipelineResult:
    """Advisor-Executorパイプラインを実行し、SNS投稿ドラフト一式を生成する。

    Args:
        brief: 店舗・商品情報。
        brand_guideline: ブランドイメージ・規約（景表法・薬機法上のNG表現等）。
            Advisorのレビュー基準として使われる。
        knowledge_digest: 既存スキル insta-food-buzz の知見ダイジェスト
            （skill_knowledge.build_knowledge_digest の戻り値）。指定した場合、
            Workerの計画立案・生成プロンプトとAdvisorのレビュー基準の両方に
            埋め込まれ、五感描写・冒頭フック等の品質基準が生成の核となる。

    Returns:
        PipelineResult: 計画・レビュー結果・最終ドラフトを含む実行結果。
    """
    client = build_client()
    worker = Worker(client)
    advisor = Advisor(client)
    agent = AdvisorExecutorAgent(worker=worker, advisor=advisor)

    worker_brief = brief.to_prompt_text()
    reviewer_guideline = brand_guideline
    if knowledge_digest:
        worker_brief = f"{knowledge_digest}\n\n【今回の店舗・商品情報】\n{worker_brief}"
        reviewer_guideline = f"{brand_guideline}\n\n{knowledge_digest}"

    return agent.run(
        brief=worker_brief,
        brand_guideline=reviewer_guideline,
        tool_schema=CONTENT_TOOL_SCHEMA,
    )


def format_result_as_markdown(brief: RestaurantBrief, result: PipelineResult) -> str:
    """パイプライン結果を報告用Markdownに整形する。"""
    content = result.final_content
    hashtags = " ".join(content.get("instagram_hashtags", []))
    lines = [
        f"# SNS投稿ドラフト: {brief.store_name}｜{brief.menu_name}",
        "",
        "## 実行サマリー",
        f"- Advisor呼び出し回数: {result.advisor_calls_used}",
        f"- エラー再試行回数: {result.error_retries_used}",
        f"- 品質レビュー後の修正: {'あり' if result.was_revised else 'なし'}",
        "",
        "## 計画レビュー（Advisor / Fable 5）",
        f"- 判定: {result.plan_review.verdict.value}",
        f"- フィードバック: {result.plan_review.feedback}",
        "",
        "## 採用計画（Worker / Sonnet 5）",
        result.plan,
        "",
        "## Instagram",
        "**キャプション**",
        "",
        content.get("instagram_caption", ""),
        "",
        "**ハッシュタグ**",
        "",
        hashtags,
        "",
        "## LINE公式アカウント",
        "",
        content.get("line_message", ""),
        "",
        "## 画像生成AIプロンプト（英語）",
        "",
        f"`{content.get('image_prompt_en', '')}`",
        "",
    ]
    if result.quality_review is not None:
        lines += [
            "## 品質レビュー（Advisor / Fable 5）",
            f"- 判定: {result.quality_review.verdict.value}",
            f"- フィードバック: {result.quality_review.feedback}",
            "",
        ]
    return "\n".join(lines)
