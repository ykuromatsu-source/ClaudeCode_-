"""Advisor-Executor 協調エージェントのコアロジック。

Anthropicの Advisor Tool アーキテクチャ思想に基づき、軽量な Worker モデル
（Sonnet 5想定）が実作業（計画立案・コンテンツ生成）を行い、要所でのみ
上位の Advisor モデル（Fable 5想定）に計画レビュー・品質レビューを仰ぐ。

Advisor自身は実作業を行わない。Workerの提出物を審査し、承認
（approved）か要修正（needs_revision）かを判定するだけの役割に限定する
ことで、上位モデルの呼び出しコストを最小限に抑える。

Advisor呼び出しは「①計画レビュー」「②品質レビュー」の最大2回に厳格に
制限する（AgentBudget）。品質レビューで要修正となった場合もAdvisorへの
再相談は行わず、Workerが指摘のみに基づいて自律的に修正する。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

import anthropic

WORKER_MODEL = "claude-sonnet-5"
ADVISOR_MODEL = "claude-fable-5"

DEFAULT_MAX_ADVISOR_CALLS = 2  # 計画レビュー + 品質レビューの合計上限
DEFAULT_MAX_ERROR_RETRIES = 1  # API呼び出し失敗時の追加リトライ上限


class AdvisorVerdict(str, Enum):
    """Advisorのレビュー結果の判定区分。"""

    APPROVED = "approved"
    NEEDS_REVISION = "needs_revision"


@dataclass
class AdvisorReview:
    """Advisorが返すレビュー結果。"""

    verdict: AdvisorVerdict
    feedback: str


@dataclass
class AgentBudget:
    """Advisor呼び出し回数・エラーリトライ回数の予算管理。

    呼び出しのたびに record_* でカウントし、can_* で上限到達を判定する。
    Orchestrator（AdvisorExecutorAgent）はこの予算を横断して共有し、
    パイプライン全体でAdvisor呼び出しが2回を超えないことを保証する。
    """

    max_advisor_calls: int = DEFAULT_MAX_ADVISOR_CALLS
    max_error_retries: int = DEFAULT_MAX_ERROR_RETRIES
    advisor_calls_used: int = 0
    error_retries_used: int = 0

    def can_call_advisor(self) -> bool:
        """Advisorをあと1回以上呼び出せるかを返す。"""
        return self.advisor_calls_used < self.max_advisor_calls

    def can_retry_on_error(self) -> bool:
        """エラー時の追加リトライが残っているかを返す。"""
        return self.error_retries_used < self.max_error_retries

    def record_advisor_call(self) -> None:
        """Advisor呼び出し1回分を予算から消費する。"""
        self.advisor_calls_used += 1

    def record_error_retry(self) -> None:
        """エラーリトライ1回分を予算から消費する。"""
        self.error_retries_used += 1


class AdvisorBudgetExceeded(RuntimeError):
    """Advisor呼び出し予算を使い切った状態でレビューを要求した場合に送出される。"""


class Advisor:
    """上位モデル（Fable 5想定）による計画・品質レビュー役。

    Advisorは自ら文章やコンテンツを生成しない。Workerの計画または
    成果物を読み、承認か要修正かをツール呼び出し（構造化出力）で
    返すレビュアーに徹する。
    """

    def __init__(self, client: anthropic.Anthropic, model: str = ADVISOR_MODEL) -> None:
        self._client = client
        self._model = model

    def review(
        self,
        *,
        role: str,
        context: str,
        subject: str,
        budget: AgentBudget,
    ) -> AdvisorReview:
        """計画または成果物をレビューする。

        Args:
            role: レビューの種別ラベル（例: "計画レビュー", "品質レビュー"）。
                プロンプト内でAdvisorに今回の審査観点を伝えるために使う。
            context: レビューの前提情報（店舗情報・ブランドガイドライン等）。
            subject: レビュー対象そのもの（計画テキストまたはドラフトのJSON文字列）。
            budget: 呼び出し予算。上限に達していれば AdvisorBudgetExceeded を送出する。

        Returns:
            AdvisorReview: 判定結果とフィードバック。

        Raises:
            AdvisorBudgetExceeded: budgetの残り呼び出し回数が0の場合。
        """
        if not budget.can_call_advisor():
            raise AdvisorBudgetExceeded(
                f"Advisor呼び出し予算（上限{budget.max_advisor_calls}回）を使い切ったため"
                f"「{role}」を実行できません。"
            )

        tool_schema = {
            "name": "submit_review",
            "description": "レビュー結果を構造化して提出する",
            "input_schema": {
                "type": "object",
                "properties": {
                    "verdict": {
                        "type": "string",
                        "enum": ["approved", "needs_revision"],
                        "description": "承認するか、修正が必要か",
                    },
                    "feedback": {
                        "type": "string",
                        "description": "承認理由、または修正すべき具体的な指摘（日本語）",
                    },
                },
                "required": ["verdict", "feedback"],
            },
        }

        response = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            tools=[tool_schema],
            tool_choice={"type": "tool", "name": "submit_review"},
            system=(
                "あなたは飲食店SNS運用を統括するシニアブランドディレクター（Advisor）です。"
                "実作業は行わず、Workerが提出した計画または成果物を厳しく審査してください。"
                f"今回の審査観点: {role}。"
            ),
            messages=[
                {
                    "role": "user",
                    "content": f"【前提情報】\n{context}\n\n【レビュー対象】\n{subject}",
                }
            ],
        )
        budget.record_advisor_call()

        tool_use = _extract_tool_use(response, "submit_review")
        verdict = AdvisorVerdict(tool_use.input["verdict"])
        feedback = tool_use.input["feedback"]
        return AdvisorReview(verdict=verdict, feedback=feedback)


class Worker:
    """実作業モデル（Sonnet 5想定）。計画立案とコンテンツ生成・修正を担う。"""

    def __init__(self, client: anthropic.Anthropic, model: str = WORKER_MODEL) -> None:
        self._client = client
        self._model = model

    def draft_plan(self, *, brief: str) -> str:
        """インプットから投稿計画（コンセプト・訴求ポイント・トーン）のドラフトを立てる。"""
        response = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=(
                "あなたは飲食店SNS運用のプランナー（Worker）です。"
                "与えられた店舗・商品情報から、投稿のコンセプト・訴求ポイント・トーンを"
                "簡潔な箇条書き計画（5項目程度）にまとめてください。"
            ),
            messages=[{"role": "user", "content": brief}],
        )
        return _extract_text(response)

    def generate_content(
        self, *, brief: str, plan: str, tool_schema: dict[str, Any]
    ) -> dict[str, Any]:
        """承認済み計画に基づき、構造化されたコンテンツドラフトを生成する。"""
        response = self._client.messages.create(
            model=self._model,
            max_tokens=2048,
            tools=[tool_schema],
            tool_choice={"type": "tool", "name": tool_schema["name"]},
            system=(
                "あなたは飲食店SNS運用のコンテンツライター（Worker）です。"
                "承認済みの計画に沿って、指定ツールのスキーマ通りにドラフトを作成してください。"
            ),
            messages=[
                {
                    "role": "user",
                    "content": f"【店舗情報】\n{brief}\n\n【承認済み計画】\n{plan}",
                }
            ],
        )
        tool_use = _extract_tool_use(response, tool_schema["name"])
        return tool_use.input

    def revise_content(
        self,
        *,
        previous_draft: dict[str, Any],
        feedback: str,
        tool_schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Advisorの指摘に基づき、ドラフトを過不足なく修正する。"""
        response = self._client.messages.create(
            model=self._model,
            max_tokens=2048,
            tools=[tool_schema],
            tool_choice={"type": "tool", "name": tool_schema["name"]},
            system=(
                "あなたはWorkerです。Advisorの指摘のみに基づき、"
                "指定ツールのスキーマを保ったままドラフトを修正してください。"
                "指摘のない箇所は変更しないこと。"
            ),
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"【修正前のドラフト】\n{json.dumps(previous_draft, ensure_ascii=False, indent=2)}\n\n"
                        f"【Advisorの指摘】\n{feedback}"
                    ),
                }
            ],
        )
        tool_use = _extract_tool_use(response, tool_schema["name"])
        return tool_use.input


@dataclass
class PipelineResult:
    """パイプライン全体の実行結果。"""

    plan: str
    plan_review: AdvisorReview
    draft: dict[str, Any]
    quality_review: Optional[AdvisorReview]
    final_content: dict[str, Any]
    was_revised: bool
    advisor_calls_used: int
    error_retries_used: int


class AdvisorExecutorAgent:
    """Advisor-Executor協調パイプラインの本体。

    フロー: Worker(計画立案) → Advisor(計画レビュー) → Worker(生成)
           → Advisor(品質レビュー) → 必要ならWorker(修正)

    Advisor呼び出しは budget により計画レビュー・品質レビューの
    最大2回に厳格に制限される。品質レビューが要修正でも、Advisorへの
    再相談は行わずWorkerが1回だけ自律修正して完結させる。
    """

    def __init__(
        self,
        worker: Worker,
        advisor: Advisor,
        budget: Optional[AgentBudget] = None,
    ) -> None:
        self._worker = worker
        self._advisor = advisor
        self._budget = budget or AgentBudget()

    def run(
        self,
        *,
        brief: str,
        brand_guideline: str,
        tool_schema: dict[str, Any],
    ) -> PipelineResult:
        """パイプラインを実行する。

        Args:
            brief: 店舗・商品情報（インプット）。
            brand_guideline: ブランドイメージ・規約（Advisorのレビュー基準）。
            tool_schema: Workerが生成する構造化コンテンツのJSON Schema
                （Anthropicツール定義形式。"name"キーを含むこと）。
        """
        # ① 計画立案 → 計画レビュー（Advisor呼び出し 1/2）
        plan = self._call_with_retry(lambda: self._worker.draft_plan(brief=brief))
        plan_review = self._advisor.review(
            role="計画レビュー",
            context=f"{brand_guideline}\n\n【店舗情報】\n{brief}",
            subject=plan,
            budget=self._budget,
        )
        if plan_review.verdict is AdvisorVerdict.NEEDS_REVISION:
            # Advisorへの再相談はせず、指摘のみでWorkerが計画を練り直す
            plan = self._call_with_retry(
                lambda: self._worker.draft_plan(
                    brief=f"{brief}\n\n【前回計画へのAdvisor指摘。必ず反映すること】\n{plan_review.feedback}"
                )
            )

        # ② ドラフト生成
        draft = self._call_with_retry(
            lambda: self._worker.generate_content(brief=brief, plan=plan, tool_schema=tool_schema)
        )

        # ③ 品質レビュー（Advisor呼び出し 2/2）→ 必要なら1回だけ自律修正
        quality_review: Optional[AdvisorReview] = None
        final_content = draft
        was_revised = False
        try:
            quality_review = self._advisor.review(
                role="品質レビュー",
                context=brand_guideline,
                subject=json.dumps(draft, ensure_ascii=False, indent=2),
                budget=self._budget,
            )
            if quality_review.verdict is AdvisorVerdict.NEEDS_REVISION:
                final_content = self._call_with_retry(
                    lambda: self._worker.revise_content(
                        previous_draft=draft,
                        feedback=quality_review.feedback,
                        tool_schema=tool_schema,
                    )
                )
                was_revised = True
        except AdvisorBudgetExceeded:
            # 品質レビュー分の予算が既に尽きている場合は初稿をそのまま最終版とする
            pass

        return PipelineResult(
            plan=plan,
            plan_review=plan_review,
            draft=draft,
            quality_review=quality_review,
            final_content=final_content,
            was_revised=was_revised,
            advisor_calls_used=self._budget.advisor_calls_used,
            error_retries_used=self._budget.error_retries_used,
        )

    def _call_with_retry(self, fn: Callable[[], Any]) -> Any:
        """Worker呼び出しをラップし、失敗時は予算内で1回だけ再試行する。"""
        try:
            return fn()
        except AdvisorBudgetExceeded:
            raise
        except Exception:
            if not self._budget.can_retry_on_error():
                raise
            self._budget.record_error_retry()
            return fn()


def _extract_text(response: "anthropic.types.Message") -> str:
    """Anthropicレスポンスからテキストブロックを連結して取り出す。"""
    return "".join(block.text for block in response.content if block.type == "text")


def _extract_tool_use(response: "anthropic.types.Message", expected_name: str) -> Any:
    """Anthropicレスポンスから指定ツール名のtool_useブロックを取り出す。"""
    for block in response.content:
        if block.type == "tool_use" and block.name == expected_name:
            return block
    raise ValueError(
        f"レスポンスに期待したツール呼び出し（{expected_name}）が含まれていません: {response.content!r}"
    )
