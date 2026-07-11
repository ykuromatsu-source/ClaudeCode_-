"""デモ実行スクリプト。

大濠うなぎの7つのコンテンツバリエーション（メニュー訴求・団体向け・
テイクアウト・ランチ用・ディナー用・コース紹介・地域密着＆周辺紹介・
鰻の豆知識）を入力し、main.generate_content が組み立てる Advisor-Executor
パイプライン（Worker=Sonnet 5 / Advisor=Fable 5、insta-food-buzzの知見を
統合済み）をカテゴリごとに実行し、Markdown形式で最終ドラフトを出力する。

環境変数 ANTHROPIC_API_KEY が設定されていれば実際にAPIを呼び出す。
未設定の場合は、パイプラインの制御フロー（計画レビュー→生成→品質レビュー）と
出力フォーマットのみを、カテゴリごとに内容を変えた決定的なダミー値で再現する
「モックモード」で実行し、その旨を明示する（AI生成であるかのように偽装しない）。

実行方法:
    python3 run_demo.py            # 全7バリエーションを一括シミュレーション
    python3 run_demo.py all        # 同上
    python3 run_demo.py takeout    # テイクアウトのみ（ContentCategory.valueを指定）

    # 実APIを使う場合:
    export ANTHROPIC_API_KEY=sk-...
    python3 run_demo.py
"""

from __future__ import annotations

import os
import sys

from agent_core import AdvisorReview, AdvisorVerdict, PipelineResult
from content_service import CATEGORY_LABELS, ContentCategory, RestaurantBrief, format_result_as_markdown
from main import CATEGORY_BRIEFS, resolve_categories_from_argv, generate_content

# カテゴリごとに切り替えるモックドラフトの中身。
# 実際のAI生成は一切行わないが、7バリエーションで内容が固定の使い回しに
# ならないよう、カテゴリのテーマに沿ったプレースホルダーを個別に用意する。
_MOCK_DRAFTS: dict[ContentCategory, dict[str, object]] = {
    ContentCategory.MENU_PROMOTION: {
        "plan": (
            "炭火焼きの香ばしさと氷水締めの涼やかさという温度・食感の対比を軸に、"
            "上品で落ち着いたトーンで「夏季限定」を明確に打ち出す。"
        ),
        "instagram_caption": (
            "炭火の香りをまとったうなぎを、キリッと冷たい緑茶だしにくぐらせて。"
            "ひと口ごとに、香ばしさと涼やかさが交互にやってくる、夏だけの一杯です。"
            "なくなり次第終了。"
        ),
        "instagram_hashtags": ["#大濠うなぎ", "#福岡グルメ", "#うなぎ茶漬け", "#夏季限定メニュー", "#福岡うなぎ"],
        "line_message": (
            "🍵夏季限定「特製うなぎ冷やし茶漬け」始めました。"
            "炭火焼きの香ばしさ×冷たい緑茶だし。なくなり次第終了です。"
        ),
        "image_prompt_en": (
            "A bowl of chilled unagi ochazuke, char-grilled eel glistening over rice, "
            "cold green tea broth poured tableside, shiso and myoga garnish, elegant Japanese "
            "restaurant lighting, macro food photography, appetizing texture detail"
        ),
    },
    ContentCategory.GROUP_DINING: {
        "plan": (
            "幹事目線の「安心して任せられる」を核に、飲み放題付き宴会コースと"
            "季節替わりの鍋（冬:せり鍋／春:柳川鍋）で選べる楽しさを訴求する。"
        ),
        "instagram_caption": (
            "炭火の煙が立ちのぼる個室で、和牛とうなぎがぐつぐつ煮える贅沢すき焼きコース。"
            "飲み放題付きだから、幹事さんも安心して盛り上がれる一夜に。"
            "冬はせり鍋、春は柳川鍋にも変えられます。"
        ),
        "instagram_hashtags": ["#大濠うなぎ", "#福岡グルメ", "#福岡宴会", "#すき焼きコース", "#福岡飲み会"],
        "line_message": (
            "🍶宴会シーズン、幹事さんへ。飲み放題付き「鰻と和牛のすき焼きコース」で"
            "安心の一席をご用意します。冬はせり鍋、春は柳川鍋も選べます。"
        ),
        "image_prompt_en": (
            "A private tatami dining room, sukiyaki pot bubbling with wagyu beef and unagi, "
            "steam rising, warm izakaya lighting, group of guests toasting in soft focus, "
            "cozy izakaya atmosphere"
        ),
    },
    ContentCategory.TAKEOUT: {
        "plan": (
            "焼き立てを崩さず持ち帰れる工夫と、慶事向け「紅白うなぎ」重箱の特別感を軸に、"
            "手土産・贈答としての魅力を伝える。"
        ),
        "instagram_caption": (
            "蓋を開けた瞬間、炭火の香りがふわりと立ちのぼる「謹製 うな重弁当」。"
            "ご自宅でも、大切な方への手土産にも。慶事には紅白うなぎ仕立てもご用意しています。"
        ),
        "instagram_hashtags": ["#大濠うなぎ", "#福岡グルメ", "#うな重弁当", "#福岡テイクアウト", "#紅白うなぎ"],
        "line_message": (
            "🎁ご自宅用にも手土産にも。「謹製 うな重弁当」はご予約制です。"
            "慶事には紅白うなぎ仕立てもご用意できます。"
        ),
        "image_prompt_en": (
            "An elegant lacquered bento box of unagi kabayaki over rice, opened to reveal "
            "glistening glaze, gift wrapping cloth beside it, soft natural window light, "
            "premium Japanese takeout photography"
        ),
    },
    ContentCategory.LUNCH: {
        "plan": (
            "「少し贅沢な日常使い」を体現する自分へのご褒美ランチとして、"
            "気軽さと満足感の両立を軸に描く。"
        ),
        "instagram_caption": (
            "平日のご褒美に、日替わりうな重ランチ。夜より気軽な価格で、"
            "釜炊きご飯と炭火焼きのうなぎをひとりでもふらっと楽しめます。"
        ),
        "instagram_hashtags": ["#大濠うなぎ", "#福岡グルメ", "#うなぎランチ", "#福岡ランチ", "#ご褒美ランチ"],
        "line_message": (
            "🍱平日限定「日替わりうな重ランチ」。お一人様でも入りやすい雰囲気で"
            "お待ちしています。"
        ),
        "image_prompt_en": (
            "A neat lunch set of unagi don with rice, simple modern Japanese tableware, "
            "bright daytime restaurant light through a window, single diner mood, "
            "clean minimal food photography"
        ),
    },
    ContentCategory.DINNER: {
        "plan": (
            "白焼き・う巻きとお酒のペアリングを軸に、落ち着いた大人の夜の時間を演出する。"
        ),
        "instagram_caption": (
            "炭火でじっくり焼き上げた白焼きに、ふわとろのう巻き。"
            "日本酒の一杯とともに、静かに夜を楽しむひとときを。"
        ),
        "instagram_hashtags": ["#大濠うなぎ", "#福岡グルメ", "#白焼き", "#う巻き", "#福岡日本酒"],
        "line_message": (
            "🌙夜のお品書きに、白焼きとう巻きをご用意しています。"
            "日本酒・焼酎とともにゆったりとした時間をどうぞ。"
        ),
        "image_prompt_en": (
            "Shirayaki grilled eel without sauce on a small plate, delicate char marks, "
            "a glass of Japanese sake beside it, dim warm izakaya lighting, "
            "moody evening dining atmosphere"
        ),
    },
    ContentCategory.COURSE_INTRODUCTION: {
        "plan": (
            "単品の深掘りではなく、シーン別に選べるコースの全体像を一覧的に見せることを"
            "優先する。"
        ),
        "instagram_caption": (
            "大濠うなぎのコースは、シーンに合わせて選べます。"
            "宴会には「鰻と和牛のすき焼きコース」、冬は「せり鍋」、春は「柳川鍋」、"
            "慶事には「紅白うなぎコース」。どれも炭火焼きのうなぎが主役です。"
        ),
        "instagram_hashtags": ["#大濠うなぎ", "#福岡グルメ", "#福岡コース料理", "#すき焼きコース", "#紅白うなぎ"],
        "line_message": (
            "📋コースのご案内。すき焼き・せり鍋・柳川鍋・紅白うなぎ、シーンに合わせて"
            "お選びいただけます。"
        ),
        "image_prompt_en": (
            "A flat lay of four Japanese course meal highlights — sukiyaki, seri nabe, "
            "yanagawa nabe, and red-and-white unagi — arranged elegantly on a dark wooden "
            "table, editorial food photography, soft studio lighting"
        ),
    },
    ContentCategory.LOCAL_AREA_GUIDE: {
        "plan": (
            "地域そのものを主役にした読物として、大濠公園・舞鶴公園・福岡城跡の魅力を"
            "紹介し、店舗の紹介は最後にさりげなく添える。"
        ),
        "instagram_caption": (
            "大濠公園のお堀を渡る風、隣接する舞鶴公園から続く福岡城跡の石垣。"
            "散歩やジョギングで汗を流したあとに立ち寄れる距離に、私たち大濠うなぎは"
            "あります。"
        ),
        "instagram_hashtags": ["#大濠公園", "#福岡城跡", "#舞鶴公園", "#福岡散歩", "#大濠うなぎ"],
        "line_message": (
            "🌳大濠公園のお散歩帰りに。福岡城跡・舞鶴公園も歩いて楽しめるエリアです。"
        ),
        "image_prompt_en": (
            "A serene lakeside park path around Ohori Park with reflections on the water, "
            "historic Fukuoka Castle ruins stone walls in the background, golden hour light, "
            "travel photography style, no people in focus"
        ),
    },
    ContentCategory.TRIVIA: {
        "plan": (
            "宣伝色を抑え、うなぎの旬・炭火焼き職人の技・釜炊きご飯へのこだわりを"
            "雑学として読ませてから、自然に店舗へつなげる。"
        ),
        "instagram_caption": (
            "「串打ち三年、裂き八年、焼き一生」。うなぎ職人の技は、炭火の熱を読む"
            "感覚に集約されます。皮はパリッと、身はふっくら。その火加減の積み重ねを、"
            "釜炊きご飯の一膳とともにお楽しみください。"
        ),
        "instagram_hashtags": ["#うなぎ豆知識", "#炭火焼き", "#職人技", "#大濠うなぎ", "#福岡グルメ"],
        "line_message": (
            "📖うなぎ豆知識：「焼き一生」と言われる炭火焼きの技術、ご存知でしたか？"
        ),
        "image_prompt_en": (
            "A skilled Japanese chef grilling unagi over binchotan charcoal, close-up of "
            "hands and glowing embers, smoke rising, traditional craftsmanship photography, "
            "warm dramatic lighting"
        ),
    },
}


def _build_mock_result(category: ContentCategory) -> PipelineResult:
    """ANTHROPIC_API_KEY未設定時に、指定カテゴリのパイプライン構造のみを再現するダミー結果を作る。

    実際のAI生成は一切行わない。Advisor-Executorのフロー
    （計画立案→計画レビュー→生成→品質レビュー→自律修正）と、
    format_result_as_markdown による出力フォーマットを検証するためだけの
    決定的なプレースホルダー値であり、カテゴリごとにテーマへ即した内容を返す。
    """
    label = CATEGORY_LABELS[category]
    spec = _MOCK_DRAFTS[category]
    plan = f"[モック / {label}] {spec['plan']}"
    plan_review = AdvisorReview(
        verdict=AdvisorVerdict.APPROVED,
        feedback=f"[モック] {label}の切り口とブランドトーンとの整合に問題なし。承認。",
    )
    draft = {
        "instagram_caption": f"[モックドラフト：実際のAI生成ではありません / {label}]\n{spec['instagram_caption']}",
        "instagram_hashtags": spec["instagram_hashtags"],
        "line_message": f"[モック] {spec['line_message']}",
        "image_prompt_en": f"[MOCK / {label}] {spec['image_prompt_en']}",
    }
    quality_review = AdvisorReview(
        verdict=AdvisorVerdict.APPROVED,
        feedback=f"[モック] {label}として五感描写・カテゴリ方針ともに基準を満たしている。",
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


def _run_one(category: ContentCategory, brief: RestaurantBrief) -> None:
    """1カテゴリ分のパイプライン（live優先・失敗/未設定時はmock）を実行し出力する。"""
    api_key_present = bool(os.environ.get("ANTHROPIC_API_KEY"))
    label = CATEGORY_LABELS[category]

    if api_key_present:
        try:
            result = generate_content(brief)
            mode = "live（実API呼び出し）"
        except Exception as exc:  # noqa: BLE001 - 実行失敗時はモックへ安全にフォールバックする
            print(
                f"[警告] {label}: 実API呼び出しに失敗したためモックモードにフォールバックします: {exc}",
                file=sys.stderr,
            )
            result = _build_mock_result(category)
            mode = "mock（実API呼び出し失敗によるフォールバック）"
    else:
        result = _build_mock_result(category)
        mode = "mock（ANTHROPIC_API_KEY未設定）"

    print(f"<!-- カテゴリ: {label} / 実行モード: {mode} -->\n")
    print(format_result_as_markdown(brief, result))


def main() -> int:
    """デモを実行し、指定カテゴリ（省略時は全7バリエーション）の生成結果をMarkdownで標準出力へ表示する。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "[情報] ANTHROPIC_API_KEY が未設定のため、モックモードで"
            "パイプラインの構造と出力フォーマットのみを検証します。"
            "実際のAI生成を行うには ANTHROPIC_API_KEY を設定してください。",
            file=sys.stderr,
        )

    try:
        categories = resolve_categories_from_argv(sys.argv[1:])
    except ValueError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    for i, category in enumerate(categories):
        if i > 0:
            print("\n---\n")
        _run_one(category, CATEGORY_BRIEFS[category])
    return 0


if __name__ == "__main__":
    sys.exit(main())
