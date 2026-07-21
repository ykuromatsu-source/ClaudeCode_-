"""Instagram Graph API（Meta Graph API）投稿専用モジュール（Phase 2-B）。

Meta Graph APIの2ステップ投稿フロー（コンテナ作成 → パブリッシュ）を実装する。

  - 静止画（フィード投稿）: ``POST /{ig-user-id}/media`` (image_url, caption)
    → ``POST /{ig-user-id}/media_publish``
  - リール動画（Reels）: ``POST /{ig-user-id}/media``
    (media_type=REELS, video_url, caption) → コンテナステータス確認（ポーリング）
    → ``POST /{ig-user-id}/media_publish``

安全設計:
  - ``dry_run=True`` を指定した場合、または ``INSTAGRAM_ACCESS_TOKEN`` が
    未設定の場合は、実際のHTTPリクエストを一切送信せず、送信予定のエンドポイント・
    ペイロードをターミナルへログ出力するだけの安全なモックモードで動作する。
  - アクセストークンはログ・例外メッセージのいずれにも出力しない
    （常にペイロードから除去してから表示する）。
  - ネットワークエラー・タイムアウトは ``InstagramAPIError`` にラップし、
    ``post_image`` / ``post_reels`` は例外を送出せず ``PublishResult(success=False)``
    として返す（呼び出し元のバッチ処理を1件の失敗で巻き込んで止めないため）。

このモジュール単体では ``.env`` を読み書きしない・実投稿を行わない
（``dry_run`` またはトークン未設定時は常に安全側）。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GRAPH_API_BASE = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v20.0"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_POLL_TIMEOUT_SECONDS = 60.0

_dotenv_loaded = False


class InstagramAPIError(RuntimeError):
    """Instagram Graph API呼び出し中の回復不能なエラー（ネットワーク・認証・タイムアウト等）。"""


def _load_dotenv_once() -> None:
    """ワークスペース直下の ``.env`` を読み込む（python-dotenv 未インストールでも継続）。

    ``override=False`` により、既にシェルで export 済みの環境変数は上書きしない。
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    try:
        from dotenv import load_dotenv

        env_path = os.path.join(WORKSPACE_ROOT, ".env")
        load_dotenv(env_path, override=False)
    except ImportError:
        pass  # python-dotenv 未インストールでも os.getenv のみで動作継続する
    _dotenv_loaded = True


@dataclass(frozen=True)
class InstagramConfig:
    """Instagram Graph API接続設定。"""

    account_id: Optional[str] = None
    access_token: Optional[str] = None
    api_version: str = DEFAULT_API_VERSION

    @property
    def is_configured(self) -> bool:
        """実API呼び出しに最低限必要な情報（アカウントID・アクセストークン）が揃っているか。"""
        return bool(self.account_id and self.access_token)


def load_instagram_config() -> InstagramConfig:
    """``.env``（未インストール時は環境変数のみ）からInstagram接続設定を読み込む。"""
    _load_dotenv_once()
    return InstagramConfig(
        account_id=os.getenv("INSTAGRAM_ACCOUNT_ID") or None,
        access_token=os.getenv("INSTAGRAM_ACCESS_TOKEN") or None,
        api_version=os.getenv("META_GRAPH_API_VERSION") or DEFAULT_API_VERSION,
    )


@dataclass
class PublishResult:
    """1件の投稿処理（モック含む）の結果。呼び出し側はこれを見るだけでよく、
    例外処理を書く必要はない（``post_image``/``post_reels`` が例外を投げないため）。
    """

    success: bool
    media_id: Optional[str] = None
    container_id: Optional[str] = None
    is_mock: bool = False
    message: str = ""
    request_log: List[Dict[str, Any]] = field(default_factory=list)
    """モード（mock/live）を問わず、送信したエンドポイント・ペイロードの記録
    （access_tokenは含まない）。検証・監査用に呼び出し元へ引き渡す。"""


class InstagramPoster:
    """Meta Graph APIへの2ステップ投稿（コンテナ作成→パブリッシュ）を担う。

    ``dry_run=True`` を明示指定するか、``.env``（または環境変数）に
    ``INSTAGRAM_ACCESS_TOKEN`` が設定されていない場合、``is_mock`` が
    自動的に True になり、実HTTPリクエストを一切送信しない。
    """

    def __init__(
        self,
        config: Optional[InstagramConfig] = None,
        dry_run: bool = False,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        poll_timeout: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    ) -> None:
        self.config = config or load_instagram_config()
        # モック判定: 明示的な dry_run 指定 or アクセストークン未設定（安全側フォールバック）
        self.is_mock = dry_run or not self.config.is_configured
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self._current_log: List[Dict[str, Any]] = []

    def _base_url(self) -> str:
        return f"{GRAPH_API_BASE}/{self.config.api_version}/{self.config.account_id}"

    @staticmethod
    def _redact(payload: Dict[str, Any]) -> Dict[str, Any]:
        """ログ・例外メッセージに出す前にアクセストークン等の秘密情報を除去する。"""
        return {k: v for k, v in payload.items() if k not in ("access_token",)}

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """1回のPOST呼び出し。モック時は送信せずログのみ出力し、決定的な擬似IDを返す。

        送信先エンドポイントとペイロード（access_token除去済み）を
        ``self._current_log`` へ記録し、呼び出し元の ``PublishResult.request_log``
        から検証できるようにする。
        """
        url = f"{self._base_url()}/{path}"
        safe_payload = self._redact(payload)

        if self.is_mock:
            print(f"  [MOCK POST] {url}")
            print(f"    payload: {json.dumps(safe_payload, ensure_ascii=False)}")
            fake_id = "mock_" + hashlib.sha1(
                json.dumps(safe_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:12]
            self._current_log.append(
                {"mode": "mock", "method": "POST", "url": url, "payload": safe_payload, "response": {"id": fake_id}}
            )
            return {"id": fake_id}

        try:
            import httpx
        except ImportError as exc:
            raise InstagramAPIError(
                "httpx がインストールされていません。実API呼び出しには `pip install httpx` が必要です"
                "（--dry-run を使えばインストール無しで検証できます）。"
            ) from exc

        try:
            response = httpx.post(
                url,
                data={**payload, "access_token": self.config.access_token},
                timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data = response.json()
            self._current_log.append(
                {"mode": "live", "method": "POST", "url": url, "payload": safe_payload, "response": data}
            )
            return data
        except httpx.TimeoutException as exc:
            raise InstagramAPIError(f"Instagram Graph APIへの接続がタイムアウトしました: {path}") from exc
        except httpx.HTTPStatusError as exc:
            raise InstagramAPIError(
                f"Instagram Graph APIがエラーを返しました: {path} (HTTP {exc.response.status_code})"
            ) from exc
        except httpx.HTTPError as exc:
            raise InstagramAPIError(f"Instagram Graph API呼び出しに失敗しました: {path}: {exc}") from exc

    def create_image_container(self, image_url: str, caption: str) -> str:
        """静止画投稿用のメディアコンテナを作成し、コンテナIDを返す。"""
        data = self._post("media", {"image_url": image_url, "caption": caption})
        return data["id"]

    def create_reels_container(self, video_url: str, caption: str) -> str:
        """Reels（動画）投稿用のメディアコンテナを作成し、コンテナIDを返す。"""
        data = self._post(
            "media",
            {"media_type": "REELS", "video_url": video_url, "caption": caption},
        )
        return data["id"]

    def _container_status(self, container_id: str) -> str:
        """コンテナの処理ステータス（status_code）を取得する。"""
        if self.is_mock:
            print(f"  [MOCK STATUS] container={container_id} -> FINISHED")
            return "FINISHED"

        import httpx  # is_mock=False時点で create_*_container が成功済み＝httpx利用可能

        url = f"{GRAPH_API_BASE}/{self.config.api_version}/{container_id}"
        try:
            response = httpx.get(
                url,
                params={"fields": "status_code", "access_token": self.config.access_token},
                timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return response.json().get("status_code", "UNKNOWN")
        except httpx.HTTPError as exc:
            raise InstagramAPIError(f"コンテナステータスの確認に失敗しました: {exc}") from exc

    def wait_for_container_ready(self, container_id: str) -> None:
        """Reelsのコンテナ処理完了（status_code=FINISHED）をポーリングで待つ。

        Raises:
            InstagramAPIError: 処理エラー（ERROR）、またはポーリングが
                ``poll_timeout`` 秒を超えてもFINISHEDにならなかった場合。
        """
        if self.is_mock:
            self._container_status(container_id)
            return

        elapsed = 0.0
        while elapsed < self.poll_timeout:
            status = self._container_status(container_id)
            if status == "FINISHED":
                return
            if status == "ERROR":
                raise InstagramAPIError(f"動画コンテナの処理でエラーが発生しました（container={container_id}）")
            time.sleep(self.poll_interval)
            elapsed += self.poll_interval

        raise InstagramAPIError(
            f"動画コンテナの処理がタイムアウトしました（container={container_id}, "
            f"timeout={self.poll_timeout}秒）。しばらく待ってから再実行してください。"
        )

    def publish_container(self, container_id: str) -> str:
        """コンテナIDを指定してパブリッシュし、メディアIDを返す。"""
        data = self._post("media_publish", {"creation_id": container_id})
        return data["id"]

    def post_image(self, image_url: str, caption: str) -> PublishResult:
        """静止画（フィード）を2ステップ（コンテナ作成→パブリッシュ）で投稿する。

        例外は送出せず、失敗時も ``PublishResult(success=False)`` として返す。
        """
        self._current_log = []
        try:
            container_id = self.create_image_container(image_url, caption)
            media_id = self.publish_container(container_id)
            return PublishResult(
                success=True,
                media_id=media_id,
                container_id=container_id,
                is_mock=self.is_mock,
                message="モック投稿が完了しました" if self.is_mock else "投稿が完了しました",
                request_log=self._current_log,
            )
        except InstagramAPIError as exc:
            return PublishResult(success=False, is_mock=self.is_mock, message=str(exc), request_log=self._current_log)

    def post_reels(self, video_url: str, caption: str) -> PublishResult:
        """Reels（動画）を3ステップ（コンテナ作成→ステータス確認→パブリッシュ）で投稿する。

        例外は送出せず、失敗時も ``PublishResult(success=False)`` として返す。
        """
        self._current_log = []
        try:
            container_id = self.create_reels_container(video_url, caption)
            self.wait_for_container_ready(container_id)
            media_id = self.publish_container(container_id)
            return PublishResult(
                success=True,
                media_id=media_id,
                container_id=container_id,
                is_mock=self.is_mock,
                message="モック投稿が完了しました" if self.is_mock else "投稿が完了しました",
                request_log=self._current_log,
            )
        except InstagramAPIError as exc:
            return PublishResult(success=False, is_mock=self.is_mock, message=str(exc), request_log=self._current_log)
