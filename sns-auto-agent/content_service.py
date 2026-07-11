"""飲食店向けSNS投稿ドラフト生成サービス。

Instagramキャプション＋ハッシュタグ、LINE公式アカウント配信文、
画像生成AI（Midjourney/DALL-E想定）用の英語プロンプトを一括生成する。
実際のモデル呼び出しは agent_core.AdvisorExecutorAgent に委譲する。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

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
class BrandRules:
    """店舗ごとに差し替え可能な運用ルール（トーン＆マナー・NGワード・文字数目安・必須ハッシュタグ）。

    insta-food-buzzの知見（五感描写・冒頭フック等「どう書くか」の品質基準）とは役割が異なり、
    こちらは「何を守るべきか」という店舗運用・コンプライアンス寄りの制約、および
    「その店ならではの何を伝えるか」というブランド固有の実質情報を担う。
    店舗が変われば、この一式を差し替えるだけで別ブランドの運用ルールに切り替えられる。
    """

    tone_and_manner: str
    """トーン＆マナーの指定（例: "上品で落ち着いたトーン。誇張表現は避ける"）。"""

    ng_words: list[str] = field(default_factory=list)
    """本文・ハッシュタグとも使用を禁止する語（例: 断定的最上級表現、薬機法抵触語）。"""

    instagram_char_range: tuple[int, int] = (150, 300)
    """Instagramキャプションの文字数目安（下限, 上限）。絵文字・改行を含む。"""

    line_char_range: tuple[int, int] = (100, 200)
    """LINEメッセージの文字数目安（下限, 上限）。"""

    mandatory_hashtags: list[str] = field(default_factory=list)
    """他のハッシュタグに加えて必ず含めるハッシュタグ（例: 店舗公式タグ）。"""

    brand_concept: str = ""
    """ブランドコンセプト・開業背景・立地の歴史的文脈など、投稿全体の世界観の軸。"""

    signature_menu_points: list[str] = field(default_factory=list)
    """看板メニューの構造・食べ方のこだわり（味変の順序等）・こだわりの産地/製法。"""

    pr_strengths: list[str] = field(default_factory=list)
    """SNS映え・職人技・厳選素材など、積極的に押し出すべきPRの強み。"""

    scene_appeals: list[str] = field(default_factory=list)
    """宴会・お祝い・テイクアウトなど、メニュー以外に訴求すべき利用シーン展開。"""

    instagram_focus: str = ""
    """Instagram固有の見せ方の指示（シズル感の言語化・看板メニューの強調・雰囲気の演出等）。"""

    line_focus: str = ""
    """LINE公式アカウント固有の見せ方の指示（呼びかけの口調・限定感の演出等）。"""

    image_prompt_focus: str = ""
    """画像生成AIプロンプト固有の描写指示（質感・光・湯気やタレなどのディテール）。"""

    def to_guideline_text(self) -> str:
        """Advisorのレビュー基準・Workerの生成プロンプトに埋め込むテキストへ変換する。"""
        ng = "、".join(self.ng_words) if self.ng_words else "指定なし"
        mandatory = " ".join(self.mandatory_hashtags) if self.mandatory_hashtags else "指定なし"
        ig_lo, ig_hi = self.instagram_char_range
        line_lo, line_hi = self.line_char_range

        lines = [
            f"・トーン＆マナー: {self.tone_and_manner}",
            f"・NGワード（本文・ハッシュタグとも使用禁止）: {ng}",
            f"・Instagramキャプション文字数目安: {ig_lo}〜{ig_hi}字（絵文字・改行含む）",
            f"・LINEメッセージ文字数目安: {line_lo}〜{line_hi}字",
            f"・必ず含めるハッシュタグ（他のハッシュタグに加えて必須）: {mandatory}",
        ]

        if self.brand_concept:
            lines.append(f"・ブランドコンセプト: {self.brand_concept}")

        if self.signature_menu_points:
            points = "\n".join(f"  - {p}" for p in self.signature_menu_points)
            lines.append(f"・看板メニューのこだわり:\n{points}")

        if self.pr_strengths:
            strengths = "\n".join(f"  - {p}" for p in self.pr_strengths)
            lines.append(f"・積極的に押し出すPRの強み:\n{strengths}")

        if self.scene_appeals:
            scenes = "\n".join(f"  - {p}" for p in self.scene_appeals)
            lines.append(f"・メニュー以外に訴求すべき利用シーン:\n{scenes}")

        if self.instagram_focus:
            lines.append(f"・Instagram固有の見せ方: {self.instagram_focus}")

        if self.line_focus:
            lines.append(f"・LINE公式アカウント固有の見せ方: {self.line_focus}")

        if self.image_prompt_focus:
            lines.append(f"・画像生成AIプロンプト固有の描写指示: {self.image_prompt_focus}")

        return "\n".join(lines) + "\n"


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
    """APIクライアントを構築する。

    まず `sns-auto-agent/.env`（このモジュールと同じディレクトリ）を読み込み、
    未設定のキーのみ環境変数で補う。シェルの `export` は呼び出しプロセスが
    別であれば引き継がれないため、`.env` を優先的な設定手段とする
    （`.env` はリポジトリの `.gitignore` で除外済み、コミットされない）。

    Raises:
        RuntimeError: ANTHROPIC_API_KEY が どちらの方法でも設定されていない場合。
    """
    try:
        from dotenv import load_dotenv

        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        load_dotenv(env_path, override=False)
    except ImportError:
        pass  # python-dotenv 未インストールでも環境変数のみで動作可能にする

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY が見つかりません。次のいずれかで設定してください。\n"
            "  1) sns-auto-agent/.env に ANTHROPIC_API_KEY=sk-... を1行追加する（推奨）\n"
            "  2) 同一プロセス内で `export ANTHROPIC_API_KEY=sk-...` してから実行する"
        )
    return anthropic.Anthropic(api_key=api_key)


def generate_sns_drafts(
    brief: RestaurantBrief,
    brand_guideline: str,
    knowledge_digest: str = "",
    brand_rules: Optional[BrandRules] = None,
) -> PipelineResult:
    """Advisor-Executorパイプラインを実行し、SNS投稿ドラフト一式を生成する。

    Args:
        brief: 店舗・商品情報。
        brand_guideline: ブランドイメージ・規約（景表法・薬機法上のNG表現等）。
            Advisorのレビュー基準として使われる。
        knowledge_digest: 既存スキル insta-food-buzz の知見ダイジェスト
            （skill_knowledge.build_knowledge_digest の戻り値）。指定した場合、
            Workerの計画立案・生成プロンプトとAdvisorのレビュー基準の両方に
            埋め込まれ、五感描写・冒頭フック等「どう書くか」の品質基準が生成の核となる。
        brand_rules: 店舗別に差し替え可能な運用ルール（トーン＆マナー・NGワード・
            文字数目安・必須ハッシュタグ）。指定した場合、insta-food-buzzの知見に
            重ねて、「何を守るか」という運用上の制約としてWorker/Advisor双方に適用される。

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
        reviewer_guideline = f"{reviewer_guideline}\n\n{knowledge_digest}"

    if brand_rules is not None:
        rules_text = brand_rules.to_guideline_text()
        worker_brief = f"{worker_brief}\n\n【店舗別運用ルール（必ず遵守）】\n{rules_text}"
        reviewer_guideline = f"{reviewer_guideline}\n\n【店舗別運用ルール（必ず遵守）】\n{rules_text}"

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
