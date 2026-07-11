"""デモ実行スクリプト。

大濠うなぎの7つのコンテンツバリエーション（メニュー訴求・団体向け・
テイクアウト・ランチ用・ディナー用・コース紹介・地域密着＆周辺紹介・
鰻の豆知識）を入力し、main.generate_content が組み立てる Advisor-Executor
パイプライン（Worker=Sonnet 5 / Advisor=Fable 5、insta-food-buzzの知見を
統合済み）をカテゴリごとに実行し、Instagram・LINE・X（旧Twitter・140字以内）・
Threads（500字以内）・画像生成AIプロンプトの5種ドラフトをMarkdown形式で出力する。

環境変数 ANTHROPIC_API_KEY が設定されていれば実際にAPIを呼び出す。
未設定の場合は、パイプラインの制御フロー（計画レビュー→生成→品質レビュー）と
出力フォーマットのみを、カテゴリごとに内容を変えた決定的なダミー値で再現する
「モックモード」で実行し、その旨を明示する（AI生成であるかのように偽装しない）。

生成したドラフトは画面出力と同時に `outputs/YYYY-MM/YYYY-MM-DD_カテゴリ名.md`
へ自動保存される（カレンダー自動ストック機能）。投稿予定日は `--date` で
指定でき、省略時は実行当日の日付が使われる。

実行方法:
    python3 run_demo.py                          # 全7バリエーションを実行当日の日付で一括生成・保存
    python3 run_demo.py all --date 2026-07-15    # 全7バリエーションを2026-07-15付けで生成・保存
    python3 run_demo.py takeout --date 2026-07-20  # テイクアウトのみ2026-07-20付けで生成・保存

    # 実APIを使う場合:
    export ANTHROPIC_API_KEY=sk-...
    python3 run_demo.py
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

from agent_core import AdvisorReview, AdvisorVerdict, PipelineResult
from content_service import (
    CATEGORY_LABELS,
    ContentCategory,
    RestaurantBrief,
    SimulationEntry,
    build_category_date_arg_parser,
    build_list_arg_parser,
    build_simulate_arg_parser,
    format_result_as_markdown,
    format_simulation_summary,
    format_stock_summary,
    get_category_for_weekday,
    optimize_instagram_hashtags,
    save_draft_to_calendar,
    scan_output_stock,
)
from main import CATEGORY_BRIEFS, SAMPLE_BRAND_RULES, resolve_categories, generate_content

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
        "x_post": (
            "炭火焼きのうなぎを氷水で締め、キリッと冷えた緑茶だしで。"
            "夏だけの「うなぎ冷やし茶漬け」、なくなり次第終了です。🍵 #大濠うなぎ"
        ),
        "threads_post": (
            "暑さで食欲が落ちる季節にこそ、試してほしい一杯があります。\n\n"
            "炭火でじっくり焼き上げたうなぎを氷水でキュッと締め、冷たい緑茶だしをたっぷりと。"
            "大葉・みょうが・白胡麻の薬味と、仕上げの山葵がアクセントになる、"
            "大濠うなぎの夏季限定「特製うなぎ冷やし茶漬け」です。\n\n"
            "炭火の香ばしさと、だしの涼やかさ。ひと口ごとに温度差が心地よく効いてきます。"
            "なくなり次第終了なので、気になった方はお早めに。\n\n"
            "冷たい茶漬け派、それとも熱々派？"
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
        "x_post": (
            "幹事さんへ。飲み放題付き「鰻と和牛のすき焼きコース」なら安心して盛り上がれます。"
            "冬はせり鍋、春は柳川鍋にも変更可。大濠うなぎ🍶"
        ),
        "threads_post": (
            "宴会の幹事って、実はいちばん気を遣う役目ですよね。\n\n"
            "大濠うなぎの「鰻と和牛のすき焼きコース」は、飲み放題付きで安心してお任せいただけます。"
            "炭火焼きのうなぎと和牛がぐつぐつ煮える贅沢な一鍋。"
            "冬は「せり鍋」、春は「柳川鍋」と季節替わりの選択肢もあり、"
            "大人数でも個室感を保ちながら盛り上がれます。\n\n"
            "次の宴会の幹事、任されていませんか？"
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
        "x_post": (
            "蓋を開けた瞬間、炭火の香りがふわり。「謹製 うな重弁当」はご自宅にも手土産にも。"
            "慶事には紅白うなぎ仕立てもご用意できます。大濠うなぎ🎁"
        ),
        "threads_post": (
            "手土産に何を持っていくか迷ったとき、思い出してほしいメニューです。\n\n"
            "大濠うなぎの「謹製 うな重弁当」は、焼き立てのうなぎを崩さず持ち帰れるよう仕立てた"
            "謹製重箱。蓋を開けた瞬間、炭火の香りがふわりと立ちのぼります。"
            "ご自宅用にはもちろん、大切な方への手土産にも。慶事には紅白うなぎ仕立てもご用意できます。\n\n"
            "ご予約制です。次のお祝い事の手土産、決まりましたか？"
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
        "x_post": (
            "平日のご褒美に、日替わりうな重ランチ。夜より気軽な価格で、"
            "釜炊きご飯と炭火焼きのうなぎをひとりでも。大濠うなぎ🍱"
        ),
        "threads_post": (
            "平日のランチに、ちょっとした贅沢を挟みたくなる日ってありませんか。\n\n"
            "大濠うなぎの「日替わりうな重ランチ」は、夜より気軽な価格帯で楽しめる平日限定メニュー。"
            "釜炊きご飯に炭火焼きのうなぎを乗せて、お一人様でもふらっと立ち寄れる雰囲気で"
            "お待ちしています。\n\n"
            "今週、自分にご褒美ランチはいかがですか？"
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
        "x_post": (
            "炭火でじっくり焼き上げた白焼きに、ふわとろのう巻き。"
            "日本酒の一杯とともに、静かな夜のひとときを。大濠うなぎ🌙"
        ),
        "threads_post": (
            "静かに過ごしたい夜、ありますよね。\n\n"
            "大濠うなぎの夜のお品書きには、白焼きとう巻きをご用意しています。"
            "炭火でじっくり焼き上げた白焼きはタレを使わない分、うなぎ本来の香りと脂の甘みが"
            "際立ちます。ふわとろのう巻きと合わせて、日本酒や焼酎とともにゆったりとした"
            "時間をどうぞ。\n\n"
            "今夜の一杯、何にしますか？"
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
        "x_post": (
            "宴会にはすき焼きコース、冬はせり鍋、春は柳川鍋、慶事には紅白うなぎコース。"
            "シーンに合わせて選べる大濠うなぎのコース一覧です。"
        ),
        "threads_post": (
            "「どのコースを選べばいいか分からない」というお声をよくいただくので、"
            "まとめてご紹介します。\n\n"
            "宴会・夜の会食には「鰻と和牛のすき焼きコース」（飲み放題付き）。"
            "冬は「せり鍋」、春は「柳川鍋」に変更も可能です。お祝い事には「紅白うなぎコース」。"
            "ご自宅用・贈答用には「謹製 うな重弁当」のテイクアウトも。\n\n"
            "どれも炭火焼きのうなぎが主役です。シーンを教えていただければ、"
            "ぴったりの一品をご提案します。"
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
        "x_post": (
            "大濠公園のお堀を渡る風、隣接する舞鶴公園から続く福岡城跡の石垣。"
            "散歩やジョギングのあとに立ち寄れる距離に、大濠うなぎはあります。"
        ),
        "threads_post": (
            "大濠公園の魅力を、少しだけ紹介させてください。\n\n"
            "お堀の水面に映る空、四季折々の木々、隣接する舞鶴公園から続く福岡城跡の石垣。"
            "散歩やジョギングで汗を流す人たちの姿は、この街の日常の風景です。\n\n"
            "私たち大濠うなぎは、そのお堀からほど近い場所にあります。"
            "散策の帰りに、少し贅沢な一杯はいかがですか。\n\n"
            "お気に入りの散歩コース、教えてください。"
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
        "x_post": (
            "「串打ち三年、裂き八年、焼き一生」。うなぎ職人の技は炭火の熱を読む感覚に"
            "集約されます。皮はパリッと、身はふっくら。大濠うなぎ📖"
        ),
        "threads_post": (
            "うなぎの世界には「串打ち三年、裂き八年、焼き一生」という言葉があります。\n\n"
            "串打ち・裂きの技術を習得するだけでも長い年月がかかり、"
            "最後の「焼き」に至っては一生かけて極める領域だと言われています。"
            "炭火の熱を読み、皮はパリッと・身はふっくらに仕上げる火加減の積み重ね。\n\n"
            "大濠うなぎでは、その火加減にこだわり抜いた炭火焼きを、"
            "釜炊きご飯とともにお楽しみいただけます。\n\n"
            "この豆知識、誰かに話したくなりませんでしたか？"
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
    optimized_hashtags = optimize_instagram_hashtags(
        category, SAMPLE_BRAND_RULES, spec["instagram_hashtags"]
    )
    draft = {
        "instagram_caption": f"[モックドラフト：実際のAI生成ではありません / {label}]\n{spec['instagram_caption']}",
        "instagram_hashtags": optimized_hashtags,
        "line_message": f"[モック] {spec['line_message']}",
        "image_prompt_en": f"[MOCK / {label}] {spec['image_prompt_en']}",
        "x_post": f"[モック] {spec['x_post']}",
        "threads_post": f"[モック] {spec['threads_post']}",
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


def _generate_result(category: ContentCategory, brief: RestaurantBrief) -> tuple[PipelineResult, str]:
    """1カテゴリ分の `PipelineResult` を、live優先・未設定/失敗時はmockへ安全に
    フォールバックしつつ取得する。`_run_one` と `_run_simulate` で共有するロジック。

    Returns:
        tuple[PipelineResult, str]: 実行結果と、表示用の実行モード文字列。
    """
    api_key_present = bool(os.environ.get("ANTHROPIC_API_KEY"))
    label = CATEGORY_LABELS[category]

    if api_key_present:
        try:
            return generate_content(brief), "live（実API呼び出し）"
        except Exception as exc:  # noqa: BLE001 - 実行失敗時はモックへ安全にフォールバックする
            print(
                f"[警告] {label}: 実API呼び出しに失敗したためモックモードにフォールバックします: {exc}",
                file=sys.stderr,
            )
            return _build_mock_result(category), "mock（実API呼び出し失敗によるフォールバック）"

    return _build_mock_result(category), "mock（ANTHROPIC_API_KEY未設定）"


def _run_one(category: ContentCategory, brief: RestaurantBrief, post_date: date) -> None:
    """1カテゴリ分のパイプライン（live優先・失敗/未設定時はmock）を実行し、
    画面出力とカレンダーフォルダへの自動保存の両方を行う。
    """
    label = CATEGORY_LABELS[category]
    result, mode = _generate_result(category, brief)

    print(f"<!-- カテゴリ: {label} / 投稿予定日: {post_date.isoformat()} / 実行モード: {mode} -->\n")
    markdown = format_result_as_markdown(brief, result)
    print(markdown)

    saved_path = save_draft_to_calendar(markdown, category, post_date)
    print(f"\n[保存完了] {saved_path}", file=sys.stderr)


def _run_simulate(start_date: date, days: int) -> list[SimulationEntry]:
    """曜日別スケジュールルールに基づき、`start_date` から `days` 日分のドラフトを
    （live優先・未設定/失敗時はmockで）一括生成し、カレンダーフォルダへ自動保存する。

    Args:
        start_date: シミュレーションを開始する投稿予定日。
        days: 生成する期間の日数（1以上）。

    Returns:
        list[SimulationEntry]: 各日の投稿予定日・割り当てカテゴリ・保存先パス。
    """
    entries: list[SimulationEntry] = []
    for offset in range(days):
        target_date = start_date + timedelta(days=offset)
        category = get_category_for_weekday(target_date)
        brief = CATEGORY_BRIEFS[category]
        result, _mode = _generate_result(category, brief)
        markdown = format_result_as_markdown(brief, result)
        saved_path = save_draft_to_calendar(markdown, category, target_date)
        entries.append(SimulationEntry(post_date=target_date, category=category, file_path=saved_path))
    return entries


def main() -> int:
    """デモを実行し、指定カテゴリ（省略時は全7バリエーション）の生成結果を
    指定した投稿予定日（省略時は実行当日）でMarkdownとして標準出力へ表示すると同時に、
    カレンダーフォルダ（outputs/YYYY-MM/YYYY-MM-DD_カテゴリ名.md）へ自動保存する。
    先頭引数が "list" の場合は、生成を行わず `outputs/` のストック一覧を表示する。
    先頭引数が "simulate" の場合は、曜日別スケジュールルールに基づき指定期間分を
    （live優先・未設定/失敗時はmockで）一括生成・保存する。

    実行方法:
        python3 run_demo.py list                    # 保存済みストックを全期間一覧表示
        python3 run_demo.py list --month 2026-07    # 2026年7月のストックのみ一覧表示
        python3 run_demo.py list --date 2026-07-12  # 投稿予定日で絞り込んで一覧表示
        python3 run_demo.py simulate --days 7       # 実行当日から1週間分を曜日別ルールで一括生成・保存
        python3 run_demo.py simulate --start-date 2026-08-01 --days 3  # 期間・開始日を指定
    """
    argv = sys.argv[1:]

    if argv and argv[0] == "list":
        list_parser = build_list_arg_parser("run_demo.py")
        list_args = list_parser.parse_args(argv[1:])
        entries = scan_output_stock(month=list_args.month, target_date=list_args.filter_date)
        print(format_stock_summary(entries))
        return 0

    if argv and argv[0] == "simulate":
        simulate_parser = build_simulate_arg_parser("run_demo.py")
        simulate_args = simulate_parser.parse_args(argv[1:])
        start_date: date = simulate_args.start_date or date.today()
        entries = _run_simulate(start_date, simulate_args.days)
        print(format_simulation_summary(entries))
        return 0

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "[情報] ANTHROPIC_API_KEY が未設定のため、モックモードで"
            "パイプラインの構造と出力フォーマットのみを検証します。"
            "実際のAI生成を行うには ANTHROPIC_API_KEY を設定してください。",
            file=sys.stderr,
        )

    parser = build_category_date_arg_parser("run_demo.py")
    args = parser.parse_args(argv)
    post_date: date = args.post_date or date.today()

    categories = resolve_categories(args.category)

    for i, category in enumerate(categories):
        if i > 0:
            print("\n---\n")
        _run_one(category, CATEGORY_BRIEFS[category], post_date)
    return 0


if __name__ == "__main__":
    sys.exit(main())
