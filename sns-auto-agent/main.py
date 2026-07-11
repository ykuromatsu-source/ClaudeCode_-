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
    BrandRules,
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

# 店舗別の運用ルール（トーン＆マナー・NGワード・文字数目安・必須ハッシュタグ・
# ブランドコンセプト・看板メニューのこだわり・PRの強み・シーン展開・媒体別の見せ方）。
# 店舗が変わる場合はこのプリセットを差し替えるだけでよい。
SAMPLE_BRAND_RULES = BrandRules(
    tone_and_manner="本物の日本の味を伝える、上品で落ち着いたトーン。過度にくだけた表現・煽り文句は避ける。",
    ng_words=["最高", "絶対", "日本一", "激安", "爆盛り", "神"],
    instagram_char_range=(150, 300),
    line_char_range=(100, 180),
    mandatory_hashtags=["#大濠うなぎ", "#福岡グルメ"],
    brand_concept=(
        "「うなぎ文化をもっと身近に」。2025年8月オープン。特別な日だけでなく、"
        "少し贅沢な日常使いに寄り添う専門店を目指す。大濠のお堀周辺が持つ歴史的背景を"
        "現代に再解釈した店づくりであり、高級店のような堅苦しい構えではなく、"
        "質は極めて高いが親しみやすい雰囲気を大切にする。"
    ),
    signature_menu_points=[
        "看板メニューは、釜炊きご飯の上に炭火焼きのうなぎを乗せた「釜まぶし」。",
        "食べ方は3段階の味変が基本: 1杯目はそのまま、2杯目は薬味とともに、"
        "3杯目は出汁茶漬けにして楽しむ。",
        "国産うなぎと釜炊きご飯を厳選して使用している。",
    ],
    pr_strengths=[
        "福岡名物の明太子を丸ごと乗せた「明太白釜まぶし」（SNS映えする看板の一皿）。",
        "職人による炭火焼き（皮目はパリッと、身はふっくら柔らかい仕上げ）。",
    ],
    scene_appeals=[
        "宴会・夜の会食: 「鰻と和牛のすき焼きコース」、冬のせり鍋、春の柳川鍋、飲み放題あり。",
        "お祝い事: 紅白うなぎ。",
        "テイクアウト: 謹製 うな重弁当。",
    ],
    instagram_focus=(
        "炭火焼きの煙や滴るタレといった「シズル感」を具体的な言葉で描写すること。"
        "看板メニュー「釜まぶし」の特長（3段階の味変等）を強調すること。"
        "お一人様でもグループでも「入りやすい雰囲気」が伝わる演出を心がけること。"
    ),
    line_focus=(
        "丁寧かつ親しみやすい口調で呼びかけること。夜の会食（宴会コース等）の"
        "便利さをアピールすること。限定情報感（今だけ・友だち限定等）を演出すること。"
    ),
    image_prompt_focus=(
        "滴るタレ、炭火の煙、和モダンな店内の質感が最高のクオリティで描画されるよう、"
        "光の当たり方・素材の照り・湯気や煙の立ち方まで具体的に英語で描写すること。"
    ),
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
        brief,
        brand_guideline=LEGAL_AND_BRAND_GUIDELINE,
        knowledge_digest=digest,
        brand_rules=SAMPLE_BRAND_RULES,
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
