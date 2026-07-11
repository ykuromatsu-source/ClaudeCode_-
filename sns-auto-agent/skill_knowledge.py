"""既存スキル insta-food-buzz の高品質なプロンプト資産を読み込むモジュール。

~/.claude/skills/insta-food-buzz/ は正解率100%（トリガー評価20/20）を
達成済みの検証済みスキルであり、そのペルソナ定義・品質スコアリング基準・
CTAフォーマットを本プロジェクトの生成エンジンの核として流用する。

このモジュールは対象ディレクトリを読み取り専用のリファレンスとして扱い、
一切の書き込み・変更を行わない。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

SKILL_DIR = os.path.expanduser("~/.claude/skills/insta-food-buzz")

_SKILL_MD = "SKILL.md"
_SCORING_MD = "scoring_criteria.md"
_CTA_MD = "cta_examples.md"


class SkillKnowledgeError(RuntimeError):
    """既存スキル資産の読み込みに失敗した場合に送出される。"""


def _read_skill_file(filename: str) -> str:
    """指定ファイルを読み取り専用で読み込む。書き込みは一切行わない。"""
    path = os.path.join(SKILL_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError as exc:
        raise SkillKnowledgeError(
            f"既存スキル資産 {path} が見つかりません。"
            "~/.claude/skills/insta-food-buzz/ の内容やパスが変わっていないか確認してください。"
        ) from exc


def _strip_frontmatter(markdown_text: str) -> str:
    """先頭のYAMLフロントマター（--- ... ---）を除去した本文を返す。"""
    lines = markdown_text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[i + 1 :]).strip()
    return markdown_text.strip()


@dataclass(frozen=True)
class SkillKnowledge:
    """insta-food-buzzスキルから読み込んだ生成知見一式。"""

    persona_and_rules: str
    """SKILL.md本文（フロントマター除去済み）。ペルソナ・出力構成・品質のポイントを含む。"""

    scoring_criteria: str
    """scoring_criteria.md全文。五感描写・冒頭フック等、10項目120点満点のスコアリング基準。"""

    cta_format: str
    """cta_examples.md全文。投稿文作成後の次アクション提案フォーマット。"""


def load_skill_knowledge() -> SkillKnowledge:
    """3つの参照ファイルを読み取り専用で読み込み、SkillKnowledgeとして返す。

    Raises:
        SkillKnowledgeError: いずれかのファイルが見つからない場合。
    """
    return SkillKnowledge(
        persona_and_rules=_strip_frontmatter(_read_skill_file(_SKILL_MD)),
        scoring_criteria=_read_skill_file(_SCORING_MD),
        cta_format=_read_skill_file(_CTA_MD),
    )


def build_knowledge_digest(knowledge: SkillKnowledge) -> str:
    """Worker/Advisorへの生成・レビュープロンプトに埋め込む知見ダイジェストを組み立てる。

    insta-food-buzzスキルの資産を要約せず本文のまま引き渡すことで、
    「五感の具体描写」「冒頭フック」「ハッシュタグの大中小規模感」
    「自然なCTA」といった、既に検証済みの品質基準をInstagram/LINE/
    画像生成プロンプトいずれの生成でも核として踏襲させる。

    Args:
        knowledge: load_skill_knowledge() で取得した知見一式。

    Returns:
        Worker/Advisorのプロンプトに直接埋め込めるダイジェスト文字列。
    """
    return (
        "以下は、トリガー評価で正解率100%（正例10/10・負例10/10）を達成済みの"
        "既存Instagram投稿生成スキル「insta-food-buzz」が確立している生成ルールです。"
        "Instagram・LINE公式アカウント・画像生成AIプロンプトのいずれを生成する場合も、"
        "このペルソナ・品質基準を核として厳格に踏襲してください。\n\n"
        "==== 【ペルソナ・出力ルール（insta-food-buzz/SKILL.md）】 ====\n"
        f"{knowledge.persona_and_rules}\n\n"
        "==== 【品質スコアリング基準（scoring_criteria.md・合格ライン96点以上/120点）】 ====\n"
        f"{knowledge.scoring_criteria}\n\n"
        "==== 【CTA・次アクション提案フォーマット（cta_examples.md）】 ====\n"
        f"{knowledge.cta_format}\n"
    )
