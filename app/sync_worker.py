"""Background sync workers for Nhanh conversations.

Two daemon threads:

- `LightSyncWorker` (default 10 minutes): cheap. Pulls only the top of the
  global conversation list and fetches messages for conversations whose
  `updatedAt` advanced since the last cache write. Runs SLA detection and the
  grading guard on each updated conv. Drives the dashboard's "real-time" feel.

- `HeavySyncWorker` (default 4 hours): authoritative. Full crawl per pageId
  (matching the pagination quirks of Nhanh's API) for the backfill window. Used
  for cold start and for catching anything the light sync missed.

Both write to the same SQLite cache via `cache_db`. Trigger overlap inside a
worker is rejected (a worker only allows one concurrent tick). The two workers
do not coordinate beyond the cache itself — SQLite UPSERT + the
`sla_violations` UNIQUE constraint keep state consistent.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from app import cache_db
from app.evaluator import evaluate_conversation, is_conversation_ready_for_grading
from app.nhanh_adapter import build_conversation
from app.nhanh_client import (
    NhanhClientError,
    NhanhConfig,
    build_service_url,
    extract_items,
    list_conversations as nhanh_list_conversations,
    list_messages as nhanh_list_messages,
    post_json,
)
from app.sla_monitor import detect_violations_for_conversation


DEFAULT_LIGHT_INTERVAL_S = 10 * 60
DEFAULT_HEAVY_INTERVAL_S = 4 * 3600
DEFAULT_BACKFILL_DAYS = 7
DEFAULT_INTER_CALL_DELAY = 0.2


def _normalize_message_for_db(item: dict[str, Any], page_id: str, customer_id: str | None) -> dict[str, Any]:
    """Mirror `nhanh_adapter.map_message` for the message rows we persist. Keeps
    raw_json intact but tags sender_type so SLA/state queries don't re-derive
    it from the raw payload every time."""
    sender_id = str(item.get("senderId") or "")
    if customer_id and sender_id == str(customer_id):
        sender_type = "customer"
    else:
        sender_type = "employee"
    enriched = dict(item)
    enriched["senderType"] = sender_type
    return enriched


def _is_system_notification(item: dict[str, Any]) -> bool:
    """TikTok/Facebook system events (read receipt, typing indicator) arrive as
    messages with empty text and an attachment of type='notification'. Treat
    them as filler so they don't poison rules that inspect the last real
    message (e.g. sop_closing) or the last-sender heuristic."""
    text = str(item.get("message") or item.get("content") or item.get("text") or "").strip()
    if text:
        return False
    attachments = item.get("attachments") or []
    if not isinstance(attachments, list) or not attachments:
        return False
    return all(
        isinstance(att, dict) and str(att.get("type") or "").lower() == "notification"
        for att in attachments
    )


def _last_message_from_messages(messages: list[dict[str, Any]]) -> tuple[int | None, str | None]:
    """Pick last_message_at / last_message_sender from message list. Expects
    each dict to carry `created_at` (unix) and `sender_type`."""
    candidates = [m for m in messages if int(m.get("created_at") or 0) > 0]
    if not candidates:
        return None, None
    latest = max(candidates, key=lambda m: int(m.get("created_at") or 0))
    return int(latest["created_at"]), str(latest.get("sender_type") or "")


class _WorkerBase:
    """Shared lifecycle, status, and per-conv processing."""

    name: str = "worker"

    def __init__(
        self,
        config: NhanhConfig,
        ruleset: Any,
        db_path: Path,
        ruleset_version: str,
        interval_s: int,
        inter_call_delay_s: float = DEFAULT_INTER_CALL_DELAY,
        clock: Any = time.time,
        sleeper: Any = time.sleep,
    ) -> None:
        self.config = config
        self.ruleset = ruleset
        self.db_path = db_path
        self.ruleset_version = ruleset_version
        self.interval_s = interval_s
        self.inter_call_delay_s = inter_call_delay_s
        self._clock = clock
        self._sleep = sleeper
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._tick_lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name=f"nhanh-{self.name}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def trigger_now(self) -> bool:
        if not self._tick_lock.acquire(blocking=False):
            return False
        self._tick_lock.release()
        threading.Thread(
            target=self._tick, name=f"nhanh-{self.name}-trigger", daemon=True,
        ).start()
        return True

    # ---------- status helpers ----------

    def _set_state(self, key: str, value: str | None) -> None:
        cache_db.set_sync_state(self.db_path, key, value)

    def _log_error(self, message: str) -> None:
        self._set_state(f"last_{self.name}_sync_error", message[:2000])
        self._set_state(f"last_{self.name}_sync_error_ts", str(int(self._clock())))

    def _clear_error(self) -> None:
        self._set_state(f"last_{self.name}_sync_error", None)
        self._set_state(f"last_{self.name}_sync_error_ts", None)

    def get_status(self) -> dict[str, Any]:
        state = cache_db.all_sync_state(self.db_path)

        def _f(key: str) -> float | None:
            v = state.get(key)
            try:
                return float(v) if v is not None else None
            except ValueError:
                return None

        return {
            "is_syncing": state.get(f"is_{self.name}_syncing") == "1",
            "last_sync_ts": _f(f"last_{self.name}_sync_ts"),
            "last_sync_duration_s": _f(f"last_{self.name}_sync_duration_s"),
            "last_error": state.get(f"last_{self.name}_sync_error"),
            "last_error_ts": _f(f"last_{self.name}_sync_error_ts"),
            "interval_s": self.interval_s,
            "ruleset_version": self.ruleset_version,
            "conversation_count": cache_db.conversation_count(self.db_path),
        }

    # ---------- core ----------

    def _loop(self) -> None:
        # First-run kick: heavy worker backfills immediately; light worker
        # waits for first interval to give heavy a head start.
        if self._should_run_immediately():
            self._tick()
        while not self._stop_event.is_set():
            if self._stop_event.wait(timeout=self.interval_s):
                break
            self._tick()

    def _should_run_immediately(self) -> bool:
        return False

    def _tick(self) -> None:
        raise NotImplementedError

    def _process_conversation(self, conv_id: str, summary_item: dict[str, Any]) -> None:
        """Fetch messages, persist them, run SLA detection, optionally grade."""
        if not conv_id:
            return
        try:
            messages_payload = nhanh_list_messages(
                self.config, conversation_id=conv_id, size=50,
            )
        except NhanhClientError as exc:
            self._log_error(f"list_messages {conv_id}: {exc}")
            return

        raw_items = extract_items(messages_payload, "messages", "items")
        page_id = str(summary_item.get("pageId") or "")
        customer_id = str(summary_item.get("pageUserId") or "")
        enriched_items = [_normalize_message_for_db(it, page_id, customer_id) for it in raw_items]
        cache_db.upsert_messages(self.db_path, conv_id, enriched_items)

        # Strip system notifications (read receipts, typing indicators) before
        # any logic that consults the "last message" — they confuse closing/
        # SLA/grading rules that scan messages[-1] expecting a real message.
        real_items = [it for it in enriched_items if not _is_system_notification(it)]

        sender_tagged_messages = [
            {
                "created_at": int(it.get("createdAt") or 0),
                "sender_type": it.get("senderType") or "",
                "message_id": str(it.get("id") or it.get("messageId") or ""),
            }
            for it in real_items
        ]
        last_ts, last_sender = _last_message_from_messages(sender_tagged_messages)

        cache_db.upsert_conversation(
            self.db_path,
            summary_item,
            sync_source=self.name,
            last_message_at=last_ts,
            last_message_sender=last_sender,
        )

        if sender_tagged_messages:
            try:
                detect_violations_for_conversation(
                    self.db_path, conv_id, sender_tagged_messages, now=int(self._clock()),
                )
            except Exception as exc:
                self._log_error(f"sla {conv_id}: {exc}")

        # Grading guard: only evaluate if idle ≥ threshold AND last from customer.
        if not real_items:
            return
        try:
            # Rebuild payload with notifications stripped so the adapter and
            # rule engine never see them.
            clean_payload = dict(messages_payload)
            clean_payload["data"] = [it for it in raw_items if not _is_system_notification(it)]
            conversation = build_conversation(conv_id, clean_payload, summary_item=summary_item)
        except Exception as exc:
            self._log_error(f"build_conversation {conv_id}: {exc}")
            return
        if not is_conversation_ready_for_grading(conversation, now=self._clock()):
            return
        existing = cache_db.get_evaluation(self.db_path, conv_id, self.ruleset_version)
        if existing is not None:
            return  # already graded with current ruleset
        try:
            result = evaluate_conversation(conversation, self.ruleset).to_dict()
        except Exception as exc:
            self._log_error(f"evaluate {conv_id}: {exc}")
            return
        cache_db.upsert_evaluation(self.db_path, conv_id, self.ruleset_version, result)


class LightSyncWorker(_WorkerBase):
    """10-minute cadence. One `list_conversations` call + targeted message
    fetches for conversations whose updatedAt advanced."""

    name = "light"

    def __init__(self, *args: Any, page_size: int = 50, **kwargs: Any) -> None:
        kwargs.setdefault("interval_s", DEFAULT_LIGHT_INTERVAL_S)
        super().__init__(*args, **kwargs)
        self.page_size = page_size

    def _tick(self) -> None:
        if not self._tick_lock.acquire(blocking=False):
            return
        started = self._clock()
        self._set_state("is_light_syncing", "1")
        list_call_ok = False
        try:
            payload = nhanh_list_conversations(
                self.config, size=self.page_size, conversation_type=2,
            )
            list_call_ok = True
            items = extract_items(payload, "conversations", "conversation", "items")

            for summary in items:
                if self._stop_event.is_set():
                    break
                conv_id = str(summary.get("id") or summary.get("conversationId") or "")
                if not conv_id:
                    continue
                cached_row = cache_db.get_conversation_row(self.db_path, conv_id)
                nhanh_updated = int(summary.get("updatedAt") or 0)
                cached_updated = int((cached_row or {}).get("updated_at") or 0)
                if cached_row is None or nhanh_updated > cached_updated:
                    self._process_conversation(conv_id, summary)
                    if self.inter_call_delay_s > 0:
                        self._sleep(self.inter_call_delay_s)
            duration = self._clock() - started
            self._set_state("last_light_sync_ts", str(int(self._clock())))
            self._set_state("last_light_sync_duration_s", f"{duration:.2f}")
            # The top-level call succeeded; per-conv failures are logged but
            # shouldn't keep the banner red on the next page load.
            if list_call_ok:
                self._clear_error()
        except NhanhClientError as exc:
            self._log_error(str(exc))
        except Exception as exc:
            self._log_error(f"light tick unexpected: {exc}\n{traceback.format_exc()}")
        finally:
            self._set_state("is_light_syncing", "0")
            self._tick_lock.release()


class HeavySyncWorker(_WorkerBase):
    """4-hour cadence. Full per-pageId crawl over the backfill window."""

    name = "heavy"

    def __init__(
        self,
        *args: Any,
        backfill_days: int = DEFAULT_BACKFILL_DAYS,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("interval_s", DEFAULT_HEAVY_INTERVAL_S)
        super().__init__(*args, **kwargs)
        self.backfill_days = backfill_days

    def _should_run_immediately(self) -> bool:
        # Run immediately when the DB is empty (cold start).
        return cache_db.conversation_count(self.db_path) == 0

    def _tick(self) -> None:
        if not self._tick_lock.acquire(blocking=False):
            return
        started = self._clock()
        self._set_state("is_heavy_syncing", "1")
        discover_ok = False
        try:
            cutoff_ts = int(started) - self.backfill_days * 86400
            page_ids = self._discover_page_ids()
            discover_ok = True

            candidates: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            for page_id in page_ids:
                for item in self._iter_conversations_for_page(page_id, cutoff_ts):
                    iid = str(item.get("id") or "")
                    if iid and iid not in seen_ids:
                        seen_ids.add(iid)
                        candidates.append(item)

            total = len(candidates)
            self._update_progress(0, total)

            for index, summary in enumerate(candidates, start=1):
                if self._stop_event.is_set():
                    break
                conv_id = str(summary.get("id") or "")
                self._process_conversation(conv_id, summary)
                self._update_progress(index, total)
                if self.inter_call_delay_s > 0:
                    self._sleep(self.inter_call_delay_s)

            duration = self._clock() - started
            self._set_state("last_heavy_sync_ts", str(int(self._clock())))
            self._set_state("last_heavy_sync_duration_s", f"{duration:.2f}")
            if discover_ok:
                self._clear_error()
        except NhanhClientError as exc:
            self._log_error(str(exc))
        except Exception as exc:
            self._log_error(f"heavy tick unexpected: {exc}\n{traceback.format_exc()}")
        finally:
            self._set_state("is_heavy_syncing", "0")
            self._set_state("sync_progress", None)
            self._tick_lock.release()

    def is_first_run(self) -> bool:
        return cache_db.conversation_count(self.db_path) == 0 and (
            cache_db.get_sync_state(self.db_path, "last_heavy_sync_ts") is None
        )

    def _update_progress(self, processed: int, total: int) -> None:
        cache_db.set_sync_state(
            self.db_path,
            "sync_progress",
            json.dumps({"processed": processed, "total": total}),
        )

    def _discover_page_ids(self) -> list[str]:
        cached = cache_db.get_sync_state(self.db_path, "discovered_page_ids_json")
        cached_ts = cache_db.get_sync_state(self.db_path, "discovered_page_ids_ts")
        if cached and cached_ts:
            try:
                if int(self._clock()) - int(cached_ts) < 86400:
                    return list(json.loads(cached))
            except (ValueError, json.JSONDecodeError):
                pass

        url = build_service_url(self.config, "conversation/list")
        page_ids: set[str] = set()
        cursor: str | None = None
        for _ in range(4):
            payload: dict[str, Any] = {
                "filters": {"type": 2},
                "paginator": {"size": 50, "sort": {"updatedAt": "desc"}},
            }
            if cursor:
                payload["paginator"]["next"] = cursor
            response = post_json(url, payload, self.config.access_token, verify_ssl=self.config.verify_ssl)
            batch = response.get("data") or []
            if not batch:
                break
            for item in batch:
                pid = str(item.get("pageId") or "")
                if pid:
                    page_ids.add(pid)
            cursor = (response.get("paginator") or {}).get("next")
            if not cursor:
                break
            if self.inter_call_delay_s > 0:
                self._sleep(self.inter_call_delay_s)

        ordered = sorted(page_ids)
        cache_db.set_sync_state(self.db_path, "discovered_page_ids_json", json.dumps(ordered))
        cache_db.set_sync_state(self.db_path, "discovered_page_ids_ts", str(int(self._clock())))
        return ordered

    def _iter_conversations_for_page(self, page_id: str, cutoff_ts: int):
        """Iterate all conversations on `page_id` that are newer than cutoff_ts.

        Two Nhanh quirks shape this loop:
        1. `sort=updatedAt desc` is broken — each page returns a mix of new
           and old items, so we cannot early-stop on hitting a single old
           item. Items < cutoff are silently filtered; pagination continues.
        2. Deep pagination occasionally returns 504 in the middle of a crawl
           (verified for TikTok: pages 4 / 14 / 15 of a 30-page inbox). We
           retry the *same* page with exponential backoff before giving up,
           since the cursor is opaque — we cannot skip ahead. Only after
           several consecutive failures do we abandon this pageId.
        """
        url = build_service_url(self.config, "conversation/list")
        cursor: str | None = None
        retry_delays = (5, 15, 30)  # seconds between retries of the same page

        for page_idx in range(60):  # safety cap: 60 * 50 = 3000 items per pageId
            if self._stop_event.is_set():
                return
            payload: dict[str, Any] = {
                "filters": {"type": 2, "pageIds": [page_id]},
                "paginator": {"size": 50, "sort": {"updatedAt": "desc"}},
            }
            if cursor:
                payload["paginator"]["next"] = cursor

            response = None
            last_err: NhanhClientError | None = None
            attempts = (0,) + retry_delays
            for attempt_idx, delay in enumerate(attempts):
                if delay and not self._stop_event.is_set():
                    self._sleep(delay)
                try:
                    response = post_json(
                        url, payload, self.config.access_token, verify_ssl=self.config.verify_ssl,
                    )
                    last_err = None
                    break
                except NhanhClientError as exc:
                    last_err = exc
                    # Only retry transient timeouts / 5xx. Non-transient (401/
                    # 403/4xx) should fail fast — post_json's outer retry has
                    # already given them three chances at network level.
                    msg = str(exc).lower()
                    transient = (
                        "timeout" in msg or "timed out" in msg
                        or "5" in msg.split("http", 1)[-1][:4]  # crude 5xx detector
                    )
                    if not transient or attempt_idx == len(attempts) - 1:
                        break

            if response is None:
                # All retries exhausted — log and abandon this pageId. Other
                # pageIds in the outer loop continue normally.
                self._log_error(
                    f"discover page {page_idx} of {page_id} (after {len(attempts)} tries): {last_err}"
                )
                return

            batch = response.get("data") or []
            if not batch:
                return
            for item in batch:
                updated_at = int(item.get("updatedAt") or 0)
                if updated_at >= cutoff_ts:
                    yield item
            cursor = (response.get("paginator") or {}).get("next")
            if not cursor:
                return
            if self.inter_call_delay_s > 0:
                self._sleep(self.inter_call_delay_s)


# Backward-compat alias for tests that still reference SyncWorker.
SyncWorker = HeavySyncWorker
