"""飲食店向けSNS投稿ドラフト生成サービス。

Instagramキャプション＋ハッシュタグ、LINE公式アカウント配信文、
X（旧Twitter・無料枠140字以内）投稿、Threads投稿（500字以内）、
画像生成AI（Midjourney/DALL-E想定）用の英語プロンプトを一括生成する。
実際のモデル呼び出しは agent_core.AdvisorExecutorAgent に委譲する。
"""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Optional

import anthropic

from agent_core import (
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

    位置引数 `category`（省略時 "all"）でカテゴリを、`--date` で投稿予定日
    （省略時は実行当日の日付）を指定できる。
    """
    parser = argparse.ArgumentParser(
        prog=prog,
        description="大濠うなぎのSNS投稿ドラフトを7カテゴリから選んで生成する。",
    )
    category_choices = ["all"] + [c.value for c in ContentCategory]
    parser.add_argument(
        "category",
        nargs="?",
        default="all",
        choices=category_choices,
        help="生成するコンテンツカテゴリ（省略時は all で7バリエーション一括生成）",
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
    """ファイル名から判定した投稿予定日。"""

    category: Optional[ContentCategory]
    """ファイル名から判定したコンテンツカテゴリ（判定できない場合は None）。"""

    generated_at: str
    """ファイル冒頭のメタデータコメントから読み取った生成日時（読み取れない場合は "不明"）。"""

    @property
    def category_label(self) -> str:
        """コンソール表示用のカテゴリ日本語ラベル。"""
        return CATEGORY_LABELS[self.category] if self.category is not None else "(不明なカテゴリ)"


_STOCK_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(.+)\.md$")
_METADATA_GENERATED_AT_RE = re.compile(r"生成日時:\s*(.+)")


def _parse_stock_file(file_path: str) -> Optional[StockEntry]:
    """1ファイルをファイル名規則とメタデータコメントからパースする。

    ファイル名が `YYYY-MM-DD_カテゴリ名.md` 規則に合致しない、または投稿予定日が
    パースできない場合は `None` を返し、一覧から静かに除外する（堅牢性優先）。
    """
    filename = os.path.basename(file_path)
    name_match = _STOCK_FILENAME_RE.match(filename)
    if not name_match:
        return None

    date_str, category_slug = name_match.groups()
    try:
        post_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None

    try:
        category = ContentCategory(category_slug)
    except ValueError:
        category = None

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
        file_path=file_path, post_date=post_date, category=category, generated_at=generated_at
    )


def scan_output_stock(
    output_root: str = DEFAULT_OUTPUT_ROOT,
    month: Optional[str] = None,
    target_date: Optional[date] = None,
) -> list[StockEntry]:
    """`outputs/` 以下にストックされたMarkdownドラフトをスキャンし、メタデータ付きで一覧化する。

    Args:
        output_root: スキャン対象のルートディレクトリ（既定は `DEFAULT_OUTPUT_ROOT`）。
        month: 指定した場合、その年月（"YYYY-MM"）のサブフォルダのみをスキャンする。
        target_date: 指定した場合、その投稿予定日のファイルのみに絞り込む。

    Returns:
        list[StockEntry]: 投稿予定日の昇順（同日内はカテゴリ名順）に並んだ一覧。
            `output_root` が存在しない場合は空リストを返す（堅牢性優先、エラーにしない）。
    """
    if not os.path.isdir(output_root):
        return []

    if month:
        month_dirs = [os.path.join(output_root, month)]
    else:
        month_dirs = sorted(
            os.path.join(output_root, name)
            for name in os.listdir(output_root)
            if os.path.isdir(os.path.join(output_root, name))
        )

    entries: list[StockEntry] = []
    for month_dir in month_dirs:
        if not os.path.isdir(month_dir):
            continue
        for filename in sorted(os.listdir(month_dir)):
            if not filename.endswith(".md"):
                continue
            entry = _parse_stock_file(os.path.join(month_dir, filename))
            if entry is None:
                continue
            if target_date is not None and entry.post_date != target_date:
                continue
            entries.append(entry)

    entries.sort(key=lambda e: (e.post_date, e.category_label))
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
        lines.append(f"- 📅 {entry.post_date.isoformat()}｜【{entry.category_label}】")
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
