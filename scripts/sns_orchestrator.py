"""SNS自動運用オーケストレーター（Phase 1 基盤）。

既存の最適化済みスキル ``insta-food-buzz``（~/.claude/skills/insta-food-buzz/）の
ペルソナ・品質スコアリング基準・CTAフォーマットを **読み取り専用** で参照し、
投稿テーマから以下を自動生成して「マルチモーダル投稿キュー」(``data/sns_queue.json``)
へストックする。

  1. テキスト（キャプション・ハッシュタグ・フィード表紙コピー3案）
  2. ビジュアル仕様（``visual_spec``）
     - ``asset_edit`` : ``data/assets/`` の既存宣材写真を使う加工指示
       （9:16 / 1:1 リサイズ・トリミング・文字入れレイアウト）
     - ``ai_image``   : Gemini / Adobe Firefly 向けの静止画生成プロンプト
     - ``ai_video``   : Adobe Firefly (Veo 3.1) 向けの動画生成プロンプト
       （英語プロンプト・カメラワーク・9:16・参照画像アセット指定）

設計方針:
  - 外部APIによる実投稿は一切行わない。ローカルでの投稿案・生成プロンプト・
    加工指示のストック（ドライラン）に留める。
  - ``insta-food-buzz`` を含む既存スキルのファイルは絶対に変更しない。
  - スキル資産が読み込めない環境でも、内蔵ルールで安全に継続する
    （スキルはあくまで品質の「核」として活用し、依存では落とさない）。

実行例:
    python3 scripts/sns_orchestrator.py --demo        # デモ投稿案を生成しキューへ格納
    python3 scripts/sns_orchestrator.py --list         # 現在のキュー内容を要約表示
    python3 scripts/sns_orchestrator.py --seed-assets   # ダミー宣材アセットを再生成
    python3 scripts/sns_orchestrator.py --add-theme "今週末の限定 夏野菜カレー"
                                                       # テーマを1件クイック追加
    python3 scripts/sns_orchestrator.py --list-queue    # キューをテーブル形式で一覧表示
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# パス解決（このファイルは <workspace>/scripts/ 配下にある想定）
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_ROOT = os.path.dirname(SCRIPT_DIR)

DATA_DIR = os.path.join(WORKSPACE_ROOT, "data")
ASSETS_DIR = os.path.join(DATA_DIR, "assets")
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
QUEUE_PATH = os.path.join(DATA_DIR, "sns_queue.json")
ASSET_MANIFEST_PATH = os.path.join(ASSETS_DIR, "manifest.json")

# 既存スキル（読み取り専用リファレンス）
SKILL_DIR = os.path.expanduser("~/.claude/skills/insta-food-buzz")

# 走査対象とする画像/動画アセットの拡張子
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".heic")
VIDEO_EXTS = (".mp4", ".mov", ".m4v")
ASSET_EXTS = IMAGE_EXTS + VIDEO_EXTS

# 許容値
PLATFORMS = ("instagram", "x", "threads")
VISUAL_TYPES = ("asset_edit", "ai_image", "ai_video")
VISUAL_MODES = ("auto",) + VISUAL_TYPES
STATUSES = ("queued", "posted")


class OrchestratorError(RuntimeError):
    """オーケストレーター処理中の回復不能なエラー。"""


# ===========================================================================
# 1) 既存スキル資産の読み込み（読み取り専用・変更しない）
# ===========================================================================
@dataclass(frozen=True)
class SkillKnowledge:
    """``insta-food-buzz`` スキルから読み込んだ生成知見一式（読み取り専用）。"""

    available: bool
    persona_and_rules: str = ""
    scoring_criteria: str = ""
    cta_format: str = ""
    source_dir: str = SKILL_DIR


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _strip_frontmatter(markdown_text: str) -> str:
    """先頭のYAMLフロントマター（--- ... ---）を除去した本文を返す。"""
    lines = markdown_text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[i + 1:]).strip()
    return markdown_text.strip()


def load_skill_knowledge(skill_dir: str = SKILL_DIR) -> SkillKnowledge:
    """insta-food-buzz スキルの3ファイルを読み取り専用で読み込む。

    スキルが見つからない・読めない場合も例外を送出せず ``available=False`` の
    SkillKnowledge を返し、呼び出し側が内蔵ルールで継続できるようにする。
    """
    try:
        persona = _strip_frontmatter(_read_text(os.path.join(skill_dir, "SKILL.md")))
        scoring = _read_text(os.path.join(skill_dir, "scoring_criteria.md"))
        cta = _read_text(os.path.join(skill_dir, "cta_examples.md"))
    except OSError:
        return SkillKnowledge(available=False, source_dir=skill_dir)
    return SkillKnowledge(
        available=True,
        persona_and_rules=persona,
        scoring_criteria=scoring,
        cta_format=cta,
        source_dir=skill_dir,
    )


def build_skill_prompt(knowledge: SkillKnowledge, req: "PostRequest") -> str:
    """insta-food-buzz の知見ダイジェスト＋今回の入力を1本の生成プロンプトへ結合する。

    「insta-food-buzz スキルを活用して高品質なテキストを生成させる処理」の実体。
    実運用では本文字列をそのままLLMへ渡してキャプションを生成できる。ドライラン
    （本Phase）では ``CaptionEngine`` がこのプロンプトが要求する仕様に忠実な
    決定的テキストを生成するため、APIキー無しでも一貫した結果を得られる。
    """
    if knowledge.available:
        digest = (
            "==== 【ペルソナ・出力ルール（insta-food-buzz/SKILL.md）】 ====\n"
            f"{knowledge.persona_and_rules}\n\n"
            "==== 【品質スコアリング基準（scoring_criteria.md・合格ライン96点以上/120点）】 ====\n"
            f"{knowledge.scoring_criteria}\n\n"
            "==== 【CTAフォーマット（cta_examples.md）】 ====\n"
            f"{knowledge.cta_format}\n"
        )
    else:
        digest = "（insta-food-buzz スキル資産を読み込めなかったため内蔵ルールで生成します）"
    return (
        "あなたは飲食店Instagram運用で万バズを量産してきたプロのSNSマーケターです。"
        "以下の既存スキル基準を厳守し、冒頭フック・五感の具体描写・限定性・自然なCTA・"
        "大中小のハッシュタグ設計を満たすキャプションを作成してください。\n\n"
        f"{digest}\n"
        "---- 今回の投稿情報 ----\n"
        f"{req.to_brief_text()}\n"
    )


# ===========================================================================
# 2) 投稿リクエスト（オーケストレーターへの入力）
# ===========================================================================
@dataclass
class PostRequest:
    """1件の投稿案生成リクエスト。"""

    theme: str
    menu_name: str
    genre: str = "unagi"
    platform: str = "instagram"
    scheduled_at: str = ""
    store_name: str = "大濠うなぎ"
    region: str = "福岡"
    sensory: List[str] = field(default_factory=list)
    limited_info: str = ""
    want_video: bool = False
    visual_mode: str = "auto"           # auto / asset_edit / ai_image / ai_video
    camera_motion: str = ""              # ai_video時のカメラワーク上書き（任意）
    extra_keywords: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.platform not in PLATFORMS:
            raise OrchestratorError(
                f"platform は {PLATFORMS} のいずれかを指定してください: '{self.platform}'"
            )
        if self.visual_mode not in VISUAL_MODES:
            raise OrchestratorError(
                f"visual_mode は {VISUAL_MODES} のいずれかを指定してください: '{self.visual_mode}'"
            )

    def match_keywords(self) -> List[str]:
        """アセット照合に使うキーワード群（テーマ・メニュー・追加語）。"""
        return [self.theme, self.menu_name, self.genre, *self.extra_keywords]

    def to_brief_text(self) -> str:
        sensory = "、".join(self.sensory) if self.sensory else "（未指定）"
        return (
            f"テーマ: {self.theme}\n"
            f"メニュー名: {self.menu_name}\n"
            f"ジャンル: {self.genre}\n"
            f"店舗名: {self.store_name} / エリア: {self.region}\n"
            f"五感の特徴: {sensory}\n"
            f"限定性: {self.limited_info or '（なし）'}\n"
            f"媒体: {self.platform} / 動画化: {'はい' if self.want_video else 'いいえ'}"
        )


# ===========================================================================
# 3) アセットライブラリ（data/assets/ の走査＋テーマ照合）
# ===========================================================================
@dataclass(frozen=True)
class Asset:
    file: str
    menu_name: str = ""
    genre: str = ""
    keywords: List[str] = field(default_factory=list)
    orientation: str = ""      # landscape / portrait / square
    description: str = ""

    @property
    def is_video(self) -> bool:
        return self.file.lower().endswith(VIDEO_EXTS)


class AssetLibrary:
    """``data/assets/`` の宣材アセットを走査・照合する。

    manifest.json があればそのメタデータを優先し、無いファイルはファイル名から
    最小限のメタデータを補完する。テーマ・メニュー名・キーワードに対して、
    キーワード一致数でスコアリングして最良のアセットを返す。
    """

    def __init__(self, assets_dir: str = ASSETS_DIR) -> None:
        self.assets_dir = assets_dir
        self.assets: List[Asset] = self._load()

    def _load(self) -> List[Asset]:
        if not os.path.isdir(self.assets_dir):
            return []

        manifest_meta: Dict[str, Dict[str, Any]] = {}
        if os.path.isfile(ASSET_MANIFEST_PATH):
            try:
                with open(ASSET_MANIFEST_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for entry in data.get("assets", []):
                    fname = entry.get("file")
                    if fname:
                        manifest_meta[fname] = entry
            except (OSError, json.JSONDecodeError):
                manifest_meta = {}  # マニフェスト破損時はファイル走査のみで継続

        assets: List[Asset] = []
        for fname in sorted(os.listdir(self.assets_dir)):
            if not fname.lower().endswith(ASSET_EXTS):
                continue
            meta = manifest_meta.get(fname, {})
            stem = os.path.splitext(fname)[0]
            assets.append(
                Asset(
                    file=fname,
                    menu_name=meta.get("menu_name", ""),
                    genre=meta.get("genre", ""),
                    keywords=list(meta.get("keywords", [])) or stem.replace("_", " ").split(),
                    orientation=meta.get("orientation", ""),
                    description=meta.get("description", ""),
                )
            )
        return assets

    def match(self, keywords: List[str], genre: str = "") -> Optional[Asset]:
        """キーワード一致スコアが最も高いアセットを返す（0点なら None）。"""
        needle = " ".join(k for k in keywords if k).lower()
        best: Optional[Asset] = None
        best_score = 0
        for asset in self.assets:
            score = 0
            for kw in asset.keywords:
                if kw and kw.lower() in needle:
                    score += 2
            if genre and asset.genre and genre.lower() == asset.genre.lower():
                score += 1
            if asset.menu_name and asset.menu_name.lower() in needle:
                score += 3
            if score > best_score:
                best_score = score
                best = asset
        return best if best_score > 0 else None


# テーマ文字列からジャンルを推定するための組み込みキーワードヒント
# （data/assets/ に未登録のジャンルもここでカバーする）。
_GENRE_KEYWORD_HINTS: Dict[str, List[str]] = {
    "unagi": ["うなぎ", "鰻", "うな重", "釜まぶし", "unagi"],
    "sweets": ["パフェ", "スイーツ", "デザート", "アイス", "ケーキ", "パンケーキ"],
    "drink": ["ドリンク", "ジンジャーエール", "ジュース", "カクテル", "コーヒー", "ラテ"],
    "noodle": ["麺", "ラーメン", "坦々麺", "うどん", "そば", "パスタ"],
    "curry": ["カレー"],
}


def guess_genre_from_theme(theme: str, library: Optional[AssetLibrary] = None) -> str:
    """テーマ文字列からジャンルを推定する（``--add-theme`` でジャンル省略時に使用）。

    まず ``library`` が保持するアセットのメニュー名・キーワードとの一致を
    最優先で確認し（既存宣材があるジャンルを正しく引き当てるため）、次に
    組み込みのジャンルキーワードヒントで判定する。いずれにも一致しない場合は
    ``"other"`` を返し、ハッシュタグ生成側は汎用グルメ扱いへ安全にフォールバックする。
    """
    lowered = theme.lower()

    if library is not None:
        for asset in library.assets:
            if not asset.genre:
                continue
            if asset.menu_name and asset.menu_name.lower() in lowered:
                return asset.genre
            if any(kw and kw.lower() in lowered for kw in asset.keywords):
                return asset.genre

    for genre, hints in _GENRE_KEYWORD_HINTS.items():
        if any(hint.lower() in lowered for hint in hints):
            return genre

    return "other"


# ===========================================================================
# 4) キャプションエンジン（insta-food-buzz の基準を適用）
# ===========================================================================
@dataclass
class TextContent:
    caption: str
    hashtags: List[str]
    cover_copies: List[str]


class CaptionEngine:
    """insta-food-buzz の scoring_criteria（冒頭フック・五感・限定性・CTA・
    ハッシュタグ大中小）を適用してキャプション一式を組み立てる決定的エンジン。

    実LLMを呼ばずとも、スキルが要求する構造・観点を満たすテキストを生成する
    （ドライランでAPIキー不要・一貫した出力）。生成に用いるプロンプトは
    ``build_skill_prompt`` が組み立て、``skill_prompt`` として保持できる。
    """

    def __init__(self, knowledge: SkillKnowledge) -> None:
        self.knowledge = knowledge

    def generate(self, req: PostRequest) -> TextContent:
        caption = self._build_caption(req)
        hashtags = self._build_hashtags(req)
        covers = self._build_cover_copies(req)
        return TextContent(caption=caption, hashtags=hashtags, cover_copies=covers)

    # --- 本文 -------------------------------------------------------------
    def _build_caption(self, req: PostRequest) -> str:
        lead_sense = req.sensory[0] if req.sensory else "湯気の立つ一皿"
        body_senses = "、".join(req.sensory[1:]) if len(req.sensory) > 1 else "口いっぱいに広がる旨み"
        limited = req.limited_info.strip()

        hook = f"え、この{lead_sense}…もう反則。"
        if limited:
            hook = f"【{limited}】え、この{lead_sense}…もう反則。"

        body = (
            f"{req.store_name}（{req.region}）の「{req.menu_name}」。\n"
            f"ひと口ごとに、{lead_sense}。そして{body_senses}が追いかけてくる。\n"
            f"写真の湯気とツヤ、そのまま香りまで届けたいくらいです。"
        )
        if limited:
            body += f"\n{limited}なので、気になった方はお早めに。"

        cta = "保存して、次のお休みの“行きたいリスト”に入れておいてね。"
        return f"{hook}\n\n{body}\n\n{cta}"

    # ジャンル→（中規模ジャンルタグ, 小規模「地域＋ジャンル」用の語）
    _GENRE_TAGS = {
        "unagi": ("#うなぎ", "うなぎ"),
        "sweets": ("#スイーツ", "スイーツ"),
        "drink": ("#ドリンク", "カフェ"),
    }

    # --- ハッシュタグ（大・中・小の規模感を混在、30個以内） ----------------
    def _build_hashtags(self, req: PostRequest) -> List[str]:
        genre_tag, area_word = self._GENRE_TAGS.get(req.genre, ("#グルメ", "グルメ"))
        mega = ["#グルメ", "#飯テロ", "#foodie"]                     # 大（100万+）
        middle = [                                                   # 中（10-100万）
            f"#{req.region}グルメ",
            f"#{req.region}ランチ",
            f"#{req.region}ディナー",
            genre_tag,
        ]
        niche = [                                                    # 小（1-10万・発見される）
            f"#{req.store_name}",
            f"#{req.region}{area_word}",
            f"#{req.menu_name}",
        ]
        if req.limited_info:
            niche.append("#季節限定")
        niche.extend(f"#{kw}" for kw in req.extra_keywords)

        # 重複除去（順序維持）し30個以内へ丸める
        seen: set = set()
        ordered: List[str] = []
        for tag in [*mega, *middle, *niche]:
            t = tag.replace(" ", "")
            if t and t not in seen:
                seen.add(t)
                ordered.append(t)
        return ordered[:30]

    # --- フィード表紙コピー（15字以内・3案） ------------------------------
    def _build_cover_copies(self, req: PostRequest) -> List[str]:
        lead = req.sensory[0] if req.sensory else "悶絶級の一皿"
        base = [
            f"🔥反則級の{req.menu_name}",
            f"📸{req.region}の隠れ名店",
            f"😳この{lead}、知ってた？",
        ]
        if req.limited_info:
            base[1] = f"⏳{req.limited_info}"
        # 15字以内へトリム（絵文字は1字換算）
        return [c[:15] for c in base]


# ===========================================================================
# 5) ビジュアル仕様ビルダー（asset_edit / ai_image / ai_video の判定＋生成）
# ===========================================================================
@dataclass
class AIPromptSpec:
    target_model: str                    # "Adobe Firefly (Veo 3.1)" | "Gemini"
    prompt_ja: str
    prompt_en: str
    camera_motion: str
    aspect_ratio: str
    reference_image_setting: Optional[str] = None


@dataclass
class ImageEditInstruction:
    resize: str                          # "9:16" | "1:1" | "4:5"
    crop: str
    text_overlay: List[Dict[str, str]]
    notes: str = ""


@dataclass
class VisualSpec:
    type: str                            # asset_edit / ai_image / ai_video
    source_asset: Optional[str] = None
    image_edit_instruction: Optional[ImageEditInstruction] = None
    ai_prompt_spec: Optional[AIPromptSpec] = None


# 媒体・意図に応じたアスペクト比
def _aspect_for(platform: str, is_video: bool) -> str:
    if is_video:
        return "9:16"                    # Reels / Stories / TikTok
    if platform == "instagram":
        return "1:1"                     # フィード静止画
    return "1:1"


class VisualSpecBuilder:
    """テーマ・アセット有無・媒体からビジュアル仕様を判定して構築する。"""

    def __init__(self, library: AssetLibrary) -> None:
        self.library = library

    def build(self, req: PostRequest, notes_sink: Optional[List[str]] = None) -> VisualSpec:
        notes = notes_sink if notes_sink is not None else []
        asset = self.library.match(req.match_keywords(), genre=req.genre)

        effective = self._decide_type(req, asset, notes)

        if effective == "asset_edit":
            return self._build_asset_edit(req, asset)  # type: ignore[arg-type]
        if effective == "ai_video":
            return self._build_ai_video(req, asset)
        return self._build_ai_image(req, asset)

    def _decide_type(
        self, req: PostRequest, asset: Optional[Asset], notes: List[str]
    ) -> str:
        if req.visual_mode != "auto":
            # 明示指定。ただし asset_edit なのに素材が無ければ ai_image へ安全に降格。
            if req.visual_mode == "asset_edit" and asset is None:
                notes.append(
                    "visual_mode=asset_edit が指定されましたが該当アセットが無いため ai_image へ降格しました。"
                )
                return "ai_image"
            return req.visual_mode

        # auto 判定
        if req.want_video:
            return "ai_video"
        if asset is not None:
            return "asset_edit"
        return "ai_image"

    # --- asset_edit：既存宣材の加工指示 -----------------------------------
    def _build_asset_edit(self, req: PostRequest, asset: Asset) -> VisualSpec:
        aspect = _aspect_for(req.platform, is_video=False)
        cover = f"{req.limited_info or req.menu_name}"
        instruction = ImageEditInstruction(
            resize=aspect,
            crop=(
                f"主役の{req.menu_name}を中央〜やや上に配置し、{aspect}へ再構図。"
                f"余白（重箱の縁・テーブル）を活かしつつ被写体を画面の70%程度まで寄せる。"
            ),
            text_overlay=[
                {
                    "text": cover[:15],
                    "position": "top-center",
                    "style": "太ゴシック・白文字＋黒縁取り・角丸帯（半透明黒）",
                },
                {
                    "text": f"{req.store_name}｜{req.region}",
                    "position": "bottom-left",
                    "style": "細ゴシック・白文字・小サイズ",
                },
            ],
            notes=(
                f"元アセット: {asset.file}（{asset.description or asset.menu_name}）。"
                "文字は安全マージン内（上下12%）に収め、料理のシズル部分に被せない。"
            ),
        )
        return VisualSpec(
            type="asset_edit",
            source_asset=asset.file,
            image_edit_instruction=instruction,
            ai_prompt_spec=None,
        )

    # --- ai_video：Adobe Firefly (Veo 3.1) 動画生成プロンプト -------------
    def _build_ai_video(self, req: PostRequest, asset: Optional[Asset]) -> VisualSpec:
        aspect = "9:16"
        camera = req.camera_motion or "Slow cinematic zoom-in (dolly-in) toward the dish"
        subject_en = self._subject_en(req, asset)
        motion_en = self._motion_en(req)

        prompt_en = (
            f"{subject_en}. {motion_en}. "
            f"Camera: {camera}, subtle handheld micro-movement for life-like feel. "
            "Lighting: warm directional key light with soft rim light, glossy specular highlights on the glaze. "
            "Steam gently rising and drifting, sauce glistening, ultra-appetizing sizzle. "
            "Mood: premium yet approachable Japanese restaurant. "
            "Style: photoreal food cinematography, shallow depth of field, 4K, 24fps, "
            f"vertical {aspect} aspect ratio for Instagram Reels. Duration ~5-8s, seamless loop-friendly."
        )
        if asset is not None:
            prompt_en += f" Use the reference image '{asset.file}' as the exact starting frame and animate from it."

        prompt_ja = (
            f"{req.menu_name}を主役にした縦型ショート動画。{camera}。"
            "湯気がゆっくり立ち上り、タレの照りがきらめくシズル演出。"
            "暖色の斜光＋ソフトなリムライト、浅い被写界深度でプレミアム感を演出。"
            f"9:16・4K・24fps・5〜8秒・ループ向き。"
            + (f"参照画像 '{asset.file}' を開始フレームに設定して動かす。" if asset else "")
        )

        return VisualSpec(
            type="ai_video",
            source_asset=asset.file if asset else None,
            image_edit_instruction=None,
            ai_prompt_spec=AIPromptSpec(
                target_model="Adobe Firefly (Veo 3.1)",
                prompt_ja=prompt_ja,
                prompt_en=prompt_en,
                camera_motion=camera,
                aspect_ratio=aspect,
                reference_image_setting=asset.file if asset else None,
            ),
        )

    # --- ai_image：Gemini 静止画生成プロンプト ---------------------------
    def _build_ai_image(self, req: PostRequest, asset: Optional[Asset]) -> VisualSpec:
        aspect = _aspect_for(req.platform, is_video=False)
        subject_en = self._subject_en(req, asset)
        prompt_en = (
            f"{subject_en}. "
            "Professional food photography, top-down and 45-degree hybrid framing, "
            "warm directional lighting, appetizing specular highlights and natural texture, "
            "shallow depth of field, rich appetizing colors, clean minimal Japanese table setting, "
            f"high detail, photoreal, {aspect} aspect ratio."
        )
        prompt_ja = (
            f"{req.menu_name}のプロ品質の料理写真。俯瞰と斜め45度の中間アングル、"
            "暖色の斜光、照りのあるタレと立ち上る湯気、浅い被写界深度、"
            f"和の清潔なテーブルセット、高精細・フォトリアル、{aspect}。"
        )
        return VisualSpec(
            type="ai_image",
            source_asset=asset.file if asset else None,
            image_edit_instruction=None,
            ai_prompt_spec=AIPromptSpec(
                target_model="Gemini",
                prompt_ja=prompt_ja,
                prompt_en=prompt_en,
                camera_motion="static",
                aspect_ratio=aspect,
                reference_image_setting=asset.file if asset else None,
            ),
        )

    # --- 英語プロンプト部品（Veo 3.1/Firefly/Gemini高精度生成のため純英語に統一） --
    def _subject_en(self, req: PostRequest, asset: Optional[Asset]) -> str:
        genre_hint = {
            "unagi": (
                "a premium char-grilled Japanese unagi (freshwater eel) rice bowl (una-ju), "
                "glossy kabayaki soy glaze, tender flaky fillet over steamed rice in a lacquer box"
            ),
            "sweets": (
                "an elegant Japanese matcha parfait in a tall glass, "
                "layered matcha ice cream, shiratama mochi, red bean paste and matcha sauce"
            ),
            "drink": (
                "a refreshing house-made craft drink in a chilled glass with condensation, "
                "sparkling bubbles rising, ice and a garnish"
            ),
        }.get(req.genre, "a beautifully styled premium Japanese food or drink")
        subject = f"A close-up of {genre_hint}"
        if asset is not None:
            subject += " (styled exactly as in the reference image)"
        return subject

    def _motion_en(self, req: PostRequest) -> str:
        return (
            "Chopsticks gently lift a glistening piece, revealing fluffy steaming texture; "
            "sauce drips slowly"
            if req.genre == "unagi"
            else "A spoon slowly scoops through the layers, showing the cross-section"
        )


# ===========================================================================
# 6) キュー項目（sns_queue.json のレコード）とキュー管理
# ===========================================================================
@dataclass
class QueueItem:
    id: str
    platform: str
    scheduled_at: str
    status: str
    theme: str
    text_content: TextContent
    visual_spec: VisualSpec
    created_at: str = ""
    skill_source: str = ""
    build_notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def _make_id(theme: str, platform: str) -> str:
    """テーマ＋媒体から安定した投稿IDを生成する（同一入力→同一ID・冪等）。"""
    digest = hashlib.sha1(f"{platform}|{theme}".encode("utf-8")).hexdigest()[:10]
    return f"sns_{digest}"


class SNSQueue:
    """``data/sns_queue.json`` の読み書きと項目の追加・upsert を担う。"""

    def __init__(self, path: str = QUEUE_PATH) -> None:
        self.path = path
        self.items: List[Dict[str, Any]] = self._load()

    def _load(self) -> List[Dict[str, Any]]:
        if not os.path.isfile(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise OrchestratorError(f"キューの読み込みに失敗しました: {self.path}: {exc}") from exc
        return list(data.get("queue", []))

    def upsert(self, item: QueueItem) -> None:
        """同一IDがあれば置換、無ければ追加（ドライラン再実行で重複しない）。"""
        record = item.to_dict()
        for i, existing in enumerate(self.items):
            if existing.get("id") == record["id"]:
                self.items[i] = record
                return
        self.items.append(record)

    def clear(self) -> None:
        self.items = []

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        payload = {
            "schema_version": "1.0",
            "generated_by": "scripts/sns_orchestrator.py",
            "count": len(self.items),
            "queue": self.items,
        }
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            raise OrchestratorError(f"キューの保存に失敗しました: {self.path}: {exc}") from exc


# ===========================================================================
# 7) オーケストレーター本体
# ===========================================================================
class SNSOrchestrator:
    """スキル連携テキスト生成＋ビジュアル仕様生成＋キュー格納を束ねる。"""

    def __init__(self) -> None:
        self.knowledge = load_skill_knowledge()
        self.library = AssetLibrary()
        self.caption_engine = CaptionEngine(self.knowledge)
        self.visual_builder = VisualSpecBuilder(self.library)

    def build_item(self, req: PostRequest, created_at: str) -> QueueItem:
        """1件の PostRequest から QueueItem（投稿案）を構築する。実投稿はしない。"""
        # スキルプロンプトを組み立て（活用の実体。ドライランではテキスト生成の指針）。
        _ = build_skill_prompt(self.knowledge, req)

        text = self.caption_engine.generate(req)
        notes: List[str] = []
        visual = self.visual_builder.build(req, notes_sink=notes)

        skill_source = (
            f"insta-food-buzz@{self.knowledge.source_dir}"
            if self.knowledge.available
            else "builtin-rules (insta-food-buzz 未読込)"
        )
        return QueueItem(
            id=_make_id(req.theme, req.platform),
            platform=req.platform,
            scheduled_at=req.scheduled_at,
            status="queued",
            theme=req.theme,
            text_content=text,
            visual_spec=visual,
            created_at=created_at,
            skill_source=skill_source,
            build_notes=notes,
        )


# ===========================================================================
# 8) ディレクトリ準備・ダミーアセット生成
# ===========================================================================
def ensure_dirs() -> None:
    for d in (DATA_DIR, ASSETS_DIR, OUTPUT_DIR):
        os.makedirs(d, exist_ok=True)


def seed_demo_assets(force: bool = False) -> List[str]:
    """ダミー宣材アセット（JPEG）＋ manifest.json を生成する。

    PIL があれば見出し付きのプレースホルダーJPEGを描画。無い場合は最小限の
    有効なJPEGバイト列を書き出してフォールバックする（依存で落とさない）。
    """
    ensure_dirs()
    specs = [
        ("unagi_don.jpg", "UNAGI-DON (Una-ju)", (94, 52, 30),
         {"menu_name": "特上うな重", "genre": "unagi",
          "keywords": ["うな重", "うなぎ", "鰻", "土用の丑", "特上", "unagi", "unadon"],
          "orientation": "landscape",
          "description": "重箱に盛られた特上うな重。照りのあるタレ、炭火焼きの艶、山椒の小皿。"}),
        ("unagi_kamameshi.jpg", "UNAGI KAMA-MABUSHI", (120, 74, 38),
         {"menu_name": "明太白釜まぶし", "genre": "unagi",
          "keywords": ["釜まぶし", "釜飯", "明太", "うなぎ", "鰻", "kamameshi", "kamamabushi"],
          "orientation": "landscape",
          "description": "釜炊きご飯の上に炭火焼きうなぎと明太子。湯気とツヤ。"}),
        ("matcha_parfait.jpg", "MATCHA PARFAIT", (74, 103, 65),
         {"menu_name": "季節限定 抹茶パフェ", "genre": "sweets",
          "keywords": ["抹茶", "パフェ", "スイーツ", "季節限定", "matcha", "parfait"],
          "orientation": "portrait",
          "description": "背の高いグラスに抹茶アイス・白玉・あんこ・抹茶ソースの層。"}),
    ]
    written: List[str] = []
    for fname, label, bg, _meta in specs:
        path = os.path.join(ASSETS_DIR, fname)
        if os.path.isfile(path) and not force:
            continue
        _write_placeholder_jpeg(path, label, bg)
        written.append(fname)

    manifest = {
        "_note": "data/assets/ 内の宣材アセットのメタデータ。実ファイルはダミー（プレースホルダー）。",
        "assets": [{"file": f, **m} for f, _l, _bg, m in specs],
    }
    with open(ASSET_MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return written


# 最小限の有効なJPEG（2x2・グレー）: PIL不在時のフォールバック用（base64で埋め込み）
_MIN_JPEG_B64 = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcgLikxMC4pLSwzOko+MzZGNywtQFdBRkxOUlNSMj5aYVpQYEpRUk//2wBDAQ4ODhMREyYVFSZPNS01T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0//wAARCAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwAooooA/9k="
_MIN_JPEG = __import__("base64").b64decode(_MIN_JPEG_B64)


def _write_placeholder_jpeg(path: str, label: str, bg: tuple) -> None:
    try:
        from PIL import Image, ImageDraw  # 遅延import（コア機能はPIL非依存）

        img = Image.new("RGB", (1200, 900), bg)
        d = ImageDraw.Draw(img)
        d.rectangle([40, 40, 1160, 860], outline=(240, 232, 210), width=6)
        d.text((70, 70), "PLACEHOLDER ASSET (senzai stand-in)", fill=(240, 232, 210))
        d.text((70, 420), label, fill=(255, 246, 224))
        d.text((70, 800), os.path.basename(path), fill=(220, 210, 190))
        img.save(path, "JPEG", quality=82)
    except Exception:  # noqa: BLE001 - PIL不在/描画失敗でも最小JPEGで継続
        with open(path, "wb") as f:
            f.write(_MIN_JPEG)


# ===========================================================================
# 9) デモ（ドライラン）
# ===========================================================================
def demo_requests() -> List[PostRequest]:
    """ドライラン用の投稿案リクエスト群（決定的な固定日時）。"""
    return [
        PostRequest(
            theme="土用の丑の日 特上うな重",
            menu_name="特上うな重",
            genre="unagi",
            platform="instagram",
            scheduled_at="2026-07-24T11:30:00+09:00",
            store_name="大濠うなぎ",
            region="福岡",
            sensory=["ふわっとほどける身", "炭火の香ばしさ", "甘辛ダレの照り"],
            limited_info="土用の丑の日限定",
            want_video=True,                       # 宣材写真を動画化（Veo 3.1）
            visual_mode="auto",
            extra_keywords=["土用の丑"],
        ),
        PostRequest(
            theme="明太白釜まぶし フェア",
            menu_name="明太白釜まぶし",
            genre="unagi",
            platform="instagram",
            scheduled_at="2026-07-26T18:00:00+09:00",
            store_name="大濠うなぎ",
            region="福岡",
            sensory=["プチプチの明太子", "釜炊きご飯の湯気", "香ばしいうなぎ"],
            limited_info="",
            want_video=False,                      # 既存写真を加工（asset_edit）
            visual_mode="auto",
        ),
        PostRequest(
            theme="季節限定 抹茶パフェ",
            menu_name="季節限定 抹茶パフェ",
            genre="sweets",
            platform="instagram",
            scheduled_at="2026-07-28T15:00:00+09:00",
            store_name="大濠うなぎ",
            region="福岡",
            sensory=["濃厚な抹茶の苦み", "ふわとろ抹茶アイス", "もちもち白玉"],
            limited_info="夏季限定",
            want_video=True,                       # 参照付き動画化
            visual_mode="auto",
        ),
        PostRequest(
            theme="自家製クラフトジンジャーエール（宣材なし・AI画像）",
            menu_name="自家製クラフトジンジャーエール",
            genre="drink",                          # マニフェストに該当アセット無し
            platform="instagram",
            scheduled_at="2026-07-30T12:00:00+09:00",
            store_name="大濠うなぎ",
            region="福岡",
            sensory=["生姜の爽やかな刺激", "はじける炭酸"],
            limited_info="新作",
            want_video=False,
            visual_mode="ai_image",                # 該当アセット無し→純AI静止画（参照null）
        ),
    ]


def run_demo() -> str:
    """デモ投稿案を生成しキューへ upsert して保存。保存先パスを返す。"""
    ensure_dirs()
    if not AssetLibrary().assets:
        seed_demo_assets()

    orchestrator = SNSOrchestrator()
    queue = SNSQueue()
    queue.clear()  # デモは正準セットの再構築（古い項目を持ち越さない）
    created_at = "2026-07-21T00:00:00+09:00"  # ドライランは固定タイムスタンプで冪等

    for req in demo_requests():
        item = orchestrator.build_item(req, created_at=created_at)
        queue.upsert(item)

    queue.save()
    return queue.path


def _print_summary(queue_path: str) -> None:
    with open(queue_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("queue", [])
    print(f"投稿キュー: {queue_path}（{len(items)} 件）\n")
    for it in items:
        vs = it["visual_spec"]
        vtype = vs["type"]
        line = f"- [{it['status']}] {it['scheduled_at']}  {it['platform']}  «{it['theme']}»"
        print(line)
        print(f"    visual: {vtype}", end="")
        if vs.get("source_asset"):
            print(f" / asset: {vs['source_asset']}", end="")
        aps = vs.get("ai_prompt_spec")
        if aps:
            print(f" / model: {aps['target_model']} / {aps['aspect_ratio']} / cam: {aps['camera_motion']}", end="")
        print()
        print(f"    hashtags({len(it['text_content']['hashtags'])}): {' '.join(it['text_content']['hashtags'][:8])} ...")
    print()


def add_theme(
    theme: str,
    *,
    platform: str = "instagram",
    want_video: bool = False,
    genre: Optional[str] = None,
    scheduled_at: Optional[str] = None,
) -> QueueItem:
    """投稿テーマを1件クイック追加し、既存の生成ロジックを回して ``queued``
    ステータスでキューへ追記する（``--add-theme`` の実体）。

    メニュー名は ``theme`` をそのまま使用し、ジャンル省略時は
    ``guess_genre_from_theme`` で自動推定する。宣材アセットが見つからない
    場合は、既存の ``VisualSpecBuilder`` の自動フォールバック設計により
    ``ai_image``（AI静止画生成）へ安全にルーティングされる。

    Args:
        theme: 投稿テーマ（例: "今週末の限定 夏野菜カレー"）。
        platform: 投稿先媒体（既定 "instagram"）。
        want_video: True の場合、宣材があれば ``ai_video``（Veo 3.1）を優先する。
        genre: ジャンルの明示指定（省略時はテーマ文字列から自動推定）。
        scheduled_at: 投稿予定日時（ISO 8601）。省略時は現在時刻を採番する。

    Returns:
        QueueItem: キューへ追加（同一テーマ・媒体なら上書き）された投稿案。

    Raises:
        OrchestratorError: platform が不正な場合（``PostRequest`` の検証による）。
    """
    ensure_dirs()
    if not AssetLibrary().assets:
        seed_demo_assets()

    orchestrator = SNSOrchestrator()
    resolved_genre = genre or guess_genre_from_theme(theme, orchestrator.library)
    resolved_scheduled_at = scheduled_at or datetime.now().astimezone().isoformat(timespec="seconds")
    created_at = datetime.now().astimezone().isoformat(timespec="seconds")

    req = PostRequest(
        theme=theme,
        menu_name=theme,
        genre=resolved_genre,
        platform=platform,
        scheduled_at=resolved_scheduled_at,
        want_video=want_video,
        visual_mode="auto",
    )
    item = orchestrator.build_item(req, created_at=created_at)

    queue = SNSQueue()
    queue.upsert(item)
    queue.save()
    return item


def _print_queue_table(queue_path: str) -> None:
    """キューを ID・ステータス・プラットフォーム・テーマ・Visual Type の
    列でテーブル形式に整形して表示する（``--list-queue`` の実体）。
    """
    with open(queue_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("queue", [])

    print(f"投稿キュー: {queue_path}（{len(items)} 件）\n")
    if not items:
        print("（キューは空です。--add-theme や --demo で追加してください）")
        return

    headers = ("ID", "STATUS", "PLATFORM", "THEME", "VISUAL TYPE")
    rows = [
        (
            it["id"],
            it["status"],
            it["platform"],
            it["theme"],
            it["visual_spec"]["type"],
        )
        for it in items
    ]

    widths = [
        max(len(str(row[col])) for row in ([headers] + rows))
        for col in range(len(headers))
    ]
    # THEME列（日本語混在）は長くなりがちなので上限を設けて折り返し崩れを防ぐ
    widths[3] = min(widths[3], 32)

    def _fmt_row(row: tuple) -> str:
        cells = []
        for col, value in enumerate(row):
            text = str(value)
            if col == 3 and len(text) > widths[3]:
                text = text[: widths[3] - 1] + "…"
            cells.append(text.ljust(widths[col]))
        return " | ".join(cells)

    separator = "-+-".join("-" * w for w in widths)
    print(_fmt_row(headers))
    print(separator)
    for row in rows:
        print(_fmt_row(row))
    print()


# ===========================================================================
# CLI
# ===========================================================================
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sns_orchestrator.py",
        description="SNS自動運用オーケストレーター（Phase 1・投稿キュー生成のドライラン）",
    )
    parser.add_argument("--demo", action="store_true", help="デモ投稿案を生成しキューへ格納する")
    parser.add_argument("--list", action="store_true", help="現在のキュー内容を要約表示する")
    parser.add_argument("--seed-assets", action="store_true", help="ダミー宣材アセットを（再）生成する")
    parser.add_argument("--force", action="store_true", help="--seed-assets 時に既存ファイルも上書きする")
    parser.add_argument(
        "--add-theme",
        metavar="THEME",
        help="投稿テーマを1件クイック追加し、キューへ queued として格納する（例: '今週末の限定 夏野菜カレー'）",
    )
    parser.add_argument(
        "--list-queue",
        action="store_true",
        help="現在のキューを ID/ステータス/プラットフォーム/テーマ/Visual Type のテーブル形式で一覧表示する",
    )
    parser.add_argument(
        "--platform",
        choices=PLATFORMS,
        default="instagram",
        help="--add-theme 時の投稿先媒体（既定: instagram）",
    )
    parser.add_argument(
        "--genre",
        default=None,
        help="--add-theme 時のジャンルを明示指定する（省略時はテーマ文字列から自動推定）",
    )
    parser.add_argument(
        "--video",
        action="store_true",
        help="--add-theme 時、宣材があればAdobe Firefly (Veo 3.1) 動画プロンプトを優先する",
    )
    parser.add_argument(
        "--scheduled-at",
        default=None,
        metavar="ISO8601",
        help="--add-theme 時の投稿予定日時（省略時は現在時刻を採番）",
    )
    args = parser.parse_args(argv)

    try:
        if args.seed_assets:
            written = seed_demo_assets(force=args.force)
            print(f"アセット生成: {ASSETS_DIR}")
            print("  " + (", ".join(written) if written else "（既存のためスキップ／manifest更新のみ）"))
            return 0

        if args.demo:
            path = run_demo()
            print(f"[ドライラン完了] 投稿キューを生成しました: {path}\n")
            _print_summary(path)
            return 0

        if args.add_theme:
            item = add_theme(
                args.add_theme,
                platform=args.platform,
                want_video=args.video,
                genre=args.genre,
                scheduled_at=args.scheduled_at,
            )
            vs = item.visual_spec
            print(f"[追加完了] テーマ「{item.theme}」をキューへ格納しました（id={item.id}）")
            print(f"  status={item.status} / platform={item.platform} / scheduled_at={item.scheduled_at}")
            print(f"  visual: {vs.type}" + (f" / asset: {vs.source_asset}" if vs.source_asset else ""))
            if vs.ai_prompt_spec:
                aps = vs.ai_prompt_spec
                print(f"  model: {aps.target_model} / aspect: {aps.aspect_ratio} / camera: {aps.camera_motion}")
            if item.build_notes:
                for note in item.build_notes:
                    print(f"  [note] {note}")
            print(f"\nキュー保存先: {QUEUE_PATH}")
            return 0

        if args.list_queue:
            if not os.path.isfile(QUEUE_PATH):
                print(f"キューがまだありません: {QUEUE_PATH}（--demo や --add-theme で生成できます）")
                return 0
            _print_queue_table(QUEUE_PATH)
            return 0

        if args.list:
            if not os.path.isfile(QUEUE_PATH):
                print(f"キューがまだありません: {QUEUE_PATH}（--demo で生成できます）")
                return 0
            _print_summary(QUEUE_PATH)
            return 0

        parser.print_help()
        return 0

    except OrchestratorError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
