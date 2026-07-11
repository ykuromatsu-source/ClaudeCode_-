"""Advisor-Executor協調エージェントによる飲食店SNS運用自動化のエントリポイント。

既存スキル insta-food-buzz（~/.claude/skills/insta-food-buzz/、トリガー評価
正解率100%達成済み）が確立したペルソナ・品質スコアリング基準・CTAフォーマットを
読み取り専用で取り込み、Instagram投稿・LINE公式アカウント配信文・
X（旧Twitter・無料枠140字以内）投稿・Threads投稿（500字以内）・画像生成AI用
英語プロンプトの5種ドラフトを、Advisor（Fable 5想定）によるレビューを経て
Worker（Sonnet 5想定）が生成するパイプラインを提供する。

insta-food-buzzスキル本体のファイルは一切変更しない。

実行方法:
    python3 main.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from content_service import (
    BUSINESS_LABELS,
    CATEGORY_LABELS,
    Business,
    BrandRules,
    ContentCategory,
    DynamicContext,
    RestaurantBrief,
    SimulationEntry,
    DEFAULT_EXPORT_PATH,
    ValidationResult,
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
    generate_sns_drafts,
    get_category_for_weekday,
    prompt_menu_choice,
    prompt_optional_date,
    prompt_optional_month,
    prompt_positive_int,
    prompt_text_with_default,
    save_draft_to_calendar,
    scan_output_stock,
    validate_draft_text,
)
from agent_core import PipelineResult
from skill_knowledge import build_knowledge_digest, load_skill_knowledge
from webhook_service import send_draft_webhook

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
    mandatory_hashtags=["#大濠うなぎ", "#福岡グルメ", "#福岡うなぎ"],
    category_hashtags={
        ContentCategory.MENU_PROMOTION: ["#うなぎ好きと繋がりたい", "#釜まぶし"],
        ContentCategory.GROUP_DINING: ["#福岡宴会", "#福岡飲み会", "#すき焼きコース"],
        ContentCategory.TAKEOUT: ["#福岡テイクアウト", "#うなぎ弁当", "#おうちごはん"],
        ContentCategory.LUNCH: ["#福岡ランチ", "#大濠公園ランチ", "#ご褒美ランチ"],
        ContentCategory.DINNER: ["#福岡ディナー", "#福岡日本酒", "#大人の隠れ家"],
        ContentCategory.COURSE_INTRODUCTION: ["#福岡コース料理", "#接待コース", "#宴会コース"],
        ContentCategory.LOCAL_AREA_GUIDE: ["#大濠公園", "#舞鶴公園", "#福岡城跡", "#福岡観光"],
        ContentCategory.TRIVIA: ["#うなぎ豆知識", "#炭火焼き職人", "#食育"],
    },
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
    x_focus=(
        "一瞬で目を止める最重要ポイント1つだけに絞り込むこと。装飾語・接続詞・重複表現を"
        "極限まで削ぎ落とし、体言止めや短文の連続でテンポを作ること。看板メニュー名や"
        "「夏季限定」等の核心情報だけを残し、他の情報は思い切って削ること。"
    ),
    threads_focus=(
        "Instagramより肩の力を抜いた会話的なトーンで、一人称の語りかけや小さな"
        "自己開示を交えること。文末にフォロワーへの問いかけ（例:「〇〇派？△△派？」）を"
        "添え、自然にリプライを誘うこと。"
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
    category=ContentCategory.MENU_PROMOTION,
)

_BRAND_TONE = SAMPLE_BRAND_RULES.tone_and_manner

# 7つのコンテンツバリエーションそれぞれの店舗固有ブリーフ。
# `category_angle` にそのカテゴリならではの題材（大濠うなぎのメニュー・立地知識）を
# 記述し、「どう書くか」の指示（content_service.CATEGORY_FOCUS_INSTRUCTIONS）と
# 組み合わせてWorker/Advisorのプロンプトを構成する。
CATEGORY_BRIEFS: dict[ContentCategory, RestaurantBrief] = {
    ContentCategory.MENU_PROMOTION: SAMPLE_BRIEF,
    ContentCategory.GROUP_DINING: RestaurantBrief(
        store_name="大濠うなぎ",
        store_genre="うなぎ専門店",
        menu_name="鰻と和牛のすき焼きコース",
        menu_description="炭火焼きのうなぎと和牛を贅沢に使ったすき焼きコース。飲み放題付き。",
        season_or_event="通年（冬季は「せり鍋」、春季は「柳川鍋」も選べる）",
        brand_tone=_BRAND_TONE,
        category=ContentCategory.GROUP_DINING,
        category_angle=(
            "幹事目線での安心感を訴求する。飲み放題付きの宴会コースであること、"
            "冬は「せり鍋」・春は「柳川鍋」と季節替わりの選択肢があること、"
            "大人数でも個室感を保ちながら盛り上がれる店内構成であること。"
        ),
    ),
    ContentCategory.TAKEOUT: RestaurantBrief(
        store_name="大濠うなぎ",
        store_genre="うなぎ専門店",
        menu_name="謹製 うな重弁当",
        menu_description="炭火焼きのうなぎをそのまま持ち帰れる、贈答にも使える謹製重箱。",
        season_or_event="通年（慶事シーズンは「紅白うなぎ」の重箱仕立ても選べる）",
        brand_tone=_BRAND_TONE,
        category=ContentCategory.TAKEOUT,
        category_angle=(
            "自宅用・手土産・法事や慶事の持ち帰り需要にフォーカスする。"
            "焼き立てを崩さず持ち帰れる工夫、慶事向けの「紅白うなぎ」重箱、"
            "予約から受け取りまでのハードルの低さを自然に盛り込む。"
        ),
    ),
    ContentCategory.LUNCH: RestaurantBrief(
        store_name="大濠うなぎ",
        store_genre="うなぎ専門店",
        menu_name="日替わりうな重ランチ",
        menu_description="釜炊きご飯に炭火焼きのうなぎを乗せた、平日限定のランチ仕立て。",
        season_or_event="平日ランチタイム限定",
        brand_tone=_BRAND_TONE,
        category=ContentCategory.LUNCH,
        category_angle=(
            "「少し贅沢な日常使い」を体現する自分へのご褒美ランチとして描く。"
            "夜より入りやすい価格帯・提供の速さ・お一人様でも気兼ねなく入れる雰囲気を"
            "自然に匂わせる。"
        ),
    ),
    ContentCategory.DINNER: RestaurantBrief(
        store_name="大濠うなぎ",
        store_genre="うなぎ専門店",
        menu_name="白焼きとう巻き、日本酒とともに",
        menu_description="炭火で仕上げる白焼きとふわとろのう巻き。日本酒・焼酎と好相性。",
        season_or_event="通年（夜のみ提供）",
        brand_tone=_BRAND_TONE,
        category=ContentCategory.DINNER,
        category_angle=(
            "お酒に合う伝統料理（白焼き・う巻き等）とのペアリングを軸に、"
            "落ち着いた店内でゆったり夜を過ごす大人の時間を演出する。"
        ),
    ),
    ContentCategory.COURSE_INTRODUCTION: RestaurantBrief(
        store_name="大濠うなぎ",
        store_genre="うなぎ専門店",
        season_or_event="通年（季節替わりコースあり）",
        brand_tone=_BRAND_TONE,
        category=ContentCategory.COURSE_INTRODUCTION,
        category_angle=(
            "「鰻と和牛のすき焼きコース」「冬のせり鍋コース」「春の柳川鍋コース」"
            "「慶事向け紅白うなぎコース」など、シーンに応じて選べるコースの"
            "ラインナップ全体を紹介する。1つのコースを深掘りするのではなく、"
            "選択肢の豊富さそのものを見せる。"
        ),
    ),
    ContentCategory.LOCAL_AREA_GUIDE: RestaurantBrief(
        store_name="大濠うなぎ",
        store_genre="うなぎ専門店",
        brand_tone=_BRAND_TONE,
        category=ContentCategory.LOCAL_AREA_GUIDE,
        category_angle=(
            "大濠公園の四季折々の水辺の景観、隣接する舞鶴公園・福岡城跡の歴史散策路、"
            "散歩やジョギングに人気のお堀周辺の魅力を、地域ガイドとして紹介する。"
            "大濠うなぎはそのお堀からほど近い立地にある、という接続を最後にさりげなく添える。"
        ),
    ),
    ContentCategory.TRIVIA: RestaurantBrief(
        store_name="大濠うなぎ",
        store_genre="うなぎ専門店",
        brand_tone=_BRAND_TONE,
        category=ContentCategory.TRIVIA,
        category_angle=(
            "うなぎの旬や栄養にまつわる豆知識、「串打ち三年、裂き八年、焼き一生」と"
            "言われる炭火焼き職人の技術、皮はパリッと身はふっくらに仕上げる火加減の"
            "秘密、釜炊きご飯へのこだわりなど、雑学として読ませる読物コンテンツ。"
        ),
    ),
}
"""7つのコンテンツバリエーションそれぞれの店舗固有ブリーフ一覧（表示順を兼ねる）。"""


# ============================================================================
# 新規事業: 舞鶴公園BBQ / 小戸BBQ事業
# ============================================================================
# BBQ事業は天候・季節感が訴求力に直結するため、BrandRulesに加えて
# DynamicContext（季節・天候・ロケーション）を各事業ごとに用意し、
# generate_content_for_business() で自動的にプロンプトへ注入する。

MAIZURU_BBQ_BRAND_RULES = BrandRules(
    tone_and_manner=(
        "開放感があり、みんなでワイワイ楽しめるフレンドリーなトーン。"
        "「手ぶらで来られる気軽さ」を前面に出し、堅苦しさは一切出さない。"
    ),
    ng_words=["絶対", "日本一", "格安", "激安", "最強"],
    instagram_char_range=(150, 300),
    line_char_range=(100, 180),
    mandatory_hashtags=["#舞鶴公園BBQ", "#福岡BBQ", "#手ぶらBBQ"],
    category_hashtags={
        ContentCategory.MENU_PROMOTION: ["#和牛BBQ", "#福岡グルメ"],
        ContentCategory.GROUP_DINING: ["#会社の懇親会", "#チームビルディング", "#福岡団体BBQ"],
        ContentCategory.COURSE_INTRODUCTION: ["#BBQプラン", "#福岡レジャー"],
        ContentCategory.LOCAL_AREA_GUIDE: ["#舞鶴公園", "#福岡城跡", "#大濠公園"],
    },
    brand_concept=(
        "「手ぶらで来て、最高の一日を」。舞鶴公園の芝生広場を舞台にした手ぶらBBQ。"
        "器材・食材・炭・エプロンまですべてこちらで準備するので、着替えだけ持って"
        "来ればOK。福岡城跡のすぐそば、大濠公園にも隣接する好立地で、予約制のため"
        "場所取りの心配もいらない。"
    ),
    signature_menu_points=[
        "看板プランは、国産和牛と厳選野菜を使った「プレミアム和牛BBQセット」。",
        "器材・食材・炭・エプロンまで一式込みの完全手ぶらプラン。",
        "後片付けもスタッフが対応するため、手ぶらで来て手ぶらで帰れる。",
    ],
    pr_strengths=[
        "舞鶴公園の緑あふれる芝生広場というロケーションの良さ。",
        "予約制で場所取り不要、天候に応じたタープ・日陰席の用意あり。",
        "福岡城跡・大濠公園に隣接し、BBQ前後の散策も楽しめる。",
    ],
    scene_appeals=[
        "会社の懇親会・チームビルディング: 20名〜の団体対応、幹事の手間ゼロ。",
        "友人グループでの週末レジャー。",
        "ファミリーでの休日BBQ: デザート付きファミリープランあり。",
    ],
    instagram_focus=(
        "炭火で焼き上がる肉汁・煙・音といった「BBQならではの高揚感」を具体的な"
        "言葉で描写すること。「手ぶらで来られる」という最大の強みを必ず盛り込むこと。"
        "会社仲間・友人・家族など、複数人で楽しむ様子が伝わる演出を心がけること。"
    ),
    line_focus=(
        "気軽で親しみやすい口調で呼びかけること。予約の空き状況や天候に応じた"
        "対応（雨天時のキャンセルポリシー等）を簡潔に案内すること。"
    ),
    image_prompt_focus=(
        "芝生広場での開放的なBBQシーン、炭火の煙、肉が焼ける音が伝わるような"
        "ジューシーな質感、晴天の空の下での明るい自然光を英語で具体的に描写すること。"
    ),
    x_focus=(
        "「手ぶらで来られる」「炭火の香ばしさ」など最重要ポイント1つに絞り込むこと。"
        "装飾語を削ぎ落とし、短文の連続で開放的なテンポを作ること。"
    ),
    threads_focus=(
        "友人に話しかけるような会話的なトーンで、週末の予定を提案するような"
        "投げかけ（例:「今週末、予定空いてる？」）で自然にリプライを誘うこと。"
    ),
)

MAIZURU_BBQ_CONTEXT = DynamicContext(
    season="夏本番、蒸し暑い福岡の7月。夏休みシーズンに入り、家族・グループでのレジャー需要が高まる時期。",
    weather="晴天が続く予報、日中の最高気温は33℃前後。日差しが強いため、タープ・日陰席の快適さが重要な訴求ポイントになる。",
    location_note="舞鶴公園の芝生広場、福岡城跡・大濠公園に隣接。地下鉄駅から徒歩圏内でアクセスも良好。",
)

MAIZURU_BBQ_BRIEFS: dict[ContentCategory, RestaurantBrief] = {
    ContentCategory.MENU_PROMOTION: RestaurantBrief(
        store_name="舞鶴公園BBQ",
        store_genre="手ぶらBBQ",
        menu_name="手ぶらプレミアム和牛BBQセット",
        menu_description=(
            "国産和牛と厳選野菜、炭・器材・エプロンまで全て込みの手ぶらBBQセット。"
            "着替えだけ持って来ればOK。"
        ),
        season_or_event="夏季（7月〜8月、要予約）",
        brand_tone=MAIZURU_BBQ_BRAND_RULES.tone_and_manner,
        category=ContentCategory.MENU_PROMOTION,
        category_angle=(
            "手ぶらで来られる気軽さと、国産和牛の香ばしい焼き上がりを両立して訴求する。"
            "器材の準備・後片付けが不要という手間のかからなさを前面に出す。"
        ),
    ),
    ContentCategory.GROUP_DINING: RestaurantBrief(
        store_name="舞鶴公園BBQ",
        store_genre="手ぶらBBQ",
        menu_name="団体・法人向け手ぶらBBQプラン",
        menu_description="会社の懇親会・チームビルディング向けの、幹事の手間ゼロを実現する団体プラン。",
        season_or_event="夏季（7月〜8月、20名〜の団体対応）",
        brand_tone=MAIZURU_BBQ_BRAND_RULES.tone_and_manner,
        category=ContentCategory.GROUP_DINING,
        category_angle=(
            "幹事が場所取り・買い出し・後片付けに悩まなくていい安心感を訴求する。"
            "20名以上の団体対応が可能であること、雨天時の代替対応があることにも触れる。"
        ),
    ),
    ContentCategory.COURSE_INTRODUCTION: RestaurantBrief(
        store_name="舞鶴公園BBQ",
        store_genre="手ぶらBBQ",
        season_or_event="夏季（7月〜8月）",
        brand_tone=MAIZURU_BBQ_BRAND_RULES.tone_and_manner,
        category=ContentCategory.COURSE_INTRODUCTION,
        category_angle=(
            "スタンダードプラン・プレミアム和牛プラン・デザート付きファミリープランなど、"
            "シーンに応じて選べるプランのラインナップ全体を紹介する。"
        ),
    ),
    ContentCategory.LOCAL_AREA_GUIDE: RestaurantBrief(
        store_name="舞鶴公園BBQ",
        store_genre="手ぶらBBQ",
        brand_tone=MAIZURU_BBQ_BRAND_RULES.tone_and_manner,
        category=ContentCategory.LOCAL_AREA_GUIDE,
        category_angle=(
            "舞鶴公園の芝生広場の開放感、隣接する福岡城跡・大濠公園との位置関係、"
            "地下鉄駅からのアクセスの良さを地域ガイドとして紹介する。"
        ),
    ),
}
"""舞鶴公園BBQの4カテゴリ分の店舗固有ブリーフ。"""

ODO_BBQ_BRAND_RULES = BrandRules(
    tone_and_manner="海風を感じるリゾート感のある、明るく開放的なトーン。過度な煽り文句は避ける。",
    ng_words=["絶対", "日本一", "格安", "激安", "最強"],
    instagram_char_range=(150, 300),
    line_char_range=(100, 180),
    mandatory_hashtags=["#小戸BBQ", "#福岡BBQ", "#手ぶらBBQ"],
    category_hashtags={
        ContentCategory.MENU_PROMOTION: ["#海鮮BBQ", "#博多和牛", "#福岡グルメ"],
        ContentCategory.GROUP_DINING: ["#福岡サンセット", "#サークル打ち上げ", "#福岡団体BBQ"],
        ContentCategory.COURSE_INTRODUCTION: ["#BBQプラン", "#福岡レジャー"],
        ContentCategory.LOCAL_AREA_GUIDE: ["#小戸公園", "#福岡海沿い", "#福岡ドライブ"],
    },
    brand_concept=(
        "「潮風の下で、乾杯を」。小戸公園の海沿いロケーションを活かしたビーチサイドBBQ。"
        "ヨットハーバーを望む景観と、夕方から夜にかけてのサンセットタイムの利用が"
        "特に人気。駐車場完備でアクセスも良好。"
    ),
    signature_menu_points=[
        "看板プランは、新鮮な魚介と博多和牛を組み合わせた「シーサイドBBQセット」。",
        "夕方〜夜に利用する「サンセットプラン」が最大の売り。",
        "手ぶらOKのファミリープランもあり、子ども向けメニューも用意。",
    ],
    pr_strengths=[
        "海を望むロケーションと、ヨットハーバーの景観。",
        "サンセットタイムの絶景（日没時間帯の予約が特に人気）。",
        "駐車場完備でアクセス良好、車での来場にも便利。",
    ],
    scene_appeals=[
        "デート・記念日: サンセットタイムのロマンチックな演出。",
        "サークル・部活の打ち上げ: 海沿いでの開放的な盛り上がり。",
        "ファミリーでの海遊び後のBBQ利用。",
    ],
    instagram_focus=(
        "海・夕陽・潮風といった「小戸ならではの景観」を具体的な言葉で描写すること。"
        "新鮮な魚介と和牛の両方が楽しめる贅沢さを強調すること。"
        "サンセットタイムの特別感（日没の色味・海面の反射等）を演出すること。"
    ),
    line_focus=(
        "明るくリゾート感のある口調で呼びかけること。サンセットタイムの予約が"
        "埋まりやすい旨を自然に伝え、早めの予約を促すこと。"
    ),
    image_prompt_focus=(
        "海・ヨットハーバー・夕陽を背景にしたBBQシーン、新鮮な魚介の照り、"
        "夕暮れ時の暖かい光の色味を英語で具体的に描写すること。"
    ),
    x_focus=(
        "「海を見ながら」「サンセット」など最も刺さる一点に絞り込むこと。"
        "装飾語を削ぎ落とし、リゾート感が一瞬で伝わる短文でまとめること。"
    ),
    threads_focus=(
        "友人に話しかけるような会話的なトーンで、サンセットタイムの魅力を語りかけ、"
        "「一緒に行きたい人いる？」のような問いかけで自然にリプライを誘うこと。"
    ),
)

ODO_BBQ_CONTEXT = DynamicContext(
    season="夏本番、蒸し暑い福岡の7月。海風が心地よく感じられ、夕方以降の利用が増える季節。",
    weather="晴天が続く予報、日中の最高気温は33℃前後。夕方以降は海風で過ごしやすくなり、サンセットタイムが特に人気。",
    location_note="小戸公園の海沿いロケーション、ヨットハーバーを望む景観。駐車場完備。",
)

ODO_BBQ_BRIEFS: dict[ContentCategory, RestaurantBrief] = {
    ContentCategory.MENU_PROMOTION: RestaurantBrief(
        store_name="小戸BBQ事業",
        store_genre="海沿いBBQ",
        menu_name="シーサイド海鮮×博多和牛BBQセット",
        menu_description="新鮮な魚介と博多和牛を、海を見ながら楽しめる手ぶらBBQセット。",
        season_or_event="夏季（7月〜8月、要予約）",
        brand_tone=ODO_BBQ_BRAND_RULES.tone_and_manner,
        category=ContentCategory.MENU_PROMOTION,
        category_angle="海を望むロケーションと、新鮮な魚介・和牛の両方が楽しめる贅沢さを訴求する。",
    ),
    ContentCategory.GROUP_DINING: RestaurantBrief(
        store_name="小戸BBQ事業",
        store_genre="海沿いBBQ",
        menu_name="サークル・グループ向けサンセットBBQプラン",
        menu_description="夕方から夜にかけて、海風とサンセットを楽しみながらのグループBBQプラン。",
        season_or_event="夏季（7月〜8月、要予約）",
        brand_tone=ODO_BBQ_BRAND_RULES.tone_and_manner,
        category=ContentCategory.GROUP_DINING,
        category_angle=(
            "サークルの打ち上げや友人グループの利用を想定し、サンセットタイムの"
            "特別感・盛り上がりやすさを訴求する。"
        ),
    ),
    ContentCategory.COURSE_INTRODUCTION: RestaurantBrief(
        store_name="小戸BBQ事業",
        store_genre="海沿いBBQ",
        season_or_event="夏季（7月〜8月）",
        brand_tone=ODO_BBQ_BRAND_RULES.tone_and_manner,
        category=ContentCategory.COURSE_INTRODUCTION,
        category_angle=(
            "ランチプラン・サンセットプラン・ファミリープランなど、時間帯・シーンに"
            "応じて選べるプランのラインナップを紹介する。"
        ),
    ),
    ContentCategory.LOCAL_AREA_GUIDE: RestaurantBrief(
        store_name="小戸BBQ事業",
        store_genre="海沿いBBQ",
        brand_tone=ODO_BBQ_BRAND_RULES.tone_and_manner,
        category=ContentCategory.LOCAL_AREA_GUIDE,
        category_angle=(
            "小戸公園の海沿いロケーション、ヨットハーバーの景観、駐車場完備の"
            "アクセスの良さを地域ガイドとして紹介する。"
        ),
    ),
}
"""小戸BBQ事業の4カテゴリ分の店舗固有ブリーフ。"""


@dataclass(frozen=True)
class BusinessProfile:
    """事業単位でひとまとめにした運用ルール・カテゴリ別ブリーフ・動的コンテキスト。

    `Business` の値ごとにこのプロファイルを1つ持ち、CLIの `--business` 指定は
    `BUSINESS_REGISTRY` を介してBrandRules・RestaurantBrief・DynamicContextを
    一括で差し替える仕組みとして機能する。新規事業を追加する際は、対応する
    BrandRules・RestaurantBrief一式を用意し、ここへ1エントリ追加するだけでよい。
    """

    brand_rules: BrandRules
    category_briefs: dict[ContentCategory, RestaurantBrief]
    dynamic_context: Optional[DynamicContext] = None


BUSINESS_REGISTRY: dict[Business, BusinessProfile] = {
    Business.UNAGI: BusinessProfile(
        brand_rules=SAMPLE_BRAND_RULES, category_briefs=CATEGORY_BRIEFS, dynamic_context=None
    ),
    Business.MAIZURU_BBQ: BusinessProfile(
        brand_rules=MAIZURU_BBQ_BRAND_RULES,
        category_briefs=MAIZURU_BBQ_BRIEFS,
        dynamic_context=MAIZURU_BBQ_CONTEXT,
    ),
    Business.ODO_BBQ: BusinessProfile(
        brand_rules=ODO_BBQ_BRAND_RULES,
        category_briefs=ODO_BBQ_BRIEFS,
        dynamic_context=ODO_BBQ_CONTEXT,
    ),
}
"""事業（`--business`）とBusinessProfileの対応表。全事業横断ループの唯一の参照元。"""


def generate_content_for_business(business: Business, category: ContentCategory) -> PipelineResult:
    """指定事業・カテゴリの組み合わせでSNS投稿ドラフト一式を生成する。

    `BUSINESS_REGISTRY` からその事業のBrandRules・DynamicContextを解決し、
    `generate_content` へ委譲する。事業ごとのブランド差し替え・動的コンテキスト
    注入を一箇所に集約する薄いラッパー。

    Args:
        business: 対象事業。
        category: 生成したいコンテンツカテゴリ。

    Returns:
        PipelineResult: Advisor-Executorパイプラインの実行結果。

    Raises:
        KeyError: `category` がその事業の `category_briefs` に定義されていない場合。
    """
    profile = BUSINESS_REGISTRY[business]
    brief = profile.category_briefs[category]
    return generate_content(brief, brand_rules=profile.brand_rules, dynamic_context=profile.dynamic_context)


def generate_content(
    brief: RestaurantBrief,
    brand_rules: BrandRules = SAMPLE_BRAND_RULES,
    dynamic_context: Optional[DynamicContext] = None,
) -> PipelineResult:
    """insta-food-buzzの知見を統合したパイプラインでSNS投稿ドラフト一式を生成する。

    Args:
        brief: 店舗・商品情報。
        brand_rules: 適用する事業別運用ルール（既定は大濠うなぎ、既存呼び出し元との
            後方互換性のため）。
        dynamic_context: 季節・天候・ロケーションなど動的な文脈情報（省略可）。
            BBQ事業のように天候・季節感が訴求に直結する事業で使う。

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
        brand_rules=brand_rules,
        dynamic_context=dynamic_context,
    )


def generate_content_as_markdown(brief: RestaurantBrief) -> str:
    """generate_content を実行し、報告用Markdown文字列を返す。"""
    result = generate_content(brief)
    return format_result_as_markdown(brief, result)


def generate_content_for_category(category: ContentCategory) -> PipelineResult:
    """7バリエーションのうち指定した1カテゴリのみパイプラインを実行する。

    Args:
        category: 生成したいコンテンツバリエーション。`CATEGORY_BRIEFS` に
            対応する店舗固有ブリーフが定義されている必要がある。

    Returns:
        PipelineResult: 指定カテゴリのAdvisor-Executorパイプライン実行結果。

    Raises:
        KeyError: category に対応するブリーフが CATEGORY_BRIEFS に無い場合。
        skill_knowledge.SkillKnowledgeError: 既存スキル資産が読み込めない場合。
        RuntimeError: ANTHROPIC_API_KEY が未設定の場合。
    """
    return generate_content(CATEGORY_BRIEFS[category])


def generate_all_categories() -> dict[ContentCategory, PipelineResult]:
    """7バリエーションすべてを一括でシミュレーション生成する。

    Returns:
        dict[ContentCategory, PipelineResult]: `CATEGORY_BRIEFS` の定義順を
            保ったまま、各カテゴリの実行結果を格納した辞書。
    """
    return {
        category: generate_content(brief) for category, brief in CATEGORY_BRIEFS.items()
    }


def resolve_categories(category_slug: str) -> list[ContentCategory]:
    """argparseで検証済みのカテゴリ選択（"all" または ContentCategory.value）を解決する。

    `build_category_date_arg_parser` の `choices` により、ここに渡る値は
    既に "all" かいずれかの `ContentCategory.value` であることが保証されている。
    事業ごとに定義済みのカテゴリ数が異なる（例: BBQ事業は4カテゴリ）ため、
    "all" は特定事業の一覧ではなく `ContentCategory` 全種を返し、実際の生成時に
    各事業の `category_briefs` に存在するものだけへ絞り込む。
    """
    if category_slug == "all":
        return list(ContentCategory)
    return [ContentCategory(category_slug)]


def resolve_businesses(business_slug: str) -> list[Business]:
    """argparseで検証済みの事業選択（"all" または `Business.value`）を解決する。

    `build_category_date_arg_parser` の `choices` により、ここに渡る値は
    既に "all" かいずれかの `Business.value` であることが保証されている。
    """
    if business_slug == "all":
        return list(Business)
    return [Business(business_slug)]


def run_simulate(start_date: date, days: int) -> list[SimulationEntry]:
    """曜日別スケジュールルールに基づき、`start_date` から `days` 日分のドラフトを
    実APIで一括生成し、カレンダーフォルダへ自動保存する。

    Args:
        start_date: シミュレーションを開始する投稿予定日。
        days: 生成する期間の日数（1以上）。

    Returns:
        list[SimulationEntry]: 各日の投稿予定日・割り当てカテゴリ・保存先パス。

    Raises:
        skill_knowledge.SkillKnowledgeError: 既存スキル資産が読み込めない場合。
        RuntimeError: ANTHROPIC_API_KEY が未設定の場合。
    """
    entries: list[SimulationEntry] = []
    for offset in range(days):
        target_date = start_date + timedelta(days=offset)
        category = get_category_for_weekday(target_date)
        brief = CATEGORY_BRIEFS[category]
        markdown = generate_content_as_markdown(brief)
        saved_path = save_draft_to_calendar(markdown, category, target_date)
        entries.append(SimulationEntry(post_date=target_date, category=category, file_path=saved_path))
    return entries


def run_check(month: Optional[str] = None) -> list[ValidationResult]:
    """`outputs/` にストックされた既存ドラフトを、各事業のBrandRulesに基づく
    検閲ルール（ネガティブワード・文字数・ハッシュタグ数）で自動バリデーションする。

    実際のAI呼び出しは一切行わず、既に保存済みのMarkdownファイルをテキストとして
    検査するだけなので、APIキーの有無に関わらず常にスタンドアロンで完結する。

    Args:
        month: 指定した場合、その年月（"YYYY-MM"）のドラフトのみを検閲対象にする。
            省略時は `outputs/` 全期間が対象。

    Returns:
        list[ValidationResult]: 投稿予定日昇順に並んだ各ドラフトの検閲結果。
    """
    entries = scan_output_stock(month=month)
    results: list[ValidationResult] = []
    for entry in entries:
        business = entry.business or Business.UNAGI
        brand_rules = BUSINESS_REGISTRY[business].brand_rules
        with open(entry.file_path, "r", encoding="utf-8") as f:
            markdown_text = f.read()
        issues = validate_draft_text(markdown_text, brand_rules)
        results.append(
            ValidationResult(
                file_path=entry.file_path,
                post_date=entry.post_date,
                business=entry.business,
                category=entry.category,
                issues=issues,
            )
        )
    return results


def run_wizard() -> int:
    """コマンド引数を覚えなくても、画面の指示に従って番号選択・値入力するだけで
    全機能（生成・シミュレーション・一覧・エクスポート）を呼び出せる対話型ウィザード。

    `content_service.prompt_menu_choice` 等の共通入力ヘルパーを使い、標準の
    `input()` のみでメニュー選択→パラメータ入力→実行までを完結させる。
    各アクションの内部処理は、対応するCLIサブコマンド（通常生成・`simulate`・
    `list`・`export`）とまったく同じ関数を呼び出す薄いフロントエンドである。

    Returns:
        int: 終了コード（正常終了は0、エラー時は1）。
    """
    print("=" * 60)
    print("大濠うなぎ SNS自動運用エージェント：対話型ウィザード")
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

        try:
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

                    brief = profile.category_briefs[category]
                    print(
                        f"<!-- 事業: {BUSINESS_LABELS[business]} / カテゴリ: {CATEGORY_LABELS[category]} "
                        f"/ 投稿予定日: {post_date.isoformat()} -->\n"
                    )
                    result = generate_content_for_business(business, category)
                    markdown = format_result_as_markdown(brief, result)
                    print(markdown)
                    saved_path = save_draft_to_calendar(markdown, category, post_date, business=business)
                    print(f"\n[保存完了] {saved_path}")
        except Exception as exc:  # noqa: BLE001 - ウィザードの1操作として全例外を捕捉し終了コードに変換する
            print(f"エラー: {exc}")
            return 1
        return 0

    if action == "simulate":
        start_date = prompt_optional_date("シミュレーション開始日") or date.today()
        days = prompt_positive_int("生成する日数", 7)
        try:
            entries = run_simulate(start_date, days)
        except Exception as exc:  # noqa: BLE001 - ウィザードの1操作として全例外を捕捉し終了コードに変換する
            print(f"エラー: {exc}")
            return 1
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
    """CLI引数で指定した事業・カテゴリ（省略時は大濠うなぎの全バリエーション）を実行し、
    Markdownを標準出力へ表示すると同時に、指定した投稿予定日（省略時は実行当日）の
    カレンダーフォルダ（`outputs/YYYY-MM-DD/事業_カテゴリ.md`）へ自動保存する。
    先頭引数が "list" の場合は、生成を行わず `outputs/` のストック一覧を表示する。
    先頭引数が "simulate" の場合は、曜日別スケジュールルールに基づき大濠うなぎの
    指定期間分を一括生成・保存する（既存の事業固定シミュレーションのため、
    事業横断化の対象外）。
    先頭引数が "export" の場合は、生成を行わず `outputs/` のドラフトを1ファイルへ結合出力する。
    先頭引数が "check" の場合は、生成を行わず `outputs/` のドラフトをBrandRulesの
    検閲ルール（ネガティブワード・文字数・ハッシュタグ数）で自動バリデーションする。
    先頭引数が "wizard" の場合は、コマンド引数の代わりに対話形式で全機能を呼び出せる
    ウィザードモードへ入る。

    実行方法:
        python3 main.py                                  # 大濠うなぎの全バリエーションを実行当日の日付で一括生成・保存
        python3 main.py all --date 2026-07-15            # 大濠うなぎの全バリエーションを2026-07-15付けで保存
        python3 main.py takeout --date 2026-07-20          # 大濠うなぎのテイクアウトのみ保存
        python3 main.py all --business maizuru_bbq        # 舞鶴公園BBQの全カテゴリを一括生成・保存
        python3 main.py menu_promotion --business odo_bbq  # 小戸BBQ事業のメニュー訴求のみ生成・保存
        python3 main.py all --business all --date 2026-07-25  # 全3事業 × 対応カテゴリを一括生成・保存
        python3 main.py list                              # 保存済みストックを全期間一覧表示
        python3 main.py list --month 2026-07              # 2026年7月のストックのみ一覧表示
        python3 main.py list --date 2026-07-20             # 投稿予定日で絞り込んで一覧表示
        python3 main.py wizard                            # 対話型ウィザードを起動
        python3 main.py simulate --days 7                  # 大濠うなぎを実行当日から1週間分、曜日別ルールで一括生成・保存
        python3 main.py export                            # 全期間のドラフトを outputs/combined_export.md へ結合
        python3 main.py export --month 2026-07 --out outputs/2026-07-まとめ.md  # 月・出力先を指定
        python3 main.py check                             # 全期間のストックを検閲
        python3 main.py check --month 2026-07              # 2026年7月のストックのみ検閲
    """
    argv = sys.argv[1:]

    if argv and argv[0] == "list":
        list_parser = build_list_arg_parser("main.py")
        list_args = list_parser.parse_args(argv[1:])
        entries = scan_output_stock(month=list_args.month, target_date=list_args.filter_date)
        print(format_stock_summary(entries))
        return 0

    if argv and argv[0] == "simulate":
        simulate_parser = build_simulate_arg_parser("main.py")
        simulate_args = simulate_parser.parse_args(argv[1:])
        start_date: date = simulate_args.start_date or date.today()
        try:
            entries = run_simulate(start_date, simulate_args.days)
        except Exception as exc:  # noqa: BLE001 - CLIエントリポイントとして全例外を捕捉し終了コードに変換する
            print(f"エラー: {exc}", file=sys.stderr)
            return 1
        print(format_simulation_summary(entries))
        return 0

    if argv and argv[0] == "export":
        export_parser = build_export_arg_parser("main.py")
        export_args = export_parser.parse_args(argv[1:])
        saved_path = export_stocked_drafts(export_args.out_path, month=export_args.month)
        print(f"[エクスポート完了] {saved_path}")
        return 0

    if argv and argv[0] == "check":
        check_parser = build_check_arg_parser("main.py")
        check_args = check_parser.parse_args(argv[1:])
        results = run_check(month=check_args.month)
        print(format_validation_summary(results))
        return 0

    if argv and argv[0] == "wizard":
        return run_wizard()

    parser = build_category_date_arg_parser("main.py")
    args = parser.parse_args(argv)
    post_date: date = args.post_date or date.today()

    try:
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

                brief = profile.category_briefs[category]
                print(
                    f"<!-- 事業: {BUSINESS_LABELS[business]} / カテゴリ: {CATEGORY_LABELS[category]} "
                    f"/ 投稿予定日: {post_date.isoformat()} -->\n"
                )
                result = generate_content_for_business(business, category)
                markdown = format_result_as_markdown(brief, result)
                print(markdown)
                saved_path = save_draft_to_calendar(markdown, category, post_date, business=business)
                print(f"\n[保存完了] {saved_path}", file=sys.stderr)

                webhook_result = send_draft_webhook(business, category, result)
                print(f"[Webhook] {webhook_result.message}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - CLIエントリポイントとして全例外を捕捉し終了コードに変換する
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
