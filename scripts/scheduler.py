"""投稿キュー定時自動配信スケジューラー（Phase 2-C・B機能）。

``data/sns_queue.json`` を定期的に監視し、``status: queued`` かつ
``scheduled_at`` が現在時刻を過ぎている投稿案を自動検知して、
``scripts/sns_orchestrator.post_due_items``（内部で ``instagram_poster.py`` の
``InstagramPoster`` を使用）経由で配信する。

2つの実行モードに対応する:

  - **単発チェックモード（``--run-once``）**: cron や macOSの ``launchd`` 等の
    外部スケジューラーから定期起動される想定。1回だけキューを点検して終了する。
  - **常駐ループモード（既定）**: スクリプト自身が ``--interval-minutes`` で
    指定した間隔（既定60分）でキューを繰り返し点検する。

実行例:
    python3 scripts/scheduler.py --run-once --dry-run
        # 1回だけ点検（モック・実HTTPリクエストなし）
    python3 scripts/scheduler.py --run-once
        # 1回だけ点検（実際にInstagramへ配信）
    python3 scripts/scheduler.py --interval-minutes 60
        # 60分間隔で常駐監視（実際に配信）
    python3 scripts/scheduler.py --interval-minutes 15 --dry-run
        # 15分間隔で常駐監視（モック）

設計方針:
  - 標準ライブラリ（``argparse``/``datetime``/``time``/``logging``/``signal``）
    のみで構築し、外部依存を増やさない。投稿処理そのものは
    ``sns_orchestrator.py`` / ``instagram_poster.py`` へ委譲する。
  - 1回の点検サイクルで例外が発生してもスケジューラー自体はクラッシュしない。
    エラーはログへ記録した上で次の点検サイクルへ継続する（常駐運用の堅牢性）。
  - Ctrl+C（SIGINT）・SIGTERM を受け取った場合、進行中の点検サイクルの区切りで
    安全に終了する（長い待機中でも数秒おきに終了要求をチェックする）。
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime
from typing import List, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_ROOT = os.path.dirname(SCRIPT_DIR)

if SCRIPT_DIR not in sys.path:
    # `python3 scripts/scheduler.py` 直接実行時は自動で追加されるが、
    # パッケージ経由（`import scripts.scheduler`）で読み込んだ場合でも
    # 同階層の sns_orchestrator.py を確実に import できるよう明示的に追加する。
    sys.path.insert(0, SCRIPT_DIR)

from sns_orchestrator import OUTPUT_DIR, ensure_dirs, post_due_items  # noqa: E402

DEFAULT_INTERVAL_MINUTES = 60.0
"""常駐ループモードの既定の点検間隔（分）。"""

LOG_PATH = os.path.join(OUTPUT_DIR, "scheduler.log")
"""スケジューラーのログファイル出力先（コンソールと二重出力）。"""

_SLEEP_STEP_SECONDS = 5.0
"""待機中に終了要求（Ctrl+C等）をチェックする間隔（秒）。長い間隔でも素早く終了できるようにする。"""

_shutdown_requested = False


def _request_shutdown(signum: int, frame: object) -> None:
    """SIGINT/SIGTERM受信時に呼ばれる。フラグを立てるだけで即座には終了しない
    （進行中の投稿処理を中途半端な状態で中断しないため）。
    """
    global _shutdown_requested
    _shutdown_requested = True


def _setup_logging() -> logging.Logger:
    """コンソール＋ ``data/output/scheduler.log`` の二重出力ロガーを構築する。

    ログファイルへ書き込めない環境（権限不足等）でも、コンソール出力のみで
    安全に継続する。
    """
    ensure_dirs()
    logger = logging.getLogger("sns_scheduler")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger  # 同一プロセス内での再セットアップ防止

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    try:
        file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:
        logger.warning("ログファイル %s へ書き込めないため、コンソール出力のみで継続します（%s）。", LOG_PATH, exc)

    return logger


def run_check_cycle(logger: logging.Logger, dry_run: bool) -> List[dict]:
    """1回分の点検サイクル: 配信予定時刻が到来した投稿案を検知して投稿する。

    例外はこの関数の中で必ず捕捉し、ログへ記録した上で空リストを返す
    （例外を送出しない＝呼び出し元のループ/単発実行を継続・正常終了させるため）。

    Returns:
        list[dict]: 配信対象になった各アイテムの処理結果サマリー（対象無しや
            エラー時は空リスト）。
    """
    try:
        now = datetime.now().astimezone()
        logger.info("キューを点検します（基準時刻: %s / dry_run=%s）", now.isoformat(timespec="seconds"), dry_run)
        results = post_due_items(dry_run=dry_run, now=now)

        if not results:
            logger.info("配信予定時刻が到来した項目はありませんでした。")
            return []

        success = sum(1 for r in results if r["success"])
        logger.info("配信処理が完了しました: 成功 %d / 全 %d 件", success, len(results))
        for r in results:
            mark = "OK" if r["success"] else "NG"
            mode = "mock" if r["is_mock"] else "live"
            logger.info(
                "  [%s][%s] %s «%s» (%s) — %s", mark, mode, r["id"], r["theme"], r["visual_type"], r["message"]
            )
        return results

    except Exception:  # noqa: BLE001 - 1サイクルの例外でスケジューラー全体を落とさないための最終防波堤
        logger.exception("点検サイクル中に予期しないエラーが発生しました。次回のサイクルへ継続します。")
        return []


def _sleep_interruptible(total_seconds: float) -> None:
    """終了要求（Ctrl+C等）を ``_SLEEP_STEP_SECONDS`` 秒おきにチェックしながら待機する。

    ``time.sleep(total_seconds)`` を単純に呼ぶと、長い間隔（例: 60分）待機中に
    Ctrl+Cを押しても最大60分間終了できなくなるため、細切れに待機する。
    """
    elapsed = 0.0
    while elapsed < total_seconds and not _shutdown_requested:
        time.sleep(min(_SLEEP_STEP_SECONDS, total_seconds - elapsed))
        elapsed += _SLEEP_STEP_SECONDS


def run_loop(logger: logging.Logger, interval_minutes: float, dry_run: bool) -> None:
    """常駐ループモード本体。``interval_minutes`` 間隔で点検サイクルを繰り返す。"""
    interval_seconds = max(interval_minutes, 0.1) * 60.0
    logger.info(
        "常駐監視モードを開始します（間隔: %.1f分 / dry_run=%s）。Ctrl+Cで終了します。",
        interval_minutes,
        dry_run,
    )

    signal.signal(signal.SIGINT, _request_shutdown)
    try:
        signal.signal(signal.SIGTERM, _request_shutdown)
    except (ValueError, AttributeError):
        pass  # SIGTERM未対応環境（一部Windows等）でもSIGINTのみで継続動作する

    while not _shutdown_requested:
        run_check_cycle(logger, dry_run)
        if _shutdown_requested:
            break
        logger.info("次回点検まで %.1f分 待機します。", interval_minutes)
        _sleep_interruptible(interval_seconds)

    logger.info("終了要求を受け取りました。スケジューラーを終了します。")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scheduler.py",
        description=(
            "data/sns_queue.json を定期監視し、配信予定時刻(scheduled_at)が到来した "
            "投稿案をInstagramへ自動配信するスケジューラー。"
        ),
    )
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="1回だけキューを点検して終了する（cron/launchd等からの定期起動向け）",
    )
    parser.add_argument(
        "--interval-minutes",
        type=float,
        default=DEFAULT_INTERVAL_MINUTES,
        help=f"常駐監視モードでの点検間隔（分、既定 {DEFAULT_INTERVAL_MINUTES:.0f}分）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="実HTTPリクエストを送信せず、モックで点検・配信をシミュレーションする",
    )
    args = parser.parse_args(argv)

    logger = _setup_logging()

    if args.run_once:
        run_check_cycle(logger, args.dry_run)
        return 0

    try:
        run_loop(logger, args.interval_minutes, args.dry_run)
    except Exception:  # noqa: BLE001 - ループ制御自体の致命的エラーのみここに到達する
        logger.exception("スケジューラーが致命的なエラーで停止しました。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
