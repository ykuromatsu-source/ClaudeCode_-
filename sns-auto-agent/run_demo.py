"""デモ実行スクリプト。

大濠うなぎ（既存・8カテゴリ）・舞鶴公園BBQ（新規・4カテゴリ）・小戸BBQ事業
（新規・4カテゴリ）の3事業を入力し、main.generate_content_for_business が
組み立てる Advisor-Executor パイプライン（Worker=Sonnet 5 / Advisor=Fable 5、
insta-food-buzzの知見を統合済み）を事業・カテゴリごとに実行し、Instagram・
LINE・X（旧Twitter・140字以内）・Threads（500字以内）・画像生成AIプロンプトの
5種ドラフトをMarkdown形式で出力する。BBQ事業には季節・天候・ロケーションの
動的コンテキスト（DynamicContext）も自動注入される。

環境変数 ANTHROPIC_API_KEY が設定されていれば実際にAPIを呼び出す（ネットワーク
エラー・レートリミット等はagent_core側でExponential Backoffにより自動リトライ）。
未設定の場合は、パイプラインの制御フロー（計画レビュー→生成→品質レビュー）と
出力フォーマットのみを、事業・カテゴリごとに内容を変えた決定的なダミー値で
再現する「モックモード」で実行し、その旨を明示する（AI生成であるかのように
偽装しない）。

生成したドラフトは画面出力と同時に `outputs/YYYY-MM-DD/事業スラグ_カテゴリ名.md`
へ自動保存される（カレンダー自動ストック機能）。投稿予定日は `--date` で
指定でき、省略時は実行当日の日付が使われる。

実行方法:
    python3 run_demo.py                                   # 大濠うなぎの全バリエーションを実行当日の日付で一括生成・保存
    python3 run_demo.py all --date 2026-07-15             # 大濠うなぎの全バリエーションを2026-07-15付けで生成・保存
    python3 run_demo.py takeout --date 2026-07-20           # 大濠うなぎのテイクアウトのみ生成・保存
    python3 run_demo.py all --business maizuru_bbq         # 舞鶴公園BBQの全カテゴリを一括生成・保存
    python3 run_demo.py menu_promotion --business odo_bbq   # 小戸BBQ事業のメニュー訴求のみ生成・保存
    python3 run_demo.py all --business all --date 2026-07-25  # 全3事業 × 対応カテゴリを一括生成・保存

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
    BUSINESS_LABELS,
    CATEGORY_LABELS,
    DEFAULT_EXPORT_PATH,
    Business,
    ContentCategory,
    RestaurantBrief,
    SimulationEntry,
    build_category_date_arg_parser,
    build_check_arg_parser,
    build_export_arg_parser,
    build_list_arg_parser,
    build_simulate_arg_parser,
    export_stocked_drafts,
    format_result_as_markdown,
    format_simulation_summary,
    format_stock_summary,
    format_validation_summary,
    get_category_for_weekday,
    optimize_instagram_hashtags,
    prompt_menu_choice,
    prompt_optional_date,
    prompt_optional_month,
    prompt_positive_int,
    prompt_text_with_default,
    save_draft_to_calendar,
    scan_output_stock,
)
from main import (
    BUSINESS_REGISTRY,
    CATEGORY_BRIEFS,
    SAMPLE_BRAND_RULES,
    generate_content_for_business,
    resolve_businesses,
    resolve_categories,
    run_check,
)
from webhook_service import resolve_webhook_url, send_draft_webhook

# run_demo.py実行時のデモ用ダミーWebhookエンドポイント（httpbin.orgのechoサービス）。
# 環境変数/.envに SNS_POST_WEBHOOK_URL が明示的に設定されていればそちらを優先し、
# 未設定時のみこのURLへ送信して、Webhook送信処理の挙動を目視確認できるようにする。
DEMO_WEBHOOK_URL: str = "https://httpbin.org/post"


def _webhook_url_for_demo() -> str:
    """デモ実行で使うWebhook送信先を決定する。

    `SNS_POST_WEBHOOK_URL`（.env優先）が設定されていればそれを、
    未設定なら `DEMO_WEBHOOK_URL`（httpbin.org）を返す。
    """
    return resolve_webhook_url() or DEMO_WEBHOOK_URL

# 事業・カテゴリごとに切り替えるモックドラフトの中身。
# 実際のAI生成は一切行わないが、各バリエーションで内容が固定の使い回しに
# ならないよう、事業とカテゴリのテーマに沿ったプレースホルダーを個別に用意する。
_MOCK_DRAFTS: dict[Business, dict[ContentCategory, dict[str, object]]] = {
    Business.UNAGI: {
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
    },
    Business.MAIZURU_BBQ: {
        ContentCategory.MENU_PROMOTION: {
            "plan": (
                "手ぶらで来られる気軽さと、国産和牛の香ばしい炭火焼きを両立して訴求する。"
                "器材・後片付け不要という手間のかからなさを軸にする。"
            ),
            "instagram_caption": (
                "着替えだけ持ってくればOK。器材も炭も食材も、全部こちらでご用意します。"
                "ジュワッと脂が弾ける国産和牛を、舞鶴公園の芝生の上で。"
                "後片付けも私たちにお任せください。"
            ),
            "instagram_hashtags": ["#舞鶴公園BBQ", "#手ぶらBBQ", "#福岡BBQ", "#和牛BBQ", "#福岡グルメ"],
            "line_message": (
                "🍖手ぶらでOK！国産和牛のプレミアムBBQセット、器材・食材・炭すべて込みです。"
                "着替えだけ持ってきてください。"
            ),
            "image_prompt_en": (
                "Premium wagyu beef sizzling on a charcoal grill in a sunny park, smoke rising, "
                "lush green lawn background, casual outdoor BBQ setup, bright midday sunlight, "
                "appetizing texture detail"
            ),
            "x_post": (
                "着替えだけでOK。器材も和牛も炭も、全部こちらで用意します。"
                "舞鶴公園で手ぶらBBQ、始めませんか。🍖 #舞鶴公園BBQ"
            ),
            "threads_post": (
                "「BBQしたいけど、道具を揃えるのが面倒」——そんな声から生まれたのが、"
                "この手ぶらプランです。\n\n"
                "国産和牛と厳選野菜、炭・器材・エプロンまで全部こちらで用意するので、"
                "着替えだけ持ってくればOK。舞鶴公園の芝生広場で、ジュワッと脂が弾ける"
                "和牛を炭火でじっくり焼き上げます。\n\n"
                "後片付けもスタッフにお任せください。帰りも手ぶらです。\n\n"
                "今度の週末、予定空いてますか？"
            ),
        },
        ContentCategory.GROUP_DINING: {
            "plan": (
                "幹事が場所取り・買い出し・後片付けに悩まなくていい安心感を訴求する。"
                "20名以上の団体対応、雨天時の代替対応にも触れる。"
            ),
            "instagram_caption": (
                "会社の懇親会、今年は外で。20名からの団体プランなら、幹事さんの仕事は"
                "「集合場所を伝えるだけ」。場所取りも買い出しも後片付けも、全部おまかせください。"
                "雨の日は屋根付きスペースへの振替もご案内します。"
            ),
            "instagram_hashtags": ["#舞鶴公園BBQ", "#会社の懇親会", "#チームビルディング", "#福岡団体BBQ", "#手ぶらBBQ"],
            "line_message": (
                "🏢会社の懇親会に。20名〜対応の団体BBQプラン、幹事さんの負担はゼロです。"
                "雨天時の対応もご案内できます。"
            ),
            "image_prompt_en": (
                "A large group of coworkers enjoying BBQ together in a park, multiple grills, "
                "laughter and toasting, team building atmosphere, bright summer afternoon light, "
                "candid documentary photography style"
            ),
            "x_post": (
                "会社の懇親会、今年は外で。20名からの団体プランなら幹事さんの仕事は"
                "集合場所を伝えるだけ。舞鶴公園BBQ🏢"
            ),
            "threads_post": (
                "幹事という役目、実は準備が一番大変だったりしますよね。\n\n"
                "舞鶴公園BBQの団体プランなら、20名からの会社の懇親会・チームビルディングにも"
                "対応。場所取り・買い出し・後片付け、全部おまかせください。幹事さんの仕事は"
                "「集合場所を伝えるだけ」になります。\n\n"
                "雨の日は屋根付きスペースへの振替もご案内できるので、天候の心配も"
                "最小限です。\n\n"
                "次の懇親会、外でやってみませんか？"
            ),
        },
        ContentCategory.COURSE_INTRODUCTION: {
            "plan": "スタンダード・プレミアム和牛・デザート付きファミリーの3プランを一覧的に見せる。",
            "instagram_caption": (
                "舞鶴公園BBQのプランは3種類。スタンダードプランは気軽な仲間内に、"
                "プレミアム和牛プランは特別な日に、デザート付きファミリープランは"
                "お子様連れに。どのプランも器材・食材・炭込みの完全手ぶらです。"
            ),
            "instagram_hashtags": ["#舞鶴公園BBQ", "#BBQプラン", "#福岡レジャー", "#手ぶらBBQ", "#福岡団体BBQ"],
            "line_message": (
                "📋プランは3種類。スタンダード／プレミアム和牛／デザート付きファミリー、"
                "シーンに合わせてお選びください。"
            ),
            "image_prompt_en": (
                "Three BBQ plan tiers displayed as an overhead flat lay — standard set, "
                "premium wagyu set, family dessert set — arranged on a picnic table in a park, "
                "editorial food photography, natural daylight"
            ),
            "x_post": (
                "スタンダード・プレミアム和牛・デザート付きファミリー。舞鶴公園BBQは"
                "3プランからシーンに合わせて選べます。"
            ),
            "threads_post": (
                "「どのプランを選べばいいですか？」というお声をよくいただくので、"
                "まとめてご紹介します。\n\n"
                "気軽な仲間内には「スタンダードプラン」。特別な日には国産和牛を堪能する"
                "「プレミアム和牛プラン」。お子様連れには「デザート付きファミリープラン」。\n\n"
                "どのプランも炭・器材・エプロンまで込みの完全手ぶらです。\n\n"
                "シーンを教えていただければ、ぴったりのプランをご提案します。"
            ),
        },
        ContentCategory.LOCAL_AREA_GUIDE: {
            "plan": (
                "舞鶴公園の芝生広場の開放感、福岡城跡・大濠公園との位置関係、"
                "アクセスの良さを紹介する。店の紹介は最後に。"
            ),
            "instagram_caption": (
                "舞鶴公園の芝生広場は、福岡城跡のすぐそば。石垣を眺めながらの散策のあとは、"
                "大濠公園までひと続きの緑道が続きます。地下鉄駅からも歩いてすぐ。"
                "そんな公園の一角で、私たちは手ぶらBBQを開いています。"
            ),
            "instagram_hashtags": ["#舞鶴公園", "#福岡城跡", "#大濠公園", "#福岡散歩", "#舞鶴公園BBQ"],
            "line_message": (
                "🌳福岡城跡・大濠公園に隣接する舞鶴公園。散策のあとは芝生広場で"
                "手ぶらBBQはいかがですか。"
            ),
            "image_prompt_en": (
                "A wide sunny lawn in Maizuru Park with Fukuoka Castle ruins stone walls "
                "visible in the distance, families walking, BBQ tents set up under shade, "
                "golden afternoon light, travel photography style"
            ),
            "x_post": (
                "福岡城跡のすぐそば、大濠公園まで続く緑道。地下鉄駅から歩いてすぐの"
                "舞鶴公園で、手ぶらBBQを開いています。"
            ),
            "threads_post": (
                "舞鶴公園の魅力を、少しだけ紹介させてください。\n\n"
                "福岡城跡のすぐそばに広がる芝生広場。石垣を眺めながらの散策のあとは、"
                "大濠公園までひと続きの緑道が続きます。地下鉄駅からも歩いてすぐという、"
                "アクセスの良さも魅力です。\n\n"
                "私たちは、そんな公園の一角で手ぶらBBQを開いています。散策の合間に、"
                "炭火の香りに立ち寄ってみませんか。\n\n"
                "お気に入りの公園、教えてください。"
            ),
        },
    },
    Business.ODO_BBQ: {
        ContentCategory.MENU_PROMOTION: {
            "plan": "海を望むロケーションと、新鮮な魚介・博多和牛の両方が楽しめる贅沢さを訴求する。",
            "instagram_caption": (
                "潮風を感じながら、新鮮な魚介と博多和牛を炭火で。小戸公園のシーサイドBBQセットは、"
                "海を見ながら食べる贅沢な時間そのもの。ヨットハーバーを眺める特等席で、"
                "乾杯しませんか。"
            ),
            "instagram_hashtags": ["#小戸BBQ", "#海鮮BBQ", "#博多和牛", "#福岡グルメ", "#手ぶらBBQ"],
            "line_message": (
                "🌊海を見ながら新鮮な魚介と博多和牛を。小戸BBQのシーサイドセット、"
                "手ぶらでお楽しみいただけます。"
            ),
            "image_prompt_en": (
                "Fresh seafood and wagyu beef grilling over charcoal with a marina and yachts "
                "in the background, ocean breeze atmosphere, bright coastal sunlight, "
                "appetizing glistening texture, lifestyle food photography"
            ),
            "x_post": (
                "潮風を感じながら、新鮮な魚介と博多和牛を炭火で。ヨットハーバーを望む"
                "特等席、小戸BBQで乾杯しませんか。🌊"
            ),
            "threads_post": (
                "海を見ながら食べるBBQって、想像以上に贅沢な時間なんです。\n\n"
                "小戸BBQのシーサイドセットは、新鮮な魚介と博多和牛を炭火でじっくり"
                "焼き上げるプラン。ヨットハーバーを望む特等席で、潮風を感じながら"
                "いただきます。\n\n"
                "器材も食材も炭も全部込みの手ぶらプランなので、着替えだけ持ってくれば"
                "OKです。\n\n"
                "海を見ながらの乾杯、してみませんか？"
            ),
        },
        ContentCategory.GROUP_DINING: {
            "plan": (
                "サークルの打ち上げや友人グループの利用を想定し、サンセットタイムの"
                "特別感・盛り上がりやすさを訴求する。"
            ),
            "instagram_caption": (
                "夕方から始まるサンセットBBQは、サークルの打ち上げにぴったり。"
                "海が茜色に染まる時間、みんなで乾杯する瞬間はここでしか味わえません。"
                "小戸公園の海風の中で、最高の一日を締めくくりませんか。"
            ),
            "instagram_hashtags": ["#小戸BBQ", "#福岡サンセット", "#サークル打ち上げ", "#福岡団体BBQ", "#手ぶらBBQ"],
            "line_message": (
                "🌇サンセットタイムのグループBBQ、サークルの打ち上げに人気です。"
                "海が染まる時間帯は特に予約が埋まりやすいので、お早めに。"
            ),
            "image_prompt_en": (
                "A group of friends toasting drinks at a beachside BBQ during golden hour "
                "sunset, ocean and yachts in the background, warm orange sky, lively "
                "celebratory atmosphere, candid lifestyle photography"
            ),
            "x_post": (
                "海が茜色に染まる時間、みんなで乾杯。サークルの打ち上げに人気の"
                "小戸BBQサンセットプラン。🌇"
            ),
            "threads_post": (
                "サークルの打ち上げ、そろそろ違う場所を探していませんか。\n\n"
                "小戸BBQのサンセットプランは、夕方から始まるグループ向けBBQ。"
                "海が茜色に染まる時間帯にみんなで乾杯する瞬間は、ここでしか味わえません。\n\n"
                "海風に吹かれながらの開放的な盛り上がりは、屋内では出せない特別感が"
                "あります。\n\n"
                "サンセットタイムは特に予約が埋まりやすいので、早めのご予約が"
                "おすすめです。\n\n"
                "次の打ち上げ、海でどうですか？"
            ),
        },
        ContentCategory.COURSE_INTRODUCTION: {
            "plan": "ランチ・サンセット・ファミリーの3プランを時間帯・シーンに応じて紹介する。",
            "instagram_caption": (
                "小戸BBQのプランは時間帯で選べます。日差しを浴びるランチプラン、"
                "海が染まるサンセットプラン、お子様連れに嬉しいファミリープラン。"
                "どのプランも海を見ながらの贅沢な時間です。"
            ),
            "instagram_hashtags": ["#小戸BBQ", "#BBQプラン", "#福岡レジャー", "#福岡サンセット", "#手ぶらBBQ"],
            "line_message": (
                "📋ランチ／サンセット／ファミリー、時間帯で選べる3プラン。"
                "海を見ながらのBBQをお楽しみください。"
            ),
            "image_prompt_en": (
                "Three BBQ plan tiers as an overhead flat lay — lunch set, sunset set, "
                "family set — arranged near the seaside with yachts visible, editorial "
                "food photography, natural coastal light"
            ),
            "x_post": (
                "ランチ・サンセット・ファミリー。小戸BBQは時間帯に合わせて選べる"
                "3プラン、どれも海を見ながら楽しめます。"
            ),
            "threads_post": (
                "小戸BBQのプランは、時間帯で選べます。\n\n"
                "日差しを浴びながら楽しむ「ランチプラン」。海が茜色に染まる"
                "「サンセットプラン」。お子様連れに嬉しい「ファミリープラン」。\n\n"
                "どのプランも、ヨットハーバーを望む海沿いの特等席でお楽しみいただけます。\n\n"
                "特に人気なのはサンセットプランですが、お子様連れならランチプランも"
                "過ごしやすくおすすめです。\n\n"
                "どの時間帯で海を楽しみたいですか？"
            ),
        },
        ContentCategory.LOCAL_AREA_GUIDE: {
            "plan": (
                "小戸公園の海沿いロケーション、ヨットハーバーの景観、"
                "駐車場完備のアクセスの良さを紹介する。"
            ),
            "instagram_caption": (
                "小戸公園はヨットハーバーを望む海沿いの公園。駐車場も完備しているので、"
                "車での来園もスムーズです。海風に吹かれながらの散歩のあとは、"
                "私たちのBBQスペースへ。夕暮れ時が特におすすめです。"
            ),
            "instagram_hashtags": ["#小戸公園", "#福岡海沿い", "#福岡ドライブ", "#小戸BBQ", "#福岡サンセット"],
            "line_message": (
                "⛵ヨットハーバーを望む小戸公園。駐車場完備でアクセスも良好です。"
                "夕暮れ時の散歩のあとにBBQはいかがですか。"
            ),
            "image_prompt_en": (
                "A coastal park path in Odo Park with a yacht harbor view, boats gently "
                "anchored, families strolling, spacious parking lot nearby, warm late "
                "afternoon light, travel photography style"
            ),
            "x_post": (
                "ヨットハーバーを望む小戸公園。駐車場完備でアクセスも良好、"
                "夕暮れ時の散歩のあとにBBQはいかがですか。⛵"
            ),
            "threads_post": (
                "小戸公園の魅力を、少しだけ紹介させてください。\n\n"
                "ヨットハーバーを望む海沿いの公園で、係留された船を眺めながらの散歩が"
                "人気です。駐車場も完備しているので、車での来園もスムーズ。\n\n"
                "海風に吹かれながらの散歩のあとは、私たちのBBQスペースへ。特に"
                "夕暮れ時は、空と海が茜色に染まる絶好のタイミングです。\n\n"
                "お気に入りの海沿いスポット、教えてください。"
            ),
        },
    },
}


def _build_mock_result(business: Business, category: ContentCategory) -> PipelineResult:
    """ANTHROPIC_API_KEY未設定時に、指定事業・カテゴリのパイプライン構造のみを
    再現するダミー結果を作る。

    実際のAI生成は一切行わない。Advisor-Executorのフロー
    （計画立案→計画レビュー→生成→品質レビュー→自律修正）と、
    format_result_as_markdown による出力フォーマットを検証するためだけの
    決定的なプレースホルダー値であり、事業・カテゴリごとにテーマへ即した内容を返す。
    """
    business_label = BUSINESS_LABELS[business]
    category_label = CATEGORY_LABELS[category]
    label = f"{business_label}/{category_label}"
    spec = _MOCK_DRAFTS[business][category]
    plan = f"[モック / {label}] {spec['plan']}"
    plan_review = AdvisorReview(
        verdict=AdvisorVerdict.APPROVED,
        feedback=f"[モック] {label}の切り口とブランドトーンとの整合に問題なし。承認。",
    )
    brand_rules = BUSINESS_REGISTRY[business].brand_rules
    optimized_hashtags = optimize_instagram_hashtags(
        category, brand_rules, spec["instagram_hashtags"]
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


def _generate_result(
    business: Business, category: ContentCategory, brief: RestaurantBrief
) -> tuple[PipelineResult, str]:
    """1事業・1カテゴリ分の `PipelineResult` を、live優先・未設定/失敗時はmockへ
    安全にフォールバックしつつ取得する。`_run_one` と `_run_simulate` で共有するロジック。

    Returns:
        tuple[PipelineResult, str]: 実行結果と、表示用の実行モード文字列。
    """
    api_key_present = bool(os.environ.get("ANTHROPIC_API_KEY"))
    label = f"{BUSINESS_LABELS[business]}/{CATEGORY_LABELS[category]}"

    if api_key_present:
        try:
            return generate_content_for_business(business, category), "live（実API呼び出し）"
        except Exception as exc:  # noqa: BLE001 - 実行失敗時はモックへ安全にフォールバックする
            print(
                f"[警告] {label}: 実API呼び出しに失敗したためモックモードにフォールバックします: {exc}",
                file=sys.stderr,
            )
            return (
                _build_mock_result(business, category),
                "mock（実API呼び出し失敗によるフォールバック）",
            )

    return _build_mock_result(business, category), "mock（ANTHROPIC_API_KEY未設定）"


def _run_one(
    business: Business, category: ContentCategory, brief: RestaurantBrief, post_date: date
) -> None:
    """1事業・1カテゴリ分のパイプライン（live優先・失敗/未設定時はmock）を実行し、
    画面出力・カレンダーフォルダへの自動保存・外部Webhookへの送信を行う。
    """
    label = f"{BUSINESS_LABELS[business]}/{CATEGORY_LABELS[category]}"
    result, mode = _generate_result(business, category, brief)

    print(f"<!-- {label} / 投稿予定日: {post_date.isoformat()} / 実行モード: {mode} -->\n")
    markdown = format_result_as_markdown(brief, result)
    print(markdown)

    saved_path = save_draft_to_calendar(markdown, category, post_date, business=business)
    print(f"\n[保存完了] {saved_path}", file=sys.stderr)

    webhook_result = send_draft_webhook(business, category, result, webhook_url=_webhook_url_for_demo())
    print(f"[Webhook] {webhook_result.message}", file=sys.stderr)


def _run_simulate(start_date: date, days: int) -> list[SimulationEntry]:
    """曜日別スケジュールルールに基づき、`start_date` から `days` 日分の大濠うなぎの
    ドラフトを（live優先・未設定/失敗時はmockで）一括生成し、カレンダーフォルダへ
    自動保存する（既存の事業固定シミュレーションのため、事業横断化の対象外）。

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
        result, _mode = _generate_result(Business.UNAGI, category, brief)
        markdown = format_result_as_markdown(brief, result)
        saved_path = save_draft_to_calendar(markdown, category, target_date, business=Business.UNAGI)
        entries.append(SimulationEntry(post_date=target_date, category=category, file_path=saved_path))
    return entries


def run_wizard() -> int:
    """コマンド引数を覚えなくても、画面の指示に従って番号選択・値入力するだけで
    全機能（生成・シミュレーション・一覧・エクスポート）を呼び出せる対話型ウィザード。

    `content_service.prompt_menu_choice` 等の共通入力ヘルパーを使い、標準の
    `input()` のみでメニュー選択→パラメータ入力→実行までを完結させる。
    各アクションの内部処理は、対応するCLIサブコマンド（通常生成・`simulate`・
    `list`・`export`）とまったく同じ関数（`_run_one` / `_run_simulate` 等）を
    呼び出す薄いフロントエンドである。

    Returns:
        int: 終了コード（正常終了は0）。
    """
    print("=" * 60)
    print("大濠うなぎ SNS自動運用エージェント：対話型ウィザード（デモ/モック環境）")
    print("=" * 60)

    menu_options = [
        ("generate", "投稿ドラフトを生成する（事業・カテゴリ・投稿予定日を選択）"),
        ("simulate", "曜日別自動シミュレーターを実行する（大濠うなぎ・期間指定）"),
        ("list", "保存済みストック一覧を表示する"),
        ("export", "ストックを1つのファイルへエクスポートする"),
        ("check", "ストックの自動バリデーション・検閲を実行する"),
        ("quit", "終了する"),
    ]
    action = prompt_menu_choice("何をしますか？", menu_options)

    if action == "quit":
        print("ウィザードを終了します。")
        return 0

    if action == "generate":
        business_options = [(b.value, BUSINESS_LABELS[b]) for b in Business] + [
            ("all", "全事業")
        ]
        business_slug = prompt_menu_choice("対象事業を選んでください", business_options)

        category_options = [(c.value, CATEGORY_LABELS[c]) for c in ContentCategory] + [
            ("all", "全カテゴリ")
        ]
        category_slug = prompt_menu_choice("生成するカテゴリを選んでください", category_options)

        post_date = prompt_optional_date("投稿予定日") or date.today()

        if not os.environ.get("ANTHROPIC_API_KEY"):
            print(
                "[情報] ANTHROPIC_API_KEY が未設定のため、モックモードで生成します。"
            )

        businesses = resolve_businesses(business_slug)
        requested_categories = set(resolve_categories(category_slug))

        first_block = True
        for business in businesses:
            profile = BUSINESS_REGISTRY[business]
            categories = [c for c in profile.category_briefs if c in requested_categories]
            if not categories:
                available = "、".join(CATEGORY_LABELS[c] for c in profile.category_briefs)
                print(
                    f"[スキップ] {BUSINESS_LABELS[business]} には指定カテゴリが定義されていません"
                    f"（対応カテゴリ: {available}）。"
                )
                continue

            for category in categories:
                if not first_block:
                    print("\n---\n")
                first_block = False
                _run_one(business, category, profile.category_briefs[category], post_date)
        return 0

    if action == "simulate":
        start_date = prompt_optional_date("シミュレーション開始日") or date.today()
        days = prompt_positive_int("生成する日数", 7)
        entries = _run_simulate(start_date, days)
        print(format_simulation_summary(entries))
        return 0

    if action == "list":
        month = prompt_optional_month("絞り込む年月")
        filter_date = prompt_optional_date("絞り込む投稿予定日")
        entries = scan_output_stock(month=month, target_date=filter_date)
        print(format_stock_summary(entries))
        return 0

    if action == "export":
        month = prompt_optional_month("結合対象を絞り込む年月")
        out_path = prompt_text_with_default("出力先ファイルパス", DEFAULT_EXPORT_PATH)
        saved_path = export_stocked_drafts(out_path, month=month)
        print(f"[エクスポート完了] {saved_path}")
        return 0

    # action == "check"
    month = prompt_optional_month("検閲対象を絞り込む年月")
    results = run_check(month=month)
    print(format_validation_summary(results))
    return 0


def main() -> int:
    """デモを実行し、指定カテゴリ（省略時は全7バリエーション）の生成結果を
    指定した投稿予定日（省略時は実行当日）でMarkdownとして標準出力へ表示すると同時に、
    カレンダーフォルダ（outputs/YYYY-MM/YYYY-MM-DD_カテゴリ名.md）へ自動保存する。
    先頭引数が "list" の場合は、生成を行わず `outputs/` のストック一覧を表示する。
    先頭引数が "simulate" の場合は、曜日別スケジュールルールに基づき指定期間分を
    （live優先・未設定/失敗時はmockで）一括生成・保存する。
    先頭引数が "export" の場合は、生成を行わず `outputs/` のドラフトを1ファイルへ結合出力する。
    先頭引数が "check" の場合は、生成を行わず `outputs/` のドラフトをBrandRulesの
    検閲ルール（ネガティブワード・文字数・ハッシュタグ数）で自動バリデーションする。
    先頭引数が "wizard" の場合は、コマンド引数の代わりに対話形式で全機能を呼び出せる
    ウィザードモードへ入る。

    実行方法:
        python3 run_demo.py list                    # 保存済みストックを全期間一覧表示
        python3 run_demo.py list --month 2026-07    # 2026年7月のストックのみ一覧表示
        python3 run_demo.py list --date 2026-07-12  # 投稿予定日で絞り込んで一覧表示
        python3 run_demo.py simulate --days 7       # 実行当日から1週間分を曜日別ルールで一括生成・保存
        python3 run_demo.py simulate --start-date 2026-08-01 --days 3  # 期間・開始日を指定
        python3 run_demo.py export                  # 全期間のドラフトを outputs/combined_export.md へ結合
        python3 run_demo.py export --month 2026-07 --out outputs/2026-07-まとめ.md  # 月・出力先を指定
        python3 run_demo.py check                   # 全期間のストックを検閲
        python3 run_demo.py check --month 2026-07   # 2026年7月のストックのみ検閲
        python3 run_demo.py wizard                  # 対話型ウィザードを起動
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

    if argv and argv[0] == "export":
        export_parser = build_export_arg_parser("run_demo.py")
        export_args = export_parser.parse_args(argv[1:])
        saved_path = export_stocked_drafts(export_args.out_path, month=export_args.month)
        print(f"[エクスポート完了] {saved_path}")
        return 0

    if argv and argv[0] == "check":
        check_parser = build_check_arg_parser("run_demo.py")
        check_args = check_parser.parse_args(argv[1:])
        results = run_check(month=check_args.month)
        print(format_validation_summary(results))
        return 0

    if argv and argv[0] == "wizard":
        return run_wizard()

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

    businesses = resolve_businesses(args.business)
    requested_categories = set(resolve_categories(args.category))

    first_block = True
    for business in businesses:
        profile = BUSINESS_REGISTRY[business]
        categories = [c for c in profile.category_briefs if c in requested_categories]
        if not categories:
            available = "、".join(CATEGORY_LABELS[c] for c in profile.category_briefs)
            print(
                f"[スキップ] {BUSINESS_LABELS[business]} には指定カテゴリが定義されていません"
                f"（対応カテゴリ: {available}）。",
                file=sys.stderr,
            )
            continue

        for category in categories:
            if not first_block:
                print("\n---\n")
            first_block = False
            _run_one(business, category, profile.category_briefs[category], post_date)
    return 0


if __name__ == "__main__":
    sys.exit(main())
