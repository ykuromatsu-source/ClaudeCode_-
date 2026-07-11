"""飲食店向けSNS投稿ドラフト生成サービス。

Instagramキャプション＋ハッシュタグ、LINE公式アカウント配信文、
X（旧Twitter・無料枠140字以内）投稿、Threads投稿（500字以内）、
画像生成AI（Midjourney/DALL-E想定）用の英語プロンプトを一括生成する。
実際のモデル呼び出しは agent_core.AdvisorExecutorAgent に委譲する。
"""

from __future__ import annotations

import argparse
import functools
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Optional

import anthropic

from agent_core import (
    WORKER_MODEL,
    AdvisorExecutorAgent,
    Advisor,
    PipelineResult,
    Worker,
)


class ContentCategory(Enum):
    """生成するSNSコンテンツの切り口（バリエーション）。

    「うなぎ文化をもっと身近に」というコンセプトを、単一メニューのPRに
    留めず多角的に体現するための7分類。値は英語スラグで、CLI引数や
    辞書キーとしてそのまま使える。
    """

    MENU_PROMOTION = "menu_promotion"
    """通常のメニューPR（単一メニューの魅力を五感描写中心に訴求）。"""

    GROUP_DINING = "group_dining"
    """団体向け（宴会・夜の会食）。すき焼きコースや季節の鍋、飲み放題にフォーカス。"""

    TAKEOUT = "takeout"
    """テイクアウト。「謹製 うな重弁当」やイベントごとの持ち帰り需要にフォーカス。"""

    LUNCH = "lunch"
    """ランチ用。少し贅沢な日常使い、自分へのご褒美ランチにフォーカス。"""

    DINNER = "dinner"
    """ディナー用。お酒に合わせる伝統料理（白焼き・う巻き等）とゆったりした夜。"""

    COURSE_INTRODUCTION = "course_introduction"
    """コース紹介。季節限定コースや豊富な宴会プランの魅力を網羅的に紹介。"""

    LOCAL_AREA_GUIDE = "local_area_guide"
    """地域密着・周辺紹介。大濠公園・舞鶴公園・福岡城跡などの歴史・自然・観光情報。"""

    TRIVIA = "trivia"
    """鰻の豆知識。うなぎ文化・歴史・炭火焼きの技術などを伝える読物コンテンツ。"""


CATEGORY_LABELS: dict[ContentCategory, str] = {
    ContentCategory.MENU_PROMOTION: "メニュー訴求",
    ContentCategory.GROUP_DINING: "団体向け（宴会・夜の会食）",
    ContentCategory.TAKEOUT: "テイクアウト",
    ContentCategory.LUNCH: "ランチ用",
    ContentCategory.DINNER: "ディナー用",
    ContentCategory.COURSE_INTRODUCTION: "コース紹介",
    ContentCategory.LOCAL_AREA_GUIDE: "地域密着・周辺紹介",
    ContentCategory.TRIVIA: "鰻の豆知識（読物）",
}
"""各カテゴリのMarkdown見出し・CLI表示用の日本語ラベル。"""

CATEGORY_FOCUS_INSTRUCTIONS: dict[ContentCategory, str] = {
    ContentCategory.MENU_PROMOTION: (
        "指定された単一メニューの魅力を、五感描写を中心に訴求する通常のメニューPR投稿。"
    ),
    ContentCategory.GROUP_DINING: (
        "宴会・夜の会食での利用を想定し、幹事や参加者が「これなら任せて安心」と思える"
        "コース料理・飲み放題・大人数対応の安心感を伝える。個人利用ではなく団体利用の"
        "メリット（貸切感・盛り上がり・コスパ）を軸にする。"
    ),
    ContentCategory.TAKEOUT: (
        "持ち帰り需要（自宅用・手土産・イベント用）にフォーカスし、出来立てを持ち帰る"
        "利便性と特別感を両立させて伝える。予約方法や受け取りやすさなど、行動への"
        "ハードルの低さも自然に盛り込む。"
    ),
    ContentCategory.LUNCH: (
        "「少し贅沢な日常使い」を体現する、自分へのご褒美ランチとしての気軽さと"
        "満足感を両立させて伝える。夜より手が届きやすい価格帯・時間の使いやすさを"
        "自然に匂わせる。"
    ),
    ContentCategory.DINNER: (
        "ゆったり楽しむ夜の食事として、お酒に合う伝統料理（白焼き・う巻き等）との"
        "ペアリングや、落ち着いた店内での大人の時間を演出する。"
    ),
    ContentCategory.COURSE_INTRODUCTION: (
        "季節限定コースや宴会プランのラインナップを網羅的に紹介し、シーンに応じて"
        "選べる豊富さそのものを魅力として伝える。単品メニューの深掘りではなく、"
        "選択肢の全体像を見せることを優先する。"
    ),
    ContentCategory.LOCAL_AREA_GUIDE: (
        "大濠公園・舞鶴公園・福岡城跡など周辺エリアの歴史・自然・観光情報を、"
        "通常の飲食店アカウントでは扱わない「読んでも楽しい地域情報」として発信する。"
        "情報の主役は地域そのものであり、店舗の紹介は最後にさりげなく添える程度に"
        "留める。"
    ),
    ContentCategory.TRIVIA: (
        "うなぎの文化・歴史、職人による炭火焼きの技術（皮はパリッと・身はふっくら）の"
        "背景、釜炊きご飯へのこだわりなど、知的好奇心を満たす読物コンテンツとして"
        "発信する。宣伝色を抑え、雑学として読ませてから自然に店舗へつなげる。"
    ),
}
"""カテゴリごとの執筆方針。店舗を問わない汎用の切り口指示（店舗固有の実質情報は
BrandRules・RestaurantBrief側が担い、ここでは「どんな角度で書くか」のみを扱う）。"""


class Business(Enum):
    """このパイプラインが対応する事業単位。

    「大濠うなぎ」（既存）に加え、新規事業として「舞鶴公園BBQ」「小戸BBQ事業」を
    追加した3事業構成。CLIの `--business` 引数・出力ファイル名の両方で
    この値（`.value`）をそのまま識別子として使う。
    """

    UNAGI = "unagi"
    """大濠うなぎ（既存事業）。"""

    MAIZURU_BBQ = "maizuru_bbq"
    """舞鶴公園BBQ（新規事業）。舞鶴公園内でのロケーションBBQ。"""

    ODO_BBQ = "odo_bbq"
    """小戸BBQ事業（新規事業）。小戸公園・海沿いロケーションでのBBQ。"""


BUSINESS_LABELS: dict[Business, str] = {
    Business.UNAGI: "大濠うなぎ",
    Business.MAIZURU_BBQ: "舞鶴公園BBQ",
    Business.ODO_BBQ: "小戸BBQ事業",
}
"""各事業のMarkdown見出し・CLI表示用の日本語ラベル。"""

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
            "x_post": {
                "type": "string",
                "description": (
                    "X（旧Twitter・無料枠）投稿本文。日本語で140文字以内を1文字でも"
                    "超えてはならない厳格な制約（絵文字・記号・ハッシュタグを含めた"
                    "全文字数）。Instagramのような多面的な描写は行わず、五感描写は"
                    "最も刺さる一点だけに絞り込むこと。接続詞・美辞麗句・重複表現を"
                    "徹底的に削ぎ落とし、体言止めや短文の連続で一瞬にして目を止める"
                    "テンポを作ること。ハッシュタグを付ける場合も0〜2個に絞り、"
                    "140字の枠内に収めること。"
                ),
            },
            "threads_post": {
                "type": "string",
                "description": (
                    "Threads投稿本文。日本語で500文字以内。Instagramキャプションより"
                    "会話的でカジュアルなトーンを取り、一人称の語りかけや小さな"
                    "自己開示を交えること。文末にフォロワーへの問いかけなど、"
                    "リプライ・いいね・保存を自然に誘う一言を添えること。"
                    "ハッシュタグは0〜3個程度、本文の邪魔にならない範囲で末尾に"
                    "添えてよい。"
                ),
            },
        },
        "required": [
            "instagram_caption",
            "instagram_hashtags",
            "line_message",
            "image_prompt_en",
            "x_post",
            "threads_post",
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

    x_char_limit: int = 140
    """X（旧Twitter・無料枠）投稿の文字数上限。日本語も1文字としてカウントし、
    絵文字・ハッシュタグを含めて1字でも超えてはならない。"""

    threads_char_range: tuple[int, int] = (200, 500)
    """Threads投稿の文字数目安（下限, 上限）。"""

    mandatory_hashtags: list[str] = field(default_factory=list)
    """他のハッシュタグに加えて必ず含めるハッシュタグ（例: 店舗公式タグ）。"""

    category_hashtags: dict[ContentCategory, list[str]] = field(default_factory=dict)
    """カテゴリ別に自動付与するハッシュタグのプリセット。`optimize_instagram_hashtags`
    が `mandatory_hashtags`（全カテゴリ共通）の次にこのプリセットを差し込み、
    最後にWorkerが生成したハッシュタグを補完的に加える。"""

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

    x_focus: str = ""
    """X（旧Twitter）固有の見せ方の指示（一瞬で惹きつける凝縮表現・装飾を削ぎ落とす等）。"""

    threads_focus: str = ""
    """Threads固有の見せ方の指示（会話的なトーン・共感やリプライを誘う投げかけ等）。"""

    negative_words: list[str] = field(
        default_factory=lambda: ["まずい", "高い", "最悪", "遅い"]
    )
    """ストック済みドラフトの自動検閲（`validate_draft_text`）で検出するネガティブ
    ワード。`ng_words`（誇張表現などブランドトーン上のNG語）とは目的が異なり、
    こちらは低評価・クレームを想起させる語がドラフト本文に紛れ込んでいないかを
    チェックするための語彙。"""

    def to_guideline_text(self) -> str:
        """Advisorのレビュー基準・Workerの生成プロンプトに埋め込むテキストへ変換する。"""
        ng = "、".join(self.ng_words) if self.ng_words else "指定なし"
        mandatory = " ".join(self.mandatory_hashtags) if self.mandatory_hashtags else "指定なし"
        ig_lo, ig_hi = self.instagram_char_range
        line_lo, line_hi = self.line_char_range
        threads_lo, threads_hi = self.threads_char_range

        lines = [
            f"・トーン＆マナー: {self.tone_and_manner}",
            f"・NGワード（本文・ハッシュタグとも使用禁止）: {ng}",
            f"・Instagramキャプション文字数目安: {ig_lo}〜{ig_hi}字（絵文字・改行含む）",
            f"・LINEメッセージ文字数目安: {line_lo}〜{line_hi}字",
            f"・X（旧Twitter・無料枠）文字数上限: {self.x_char_limit}字以内厳守"
            "（1字でも超過不可。絵文字・ハッシュタグ込みの全文字数）",
            f"・Threads文字数目安: {threads_lo}〜{threads_hi}字",
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

        if self.x_focus:
            lines.append(f"・X（旧Twitter）固有の見せ方: {self.x_focus}")

        if self.threads_focus:
            lines.append(f"・Threads固有の見せ方: {self.threads_focus}")

        return "\n".join(lines) + "\n"


@dataclass
class RestaurantBrief:
    """投稿生成のインプットとなる店舗・商品情報。

    `category` により7種のコンテンツバリエーションのうちどれを生成するかを指定する。
    「地域密着・周辺紹介」「鰻の豆知識」のように特定メニューを主役にしない
    カテゴリでは `menu_name`/`menu_description` を空のままにし、代わりに
    `category_angle` にそのカテゴリ固有の題材（周辺スポット名、豆知識のテーマ等）を
    記述する。
    """

    store_name: str
    store_genre: str
    menu_name: str = ""
    menu_description: str = ""
    season_or_event: str = ""
    brand_tone: str = ""
    category: ContentCategory = ContentCategory.MENU_PROMOTION
    """生成する7バリエーションのうちどれか。"""

    category_angle: str = ""
    """そのカテゴリならではの切り口・題材（例: 地域紹介なら周辺スポットの具体名、
    豆知識ならテーマとなる歴史・技術の要点）。空の場合は brief の他の項目のみで組み立てる。"""

    def to_prompt_text(self) -> str:
        """Workerに渡す自然文形式に変換する。"""
        lines = [
            f"コンテンツカテゴリ: {CATEGORY_LABELS[self.category]}",
            f"店舗名: {self.store_name}",
            f"ジャンル: {self.store_genre}",
        ]
        if self.menu_name:
            lines.append(f"訴求メニュー: {self.menu_name}")
        if self.menu_description:
            lines.append(f"メニュー説明: {self.menu_description}")
        if self.season_or_event:
            lines.append(f"季節/イベント: {self.season_or_event}")
        if self.brand_tone:
            lines.append(f"ブランドトーン: {self.brand_tone}")
        if self.category_angle:
            lines.append(f"このカテゴリならではの切り口・題材: {self.category_angle}")
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class DynamicContext:
    """投稿生成時点の季節・天候・ロケーションなど、日々変化する動的な文脈情報。

    BBQ事業のように天候・季節感が訴求力に直結する事業で特に重要となるが、
    どの事業のWorker/Advisorプロンプトにも任意で注入できる汎用の仕組みとして
    設計する（`generate_sns_drafts` の `dynamic_context` 引数経由）。
    値を持たないフィールドは `to_prompt_text` の出力から自動的に除外される。
    """

    season: str = ""
    """季節感の説明（例: "夏本番、蒸し暑い福岡の7月"）。"""

    weather: str = ""
    """当日〜直近の天候・気温の説明（例: "晴天予報、最高気温33℃、絶好のBBQ日和"）。"""

    location_note: str = ""
    """立地・ロケーションに関する補足（例: "舞鶴公園の緑と大濠の水辺が隣接"）。"""

    @property
    def is_empty(self) -> bool:
        """いずれのフィールドも指定されていないかを返す。"""
        return not (self.season or self.weather or self.location_note)

    def to_prompt_text(self) -> str:
        """空でないフィールドのみ、Worker/Advisorへ渡す自然文へ変換する。"""
        lines = []
        if self.season:
            lines.append(f"・季節感: {self.season}")
        if self.weather:
            lines.append(f"・天候/気温: {self.weather}")
        if self.location_note:
            lines.append(f"・ロケーション: {self.location_note}")
        return "\n".join(lines)


def optimize_instagram_hashtags(
    category: ContentCategory,
    brand_rules: Optional[BrandRules],
    draft_hashtags: list[str],
    max_count: int = 20,
) -> list[str]:
    """Instagramハッシュタグを、店舗共通タグ→カテゴリ別プリセット→Worker生成分の
    優先順位で組み合わせ、重複を除去した最適な組み合わせへ自動選定する。

    共通タグ（`brand_rules.mandatory_hashtags`）だけでは団体向け・地域密着・豆知識
    のようなカテゴリ固有の検索性を取りこぼすため、`brand_rules.category_hashtags`
    のカテゴリ別プリセットを間に差し込むことで、カテゴリの特性に応じた発見されやすさを
    補強する。Worker（LINE/live生成 or モック）が独自に出したタグは、最後に補完的に
    残りの枠へ加える。

    Args:
        category: このドラフトが属するコンテンツカテゴリ。
        brand_rules: 店舗別運用ルール。`None` の場合はWorker生成分をそのまま返す。
        draft_hashtags: Worker（live生成 or モック）が出したハッシュタグ案。
        max_count: 最終的なハッシュタグ数の上限（Instagramの実用上の目安）。

    Returns:
        list[str]: 重複除去・優先順位付け・上限適用済みのハッシュタグ一覧。
    """
    if brand_rules is None:
        # 重複除去のみ行い、順序は維持する
        seen: set[str] = set()
        deduped = [tag for tag in draft_hashtags if not (tag in seen or seen.add(tag))]
        return deduped[:max_count]

    mandatory = brand_rules.mandatory_hashtags
    category_preset = brand_rules.category_hashtags.get(category, [])

    combined = [*mandatory, *category_preset, *draft_hashtags]
    seen = set()
    deduped = [tag for tag in combined if not (tag in seen or seen.add(tag))]
    return deduped[:max_count]


logger = logging.getLogger(__name__)

DEFAULT_RETRY_MAX_ATTEMPTS: int = 3
"""`retry_on_exception` の既定の最大リトライ回数（初回実行は含まない）。"""

DEFAULT_RETRY_INITIAL_DELAY_SECONDS: float = 2.0
"""`retry_on_exception` の1回目のリトライ前の既定待機秒数。"""

DEFAULT_RETRY_BACKOFF_MULTIPLIER: float = 2.0
"""`retry_on_exception` がリトライごとに待機時間へ掛け合わせる既定倍率
（2秒→4秒→8秒と指数関数的に増加する）。"""


def retry_on_exception(
    max_retries: int = DEFAULT_RETRY_MAX_ATTEMPTS,
    initial_delay: float = DEFAULT_RETRY_INITIAL_DELAY_SECONDS,
    backoff_multiplier: float = DEFAULT_RETRY_BACKOFF_MULTIPLIER,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
):
    """エクスポネンシャル・バックオフによる汎用リトライデコレータ。

    将来的なAnthropic API呼び出しや外部通信（Webhook送信等）で発生しうる
    一時的なエラー（ネットワーク瞬断・レートリミット等）に備え、初回実行を
    含めず最大 `max_retries` 回まで、待機時間を `initial_delay` から
    `backoff_multiplier` 倍ずつ指数関数的に増やしながら再試行する
    （既定: 2秒→4秒→8秒）。`time` / `functools` / `logging` という
    Python標準ライブラリのみで完結し、外部パッケージには一切依存しない。

    リトライが発生するたびに、エラー内容と次の待機秒数・試行回数を
    `logging.WARNING` レベルでログ出力する。最大リトライ回数を尽くしても
    成功しなかった場合は、最後に捕捉した例外をそのまま呼び出し元へ送出する
    （例外を握りつぶさない）。

    Args:
        max_retries: 初回実行を除く最大リトライ回数（既定3回）。
        initial_delay: 1回目のリトライ前の待機秒数（既定2秒）。
        backoff_multiplier: リトライごとに待機時間を掛け合わせる倍率（既定2倍）。
        exceptions: リトライ対象とする例外の型（既定は `Exception` 全般）。
            特定の通信エラーのみをリトライ対象にしたい場合はここへ絞り込める。

    Returns:
        デコレートされた関数を返すデコレータ本体。
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            delay = initial_delay
            last_exc: Optional[BaseException] = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt >= max_retries:
                        break
                    logger.warning(
                        "%s でエラーが発生しました（%s）。%.0f秒後にリトライします (試行 %d/%d)。",
                        func.__name__,
                        exc,
                        delay,
                        attempt + 1,
                        max_retries,
                    )
                    time.sleep(delay)
                    delay *= backoff_multiplier

            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator


@retry_on_exception()
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


# ============================================================================
# 縦型ショート動画（Instagramリール/TikTok/YouTubeショート）台本生成
# ============================================================================
# 通常のSNS投稿ドラフト（Instagram/LINE/X/Threads/画像プロンプト）とは独立した
# 生成スキル。既存のAdvisor-Executorパイプライン（agent_core.py）は使わず、
# Worker単体の1ショット生成（システムプロンプトで厳密な出力形式を強制）で
# 完結させることで、他の検閲・カテゴリロジックと競合しないよう疎結合に保つ。

VIDEO_SCRIPT_DURATION_SEGMENTS: dict[int, list[tuple[str, str]]] = {
    15: [("0〜2秒", "フック"), ("2〜10秒", "ボディ"), ("10〜15秒", "オファー/CTA")],
    30: [("0〜3秒", "フック"), ("3〜22秒", "ボディ"), ("22〜30秒", "オファー/CTA")],
    60: [("0〜5秒", "フック"), ("5〜45秒", "ボディ"), ("45〜60秒", "オファー/CTA")],
}
"""目標秒数（15/30/60）別の3段構成タイムスタンプ割り当て（フック→ボディ→オファー/CTA）。
未対応の秒数を指定した場合は30秒構成にフォールバックする（堅牢性優先）。"""


def _video_script_system_prompt(duration_seconds: int) -> str:
    """指定尺の3段構成タイムスタンプを埋め込んだ、動画台本生成用のシステムプロンプトを組み立てる。"""
    segments = VIDEO_SCRIPT_DURATION_SEGMENTS.get(
        duration_seconds, VIDEO_SCRIPT_DURATION_SEGMENTS[30]
    )
    segment_lines = "\n".join(f"  - {timestamp}【{label}】" for timestamp, label in segments)

    return (
        "あなたは、飲食店の縦型ショート動画（Instagramリール・TikTok・YouTubeショート）で"
        "数多くのバズを生み出してきたプロの動画ディレクター件構成作家として振る舞います。\n\n"
        f"目標尺は{duration_seconds}秒です。以下の3段構成のタイムスタンプに厳密に従い、"
        "冒頭で視聴者に離脱されない強烈なフックを作ることを最優先してください。\n"
        f"{segment_lines}\n\n"
        "出力は必ず日本語のMarkdownテーブル形式のみとし、以下の列（この順序・この列名）で"
        "3行（フック／ボディ／オファーCTA）を出力してください。\n"
        "| タイムスタンプ / シーン区分 | 映像内容（Visual） | ナレーション/音声（Audio） | 画面テロップ（Telop） |\n"
        "|---|---|---|---|\n\n"
        "テーブルの下に、以下2点を箇条書きで必ず添えてください。\n"
        "- 🎵 トレンド音楽の指定枠（ジャンル・BPM帯・雰囲気の候補を1〜2案）\n"
        "- 🎥 カメラワーク・カット割りの指示（視聴者を飽きさせないテンポの作り方。"
        "各カットは長くても2〜3秒で切り替わる想定で具体的に描写すること）\n\n"
        "説明文・前置き・後書きは一切付けず、Markdownテーブルと上記2点の箇条書きのみを"
        "出力してください。"
    )


@retry_on_exception()
def generate_video_script(
    theme: str,
    duration_seconds: int = 30,
    store_name: str = "",
    brand_rules: Optional[BrandRules] = None,
) -> str:
    """飲食店の縦型ショート動画（Instagramリール/TikTok/YouTubeショート）向けの
    3段構成（フック→ボディ→オファー/CTA）台本を、コピペでそのまま撮影・編集に
    使えるMarkdownテーブル形式で生成する。

    通常のSNS投稿ドラフト生成（`generate_sns_drafts`）とは別系統の軽量な
    1ショット生成であり、Advisor-Executorの計画レビュー等は行わない。
    実際の外部API呼び出しを伴うため、通信エラー・レートリミットに備えて
    `retry_on_exception`（エクスポネンシャル・バックオフ）を適用している。

    Args:
        theme: 動画のテーマ（例: "職人技のアピール"、"新メニューの紹介"、
            "店舗へのアクセス"）。
        duration_seconds: 目標尺（秒）。15/30/60を想定し、それ以外の値は
            30秒構成にフォールバックする。
        store_name: 店舗名（省略可）。指定した場合、生成プロンプトの文脈に含める。
        brand_rules: 店舗別運用ルール（省略可）。指定した場合、トーン＆マナーと
            ブランドコンセプトを生成プロンプトへ反映する。

    Returns:
        str: Markdownテーブル＋音楽/カメラワークの箇条書きを含む台本本文。

    Raises:
        RuntimeError: ANTHROPIC_API_KEY が未設定、または応答からテキストが
            取得できなかった場合（`retry_on_exception` の最大リトライ後）。
    """
    client = build_client()

    brand_context = ""
    if brand_rules is not None:
        brand_context = f"\n\n【ブランドトーン＆マナー】\n{brand_rules.tone_and_manner}\n"
        if brand_rules.brand_concept:
            brand_context += f"【ブランドコンセプト】\n{brand_rules.brand_concept}\n"

    user_message = (
        f"店舗名: {store_name or '（未指定）'}\n"
        f"動画のテーマ: {theme}\n"
        f"目標尺: {duration_seconds}秒"
        f"{brand_context}"
    )

    response = client.messages.create(
        model=WORKER_MODEL,
        max_tokens=1500,
        system=_video_script_system_prompt(duration_seconds),
        messages=[{"role": "user", "content": user_message}],
    )

    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text.strip()

    raise RuntimeError("動画台本の生成に失敗しました（テキスト応答が得られませんでした）。")


def generate_sns_drafts(
    brief: RestaurantBrief,
    brand_guideline: str,
    knowledge_digest: str = "",
    brand_rules: Optional[BrandRules] = None,
    dynamic_context: Optional[DynamicContext] = None,
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
        dynamic_context: 生成時点の季節・天候・ロケーションなど動的な文脈情報。
            指定した場合（かつ空でない場合）、Worker/Advisor双方のプロンプトへ
            埋め込まれる。BBQ事業のように天候・季節感が訴求に直結する事業で使う。

    Returns:
        PipelineResult: 計画・レビュー結果・最終ドラフトを含む実行結果。
    """
    client = build_client()
    worker = Worker(client)
    advisor = Advisor(client)
    agent = AdvisorExecutorAgent(worker=worker, advisor=advisor)

    category_focus = CATEGORY_FOCUS_INSTRUCTIONS[brief.category]
    worker_brief = (
        f"{brief.to_prompt_text()}\n【このカテゴリの執筆方針】\n{category_focus}\n"
    )
    reviewer_guideline = f"{brand_guideline}\n\n【このカテゴリの執筆方針】\n{category_focus}\n"

    if knowledge_digest:
        worker_brief = f"{knowledge_digest}\n\n【今回の店舗・商品情報】\n{worker_brief}"
        reviewer_guideline = f"{reviewer_guideline}\n\n{knowledge_digest}"

    if brand_rules is not None:
        rules_text = brand_rules.to_guideline_text()
        worker_brief = f"{worker_brief}\n\n【店舗別運用ルール（必ず遵守）】\n{rules_text}"
        reviewer_guideline = f"{reviewer_guideline}\n\n【店舗別運用ルール（必ず遵守）】\n{rules_text}"

    if dynamic_context is not None and not dynamic_context.is_empty:
        context_text = dynamic_context.to_prompt_text()
        worker_brief = (
            f"{worker_brief}\n\n【本日の動的コンテキスト（季節・天候・ロケーション）】\n{context_text}\n"
        )
        reviewer_guideline = (
            f"{reviewer_guideline}\n\n【本日の動的コンテキスト】\n{context_text}\n"
        )

    result = agent.run(
        brief=worker_brief,
        brand_guideline=reviewer_guideline,
        tool_schema=CONTENT_TOOL_SCHEMA,
    )

    optimized_hashtags = optimize_instagram_hashtags(
        brief.category, brand_rules, result.final_content.get("instagram_hashtags", [])
    )
    result.final_content["instagram_hashtags"] = optimized_hashtags
    result.draft["instagram_hashtags"] = optimized_hashtags

    return result


def format_result_as_markdown(brief: RestaurantBrief, result: PipelineResult) -> str:
    """パイプライン結果を報告用Markdownに整形する。"""
    content = result.final_content
    hashtags = " ".join(content.get("instagram_hashtags", []))
    category_label = CATEGORY_LABELS[brief.category]
    title_suffix = f" {brief.menu_name}" if brief.menu_name else ""
    lines = [
        f"# SNS投稿ドラフト: {brief.store_name}｜【{category_label}】{title_suffix}",
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
        "## X（旧Twitter・無料枠）",
        f"**文字数: {len(content.get('x_post', ''))}/140**",
        "",
        content.get("x_post", ""),
        "",
        "## Threads",
        f"**文字数: {len(content.get('threads_post', ''))}/500**",
        "",
        content.get("threads_post", ""),
        "",
        "## 画像生成AIプロンプト（英語）",
        "",
        f"`{content.get('image_prompt_en', '')}`",
        "",
    ]
    image_url = content.get("image_url", "")
    image_local_path = content.get("image_local_path", "")
    image_display = image_local_path or image_url
    if image_display:
        lines += [
            "## 生成画像（DALL-E 3）",
            "",
            image_display,
            "",
        ]
        if image_local_path and image_url and image_local_path != image_url:
            lines += [f"（元の生成URL、発行から短時間で失効: {image_url}）", ""]
    if result.quality_review is not None:
        lines += [
            "## 品質レビュー（Advisor / Fable 5）",
            f"- 判定: {result.quality_review.verdict.value}",
            f"- フィードバック: {result.quality_review.feedback}",
            "",
        ]
    return "\n".join(lines)


# `sns-auto-agent/` 直下（このモジュールと同じディレクトリ）を保存先ルートとする。
DEFAULT_OUTPUT_ROOT: str = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "outputs"
)


def save_draft_to_calendar(
    markdown_text: str,
    category: ContentCategory,
    post_date: date,
    business: Business = Business.UNAGI,
    output_root: str = DEFAULT_OUTPUT_ROOT,
) -> str:
    """生成済みMarkdownドラフトを投稿予定日・事業ごとにカレンダーフォルダへ自動保存する。

    保存先は `outputs/YYYY-MM-DD/事業スラグ_カテゴリ名.md` とし、`YYYY-MM-DD`
    フォルダは投稿予定日から自動判定し、無ければ `os.makedirs` で自動生成する。
    1日に複数事業・複数カテゴリのドラフトが並行して生成される運用を想定し、
    ファイル名に事業スラグを含めることで同日内の衝突を避ける。ファイル冒頭には
    生成日時・投稿予定日・事業名・カテゴリ名のメタデータをコメントブロックとして
    付与する。

    Args:
        markdown_text: `format_result_as_markdown` が組み立てた投稿ドラフト本文。
        category: このドラフトが属するコンテンツカテゴリ。
        post_date: 投稿予定日。
        business: このドラフトが属する事業（既定は大濠うなぎ、既存呼び出し元との
            後方互換性のため）。
        output_root: 保存先ルートディレクトリ（既定はこのモジュールと同じ
            ディレクトリ直下の `outputs/`）。

    Returns:
        str: 実際に書き込んだファイルの絶対パス。
    """
    day_dir = os.path.join(output_root, post_date.isoformat())
    os.makedirs(day_dir, exist_ok=True)

    file_path = os.path.join(day_dir, f"{business.value}_{category.value}.md")

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = (
        "<!--\n"
        f"生成日時: {generated_at}\n"
        f"投稿予定日: {post_date.isoformat()}\n"
        f"事業: {BUSINESS_LABELS[business]} ({business.value})\n"
        f"カテゴリ: {CATEGORY_LABELS[category]} ({category.value})\n"
        "-->\n\n"
    )

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(header + markdown_text.rstrip() + "\n")

    return file_path


def parse_date_arg(value: str) -> date:
    """CLIの `--date` オプション値をバリデーションしつつ `date` へ変換する。

    argparseの `type=` に渡すことで、不正な形式の入力に対して自動的に
    使い方メッセージを表示し安全に終了（exit code 2）させる。

    Raises:
        argparse.ArgumentTypeError: "YYYY-MM-DD" 形式でない場合。
    """
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"日付は YYYY-MM-DD 形式で指定してください（例: 2026-07-15）: '{value}'"
        ) from exc


def build_category_date_arg_parser(prog: str) -> argparse.ArgumentParser:
    """`main.py` / `run_demo.py` 共通のCLI引数パーサーを構築する。

    位置引数 `category`（省略時 "all"）でカテゴリを、`--business` で対象事業
    （省略時は大濠うなぎ。"all" で全事業一括）を、`--date` で投稿予定日
    （省略時は実行当日の日付）を指定できる。
    """
    parser = argparse.ArgumentParser(
        prog=prog,
        description="事業ごとのSNS投稿ドラフトをコンテンツカテゴリから選んで生成する。",
    )
    category_choices = ["all"] + [c.value for c in ContentCategory]
    parser.add_argument(
        "category",
        nargs="?",
        default="all",
        choices=category_choices,
        help="生成するコンテンツカテゴリ（省略時は all でそのカテゴリ全バリエーション一括生成）",
    )
    business_choices = ["all"] + [b.value for b in Business]
    parser.add_argument(
        "--business",
        dest="business",
        choices=business_choices,
        default=Business.UNAGI.value,
        help=(
            "対象事業（省略時は unagi=大濠うなぎ）。"
            "maizuru_bbq=舞鶴公園BBQ、odo_bbq=小戸BBQ事業、all=全事業一括生成"
        ),
    )
    parser.add_argument(
        "--date",
        dest="post_date",
        type=parse_date_arg,
        default=None,
        help="投稿予定日 YYYY-MM-DD（省略時は実行当日の日付）",
    )
    return parser


def parse_month_arg(value: str) -> str:
    """CLIの `--month` オプション値（`list` サブコマンド用）をバリデーションする。

    Raises:
        argparse.ArgumentTypeError: "YYYY-MM" 形式でない場合。
    """
    try:
        datetime.strptime(value, "%Y-%m")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"年月は YYYY-MM 形式で指定してください（例: 2026-07）: '{value}'"
        ) from exc
    return value


def build_list_arg_parser(prog: str) -> argparse.ArgumentParser:
    """`list` サブコマンド用のCLI引数パーサーを構築する（ストック一覧の絞り込み条件）。"""
    parser = argparse.ArgumentParser(
        prog=f"{prog} list",
        description="outputs/ に保存済みのSNS投稿ドラフトを一覧サマリー表示する。",
    )
    parser.add_argument(
        "--month",
        dest="month",
        type=parse_month_arg,
        default=None,
        help="表示する年月 YYYY-MM（省略時は全期間を表示）",
    )
    parser.add_argument(
        "--date",
        dest="filter_date",
        type=parse_date_arg,
        default=None,
        help="表示する投稿予定日 YYYY-MM-DD（省略時は絞り込みなし）",
    )
    return parser


@dataclass(frozen=True)
class StockEntry:
    """`outputs/` にストック済みの1ファイル分のメタデータ。"""

    file_path: str
    """保存されているMarkdownファイルの絶対パス。"""

    post_date: date
    """格納先フォルダ名（`YYYY-MM-DD`）から判定した投稿予定日。"""

    business: Optional[Business]
    """ファイル名から判定した事業（判定できない場合は None）。"""

    category: Optional[ContentCategory]
    """ファイル名から判定したコンテンツカテゴリ（判定できない場合は None）。"""

    generated_at: str
    """ファイル冒頭のメタデータコメントから読み取った生成日時（読み取れない場合は "不明"）。"""

    @property
    def business_label(self) -> str:
        """コンソール表示用の事業日本語ラベル。"""
        return BUSINESS_LABELS[self.business] if self.business is not None else "(不明な事業)"

    @property
    def category_label(self) -> str:
        """コンソール表示用のカテゴリ日本語ラベル。"""
        return CATEGORY_LABELS[self.category] if self.category is not None else "(不明なカテゴリ)"


_DAY_DIRNAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_METADATA_GENERATED_AT_RE = re.compile(r"生成日時:\s*(.+)")


def _split_business_category_filename(filename: str) -> tuple[Optional[Business], Optional[ContentCategory]]:
    """`<事業スラグ>_<カテゴリスラグ>.md` 形式のファイル名を事業・カテゴリへ分解する。

    事業スラグ・カテゴリスラグとも `_` を含みうるため文字列分割では一意に
    決まらない。既知の `Business` の値を先頭一致で試すことで確実に分解する。
    """
    if not filename.endswith(".md"):
        return None, None
    stem = filename[: -len(".md")]

    for business in Business:
        prefix = f"{business.value}_"
        if stem.startswith(prefix):
            category_slug = stem[len(prefix) :]
            try:
                return business, ContentCategory(category_slug)
            except ValueError:
                return business, None

    return None, None


def _parse_stock_file(file_path: str, post_date: date) -> Optional[StockEntry]:
    """1ファイルを格納先の投稿予定日・ファイル名規則・メタデータコメントからパースする。

    ファイル名が `事業スラグ_カテゴリ名.md` 規則に合致しない場合でも、事業や
    カテゴリが不明なエントリとして一覧には含める（堅牢性優先、静かに除外しない）。

    Args:
        file_path: パース対象のMarkdownファイルの絶対パス。
        post_date: このファイルが格納されている `YYYY-MM-DD` フォルダから
            判定済みの投稿予定日。
    """
    filename = os.path.basename(file_path)
    business, category = _split_business_category_filename(filename)

    generated_at = "不明"
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            # メタデータヘッダーはファイル冒頭のコメントブロックにあるため、先頭のみ読めば十分
            header = f.read(500)
        match = _METADATA_GENERATED_AT_RE.search(header)
        if match:
            generated_at = match.group(1).strip()
    except OSError:
        pass

    return StockEntry(
        file_path=file_path,
        post_date=post_date,
        business=business,
        category=category,
        generated_at=generated_at,
    )


def scan_output_stock(
    output_root: str = DEFAULT_OUTPUT_ROOT,
    month: Optional[str] = None,
    target_date: Optional[date] = None,
) -> list[StockEntry]:
    """`outputs/` 以下にストックされたMarkdownドラフトをスキャンし、メタデータ付きで一覧化する。

    保存レイアウトは `outputs/YYYY-MM-DD/事業スラグ_カテゴリ名.md` を前提とする
    （`save_draft_to_calendar` が書き込む構造と一致）。

    Args:
        output_root: スキャン対象のルートディレクトリ（既定は `DEFAULT_OUTPUT_ROOT`）。
        month: 指定した場合、その年月（"YYYY-MM"）で始まる日付フォルダのみをスキャンする。
        target_date: 指定した場合、その投稿予定日のフォルダのみに絞り込む。

    Returns:
        list[StockEntry]: 投稿予定日の昇順（同日内は事業名→カテゴリ名順）に並んだ一覧。
            `output_root` が存在しない場合は空リストを返す（堅牢性優先、エラーにしない）。
    """
    if not os.path.isdir(output_root):
        return []

    day_dirnames = sorted(
        name
        for name in os.listdir(output_root)
        if _DAY_DIRNAME_RE.match(name) and os.path.isdir(os.path.join(output_root, name))
    )

    if target_date is not None:
        day_dirnames = [name for name in day_dirnames if name == target_date.isoformat()]
    elif month:
        day_dirnames = [name for name in day_dirnames if name.startswith(month)]

    entries: list[StockEntry] = []
    for dirname in day_dirnames:
        try:
            post_date = datetime.strptime(dirname, "%Y-%m-%d").date()
        except ValueError:
            continue

        day_dir = os.path.join(output_root, dirname)
        for filename in sorted(os.listdir(day_dir)):
            if not filename.endswith(".md"):
                continue
            entry = _parse_stock_file(os.path.join(day_dir, filename), post_date)
            if entry is not None:
                entries.append(entry)

    entries.sort(key=lambda e: (e.post_date, e.business_label, e.category_label))
    return entries


def format_stock_summary(entries: list[StockEntry], output_root: str = DEFAULT_OUTPUT_ROOT) -> str:
    """`scan_output_stock` の結果を、月ごとに見出しを分けたscannableなコンソール表示へ整形する。

    Args:
        entries: `scan_output_stock` が返した一覧（投稿予定日昇順を前提とする）。
        output_root: ファイルパス表示を相対パス化する際の基準ディレクトリ。

    Returns:
        str: 標準出力にそのまま表示できる整形済みテキスト。
    """
    if not entries:
        return "保存済みのストックは見つかりませんでした。"

    lines: list[str] = []
    current_month: Optional[str] = None
    for entry in entries:
        month = entry.post_date.strftime("%Y-%m")
        if month != current_month:
            if current_month is not None:
                lines.append("")
            lines.append(f"# {month}")
            current_month = month

        rel_path = os.path.relpath(entry.file_path, start=output_root)
        lines.append(f"- 📅 {entry.post_date.isoformat()}｜【{entry.business_label}】{entry.category_label}")
        lines.append(f"    生成日時: {entry.generated_at}")
        lines.append(f"    ファイル: {rel_path}")

    lines.append("")
    lines.append(f"合計 {len(entries)} 件")
    return "\n".join(lines)


WEEKDAY_LABELS_JA: tuple[str, ...] = ("月", "火", "水", "木", "金", "土", "日")
"""`date.weekday()`（月曜=0〜日曜=6）に対応する日本語の曜日ラベル。"""

WEEKDAY_CATEGORY_SCHEDULE: dict[int, ContentCategory] = {
    0: ContentCategory.LUNCH,                  # 月: 週始めのご褒美ランチ
    1: ContentCategory.COURSE_INTRODUCTION,    # 火: 定番・コースラインナップ紹介
    2: ContentCategory.TAKEOUT,                # 水: 週半ばのテイクアウト需要
    3: ContentCategory.TRIVIA,                 # 木: うなぎの豆知識でエンタメ要素
    4: ContentCategory.GROUP_DINING,           # 金: 週末に向けた宴会・飲み放題訴求
    5: ContentCategory.DINNER,                 # 土: 週末のゆったりとしたディナー
    6: ContentCategory.LOCAL_AREA_GUIDE,       # 日: 休日のお出かけ・地域密着情報
}
"""大濠うなぎの運用に最適化した曜日別カテゴリ割り当てルール。
キーは `date.weekday()`（月曜=0〜日曜=6）。店舗の運用方針が変わった場合は
このマッピングを差し替えるだけで `simulate` コマンドの割り当てが変わる。"""


def get_category_for_weekday(target_date: date) -> ContentCategory:
    """指定日の曜日から、`WEEKDAY_CATEGORY_SCHEDULE` に基づく最適なコンテンツカテゴリを判定する。"""
    return WEEKDAY_CATEGORY_SCHEDULE[target_date.weekday()]


def parse_positive_int_arg(value: str) -> int:
    """CLIの `--days` オプション値をバリデーションしつつ正の整数へ変換する。

    Raises:
        argparse.ArgumentTypeError: 整数でない、または1以上でない場合。
    """
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"整数を指定してください: '{value}'") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"1以上の整数を指定してください: '{value}'")
    return parsed


def build_simulate_arg_parser(prog: str) -> argparse.ArgumentParser:
    """`simulate` サブコマンド用のCLI引数パーサーを構築する。"""
    parser = argparse.ArgumentParser(
        prog=f"{prog} simulate",
        description=(
            "曜日別スケジュールルールに基づき、指定期間分の投稿ドラフトを一括生成し、"
            "カレンダーフォルダへ自動保存する。"
        ),
    )
    parser.add_argument(
        "--start-date",
        dest="start_date",
        type=parse_date_arg,
        default=None,
        help="シミュレーションを開始する投稿予定日 YYYY-MM-DD（省略時は実行当日）",
    )
    parser.add_argument(
        "--days",
        dest="days",
        type=parse_positive_int_arg,
        default=7,
        help="生成する期間の日数（省略時 7、1以上の整数）",
    )
    return parser


@dataclass(frozen=True)
class SimulationEntry:
    """`simulate` コマンドで1日分生成・保存した結果。"""

    post_date: date
    """この投稿ドラフトの投稿予定日。"""

    category: ContentCategory
    """曜日別スケジュールルールから割り当てられたコンテンツカテゴリ。"""

    file_path: str
    """`save_draft_to_calendar` が書き込んだファイルの絶対パス。"""


def format_simulation_summary(
    entries: list[SimulationEntry], output_root: str = DEFAULT_OUTPUT_ROOT
) -> str:
    """`simulate` コマンドの実行結果を、日付・曜日・カテゴリ・保存先が一目でわかる
    コンソール表示用のサマリーへ整形する。

    Args:
        entries: 生成・保存済みの各日の結果（投稿予定日の昇順を前提とする）。
        output_root: ファイルパス表示を相対パス化する際の基準ディレクトリ。

    Returns:
        str: 標準出力にそのまま表示できる整形済みテキスト。
    """
    if not entries:
        return "生成対象がありませんでした。"

    start = entries[0].post_date
    end = entries[-1].post_date
    lines = [
        f"📅 投稿スケジュール自動シミュレーション結果"
        f"（{start.isoformat()} 〜 {end.isoformat()} / {len(entries)}日間）",
        "",
    ]
    for entry in entries:
        weekday_label = WEEKDAY_LABELS_JA[entry.post_date.weekday()]
        rel_path = os.path.relpath(entry.file_path, start=output_root)
        lines.append(
            f"- {entry.post_date.isoformat()}({weekday_label}) → 【{CATEGORY_LABELS[entry.category]}】"
        )
        lines.append(f"    保存先: {rel_path}")

    lines.append("")
    lines.append(f"合計 {len(entries)} 件のドラフトを生成・保存しました。")
    return "\n".join(lines)


DEFAULT_EXPORT_PATH: str = os.path.join(DEFAULT_OUTPUT_ROOT, "combined_export.md")
"""`export` コマンドのデフォルト出力先（`outputs/combined_export.md`）。"""


def build_export_arg_parser(prog: str) -> argparse.ArgumentParser:
    """`export` サブコマンド用のCLI引数パーサーを構築する。"""
    parser = argparse.ArgumentParser(
        prog=f"{prog} export",
        description="outputs/ に保存済みのSNS投稿ドラフトを1つのファイルへ結合してエクスポートする。",
    )
    parser.add_argument(
        "--month",
        dest="month",
        type=parse_month_arg,
        default=None,
        help="結合対象を絞り込む年月 YYYY-MM（省略時は全期間を結合）",
    )
    parser.add_argument(
        "--out",
        dest="out_path",
        default=DEFAULT_EXPORT_PATH,
        help=f"出力先ファイルパス（省略時 {DEFAULT_EXPORT_PATH}）",
    )
    return parser


def export_stocked_drafts(
    output_path: str = DEFAULT_EXPORT_PATH,
    month: Optional[str] = None,
    output_root: str = DEFAULT_OUTPUT_ROOT,
) -> str:
    """`outputs/` にストックされたMarkdownドラフトを1つのファイルへ結合してエクスポートする。

    週刊・月刊のまとめ資料として、日別フォルダにバラバラに保存された投稿ドラフトを
    投稿予定日の昇順で集約する。各ドラフトはファイル名由来の見出し（投稿予定日・
    事業名・カテゴリ名）と `---` の区切り線で区切られる。対象期間にドラフトが
    1件も無い場合でも、エラーにはせずその旨を明記したファイルを出力する
    （モック環境での完遂性を優先）。

    Args:
        output_path: 結合結果を書き込む出力先ファイルパス。親ディレクトリが
            無ければ自動生成する（既定は `outputs/combined_export.md`）。
        month: 指定した場合、その年月（"YYYY-MM"）のドラフトのみを対象にする。
            省略時は `outputs/` 全期間が対象。
        output_root: スキャン対象のルートディレクトリ（既定は `DEFAULT_OUTPUT_ROOT`）。

    Returns:
        str: 実際に書き込んだファイルの絶対パス。
    """
    entries = scan_output_stock(output_root=output_root, month=month)

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    scope_label = month if month else "全期間"

    lines = [
        f"# SNS投稿ドラフト 一括エクスポート（{scope_label}）",
        "",
        f"生成日時: {generated_at}",
        f"対象件数: {len(entries)} 件",
        "",
    ]

    if not entries:
        lines.append("対象期間内に保存済みのドラフトが見つかりませんでした。")
    else:
        for i, entry in enumerate(entries):
            if i > 0:
                lines.append("\n---\n")
            lines.append(
                f"## {entry.post_date.isoformat()}｜{entry.business_label}｜{entry.category_label}"
            )
            lines.append("")
            try:
                with open(entry.file_path, "r", encoding="utf-8") as f:
                    lines.append(f.read().rstrip())
            except OSError as exc:
                lines.append(f"（このファイルは読み込めませんでした: {exc}）")

    combined_text = "\n".join(lines) + "\n"

    output_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(output_dir, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(combined_text)

    return os.path.abspath(output_path)


# ============================================================================
# 対話型ウィザード（`wizard` サブコマンド）用の共通コンソール入力ヘルパー
# ============================================================================
# コマンド引数を覚えなくても、番号選択・値入力だけで全機能を呼び出せるように
# main.py / run_demo.py の run_wizard() から共通で利用される。外部ライブラリは
# 使わず、標準の input() のみで完結させる。

def prompt_menu_choice(title: str, options: list[tuple[str, str]]) -> str:
    """番号付きメニューを表示し、ユーザーが選んだ選択肢のキーを返す。

    Args:
        title: メニューの見出し。
        options: `(キー, 表示ラベル)` のタプルのリスト。番号は1始まりで自動採番する。

    Returns:
        str: 選択された選択肢のキー（`options` の1つ目の要素）。
    """
    print(f"\n{title}")
    for i, (_, label) in enumerate(options, start=1):
        print(f"  {i}. {label}")
    while True:
        raw = input(f"番号を入力してください (1-{len(options)}): ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        print(f"1〜{len(options)}の数字を入力してください。")


def prompt_optional_date(prompt_text: str) -> Optional[date]:
    """日付の入力を求める。空欄が入力された場合は `None` を返し、呼び出し側で
    既定値（実行当日等）に解決させる。不正な形式の場合は再入力を求める。
    """
    while True:
        raw = input(f"{prompt_text}（YYYY-MM-DD、空欄でスキップ）: ").strip()
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            print("日付は YYYY-MM-DD 形式で入力してください（例: 2026-07-15）。")


def prompt_optional_month(prompt_text: str) -> Optional[str]:
    """年月の入力を求める。空欄が入力された場合は `None` を返す（絞り込みなし）。
    不正な形式の場合は再入力を求める。
    """
    while True:
        raw = input(f"{prompt_text}（YYYY-MM、空欄でスキップ）: ").strip()
        if not raw:
            return None
        try:
            datetime.strptime(raw, "%Y-%m")
            return raw
        except ValueError:
            print("年月は YYYY-MM 形式で入力してください（例: 2026-07）。")


def prompt_positive_int(prompt_text: str, default: int) -> int:
    """正の整数の入力を求める。空欄が入力された場合は `default` を返す。
    整数でない、または1以上でない場合は再入力を求める。
    """
    while True:
        raw = input(f"{prompt_text}（既定 {default}、空欄で既定値）: ").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            print("整数を入力してください。")
            continue
        if value <= 0:
            print("1以上の整数を入力してください。")
            continue
        return value


def prompt_text_with_default(prompt_text: str, default: str) -> str:
    """テキストの入力を求める。空欄が入力された場合は `default` を返す。"""
    raw = input(f"{prompt_text}（既定: {default}、空欄で既定値）: ").strip()
    return raw or default


def prompt_required_text(prompt_text: str) -> str:
    """空欄を許さないテキスト入力を求める。空欄が入力された場合は再入力を促す。"""
    while True:
        raw = input(f"{prompt_text}: ").strip()
        if raw:
            return raw
        print("空欄では進められません。内容を入力してください。")


# ============================================================================
# ストック自動バリデーション（`check` サブコマンド）
# ============================================================================
# BrandRulesの検閲ルール（ネガティブワード・文字数・ハッシュタグ数）に基づき、
# outputs/ にストック済みのドラフトを事後的に自動チェックする。実際のAI呼び出しは
# 一切行わず、既に保存されたMarkdownファイルをテキストとして検査するだけなので、
# APIキーの有無に関わらず常にスタンドアロンで完結する。

INSTAGRAM_CAPTION_CHAR_LIMIT: int = 2200
"""Instagramキャプションの実用上の文字数上限。"""

INSTAGRAM_HASHTAG_COUNT_LIMIT: int = 30
"""Instagram投稿につけられるハッシュタグ数の実用上の上限。"""

_INSTAGRAM_CAPTION_SECTION_RE = re.compile(
    r"\*\*キャプション\*\*\n\n(.*?)\n\n\*\*ハッシュタグ\*\*", re.DOTALL
)
_INSTAGRAM_HASHTAGS_SECTION_RE = re.compile(
    r"\*\*ハッシュタグ\*\*\n\n(.*?)\n\n##", re.DOTALL
)
_HASHTAG_TOKEN_RE = re.compile(r"#\S+")


def _extract_instagram_caption_and_hashtags(markdown_text: str) -> tuple[str, list[str]]:
    """`format_result_as_markdown` が組み立てたMarkdownからInstagramセクションの
    キャプション本文とハッシュタグ一覧を抽出する。該当セクションが見つからない
    場合は空文字列・空リストを返す（堅牢性優先、例外を送出しない）。
    """
    caption_match = _INSTAGRAM_CAPTION_SECTION_RE.search(markdown_text)
    caption = caption_match.group(1).strip() if caption_match else ""

    hashtags_match = _INSTAGRAM_HASHTAGS_SECTION_RE.search(markdown_text)
    hashtags_line = hashtags_match.group(1).strip() if hashtags_match else ""
    hashtags = _HASHTAG_TOKEN_RE.findall(hashtags_line)

    return caption, hashtags


@dataclass(frozen=True)
class ValidationIssue:
    """検閲で検出された1件の違反内容。"""

    rule: str
    """違反したルールの分類名（例: "NGワード" / "文字数" / "ハッシュタグ数"）。"""

    message: str
    """人間が読んで内容がわかる詳細メッセージ。"""


def validate_draft_text(markdown_text: str, brand_rules: BrandRules) -> list[ValidationIssue]:
    """1件のドラフトMarkdownを、Instagramキャプションを対象に3項目で検閲する。

    - **NGワードチェック**: `brand_rules.negative_words` のいずれかが本文に
      完全一致で含まれていないか（大文字小文字・日本語とも厳格な文字列一致）。
    - **文字数チェック**: キャプション本文が `INSTAGRAM_CAPTION_CHAR_LIMIT` を
      超えていないか。
    - **ハッシュタグ数チェック**: ハッシュタグの総数が
      `INSTAGRAM_HASHTAG_COUNT_LIMIT` を超えていないか。

    Args:
        markdown_text: `format_result_as_markdown` が組み立てたMarkdown全文。
        brand_rules: 検閲基準（ネガティブワードリスト等）を提供する運用ルール。

    Returns:
        list[ValidationIssue]: 検出された違反のリスト。空リストならPass。
    """
    caption, hashtags = _extract_instagram_caption_and_hashtags(markdown_text)
    issues: list[ValidationIssue] = []

    for word in brand_rules.negative_words:
        if word in caption:
            issues.append(
                ValidationIssue(rule="NGワード", message=f"禁止ワード「{word}」が本文に含まれています。")
            )

    caption_length = len(caption)
    if caption_length > INSTAGRAM_CAPTION_CHAR_LIMIT:
        issues.append(
            ValidationIssue(
                rule="文字数",
                message=(
                    f"Instagramキャプションが{caption_length}字あり、"
                    f"上限{INSTAGRAM_CAPTION_CHAR_LIMIT}字を超えています。"
                ),
            )
        )

    hashtag_count = len(hashtags)
    if hashtag_count > INSTAGRAM_HASHTAG_COUNT_LIMIT:
        issues.append(
            ValidationIssue(
                rule="ハッシュタグ数",
                message=(
                    f"ハッシュタグが{hashtag_count}個あり、"
                    f"上限{INSTAGRAM_HASHTAG_COUNT_LIMIT}個を超えています。"
                ),
            )
        )

    return issues


@dataclass(frozen=True)
class ValidationResult:
    """1件のストック済みドラフトに対する検閲結果。"""

    file_path: str
    """検閲対象のMarkdownファイルの絶対パス。"""

    post_date: date
    """投稿予定日。"""

    business: Optional[Business]
    """このドラフトが属する事業（ファイル名から判定できない場合は None）。"""

    category: Optional[ContentCategory]
    """このドラフトが属するコンテンツカテゴリ（判定できない場合は None）。"""

    issues: list[ValidationIssue]
    """検出された違反のリスト。空リストならPass。"""

    @property
    def passed(self) -> bool:
        """違反が1件も無ければ True（Pass）。"""
        return not self.issues


def build_check_arg_parser(prog: str) -> argparse.ArgumentParser:
    """`check` サブコマンド用のCLI引数パーサーを構築する。"""
    parser = argparse.ArgumentParser(
        prog=f"{prog} check",
        description="outputs/ に保存済みのドラフトをBrandRulesの検閲ルールで自動バリデーションする。",
    )
    parser.add_argument(
        "--month",
        dest="month",
        type=parse_month_arg,
        default=None,
        help="検閲対象を絞り込む年月 YYYY-MM（省略時は全期間を検閲）",
    )
    return parser


def format_validation_summary(
    results: list[ValidationResult], output_root: str = DEFAULT_OUTPUT_ROOT
) -> str:
    """検閲結果を、月ごとに見出しを分けたPass/Fail一目瞭然なコンソール表示へ整形する。

    Args:
        results: 検閲対象の各ドラフトの結果一覧（投稿予定日昇順を前提とする）。
        output_root: ファイルパス表示を相対パス化する際の基準ディレクトリ。

    Returns:
        str: 標準出力にそのまま表示できる整形済みテキスト。
    """
    if not results:
        return "検閲対象のストックが見つかりませんでした。"

    lines: list[str] = []
    current_month: Optional[str] = None
    fail_count = 0

    for result in results:
        month = result.post_date.strftime("%Y-%m")
        if month != current_month:
            if current_month is not None:
                lines.append("")
            lines.append(f"# {month}")
            current_month = month

        business_label = (
            BUSINESS_LABELS[result.business] if result.business is not None else "(不明な事業)"
        )
        category_label = (
            CATEGORY_LABELS[result.category] if result.category is not None else "(不明なカテゴリ)"
        )
        rel_path = os.path.relpath(result.file_path, start=output_root)

        if result.passed:
            lines.append(f"- ✅ PASS  {result.post_date.isoformat()}｜【{business_label}】{category_label}")
        else:
            fail_count += 1
            lines.append(f"- ❌ FAIL  {result.post_date.isoformat()}｜【{business_label}】{category_label}")
            for issue in result.issues:
                lines.append(f"    ⚠️  [{issue.rule}] {issue.message}")
        lines.append(f"    ファイル: {rel_path}")

    lines.append("")
    passed_count = len(results) - fail_count
    lines.append(f"検閲結果: 合計 {len(results)} 件（PASS {passed_count} / FAIL {fail_count}）")
    return "\n".join(lines)
