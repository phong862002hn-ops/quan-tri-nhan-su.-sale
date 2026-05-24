from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import parse

import requests
from requests.adapters import HTTPAdapter


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = BASE_DIR / "data" / "nhanh_config.local.json"


class NhanhClientError(Exception):
    pass


@dataclass
class NhanhConfig:
    app_id: int
    business_id: int
    access_token: str
    secret_key: str = ""
    service: str = "vpage"
    verify_ssl: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NhanhConfig":
        return cls(
            app_id=int(data["app_id"]),
            business_id=int(data["business_id"]),
            access_token=str(data["access_token"]),
            secret_key=str(data.get("secret_key", "")),
            service=str(data.get("service", "vpage")),
            verify_ssl=bool(data.get("verify_ssl", True)),
        )


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> NhanhConfig:
    with Path(path).open("r", encoding="utf-8") as file:
        data = json.load(file)
    return NhanhConfig.from_dict(data)


def build_service_url(config: NhanhConfig, api_path: str) -> str:
    query = parse.urlencode({"appId": config.app_id, "businessId": config.business_id})
    return f"https://{config.service}.open.nhanh.vn/v3.0/{api_path}?{query}"


_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _session = session
    return _session


DEFAULT_TIMEOUT_S = 60
RETRY_DELAYS_S = (2, 5)  # transient timeouts → retry twice with backoff


def post_json(
    url: str,
    payload: dict[str, Any],
    access_token: str,
    verify_ssl: bool = True,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    headers = {
        "Authorization": access_token,
        "Content-Type": "application/json",
    }
    if not verify_ssl:
        print("[nhanh_client] WARNING: SSL verification disabled — request not protected against MITM.", file=sys.stderr)

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_transient: Exception | None = None
    # Initial attempt + retries on transient timeouts only. HTTP errors and
    # non-timeout connection errors fail fast — they won't get better by trying
    # again immediately.
    for attempt, delay in enumerate((0,) + RETRY_DELAYS_S):
        if delay:
            import time as _time
            _time.sleep(delay)
        try:
            response = _get_session().post(
                url, data=body, headers=headers, timeout=timeout_s, verify=verify_ssl,
            )
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            text = exc.response.text if exc.response is not None else ""
            raise NhanhClientError(f"Nhanh API HTTP {status}: {text}") from exc
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_transient = exc
            if attempt == len(RETRY_DELAYS_S):
                break
            continue
        except requests.RequestException as exc:
            raise NhanhClientError(f"Khong ket noi duoc Nhanh API: {exc}") from exc

    raise NhanhClientError(
        f"Nhanh API timeout sau {len(RETRY_DELAYS_S) + 1} lan thu: {last_transient}"
    ) from last_transient


def check_access_token(config: NhanhConfig) -> dict[str, Any]:
    if not config.secret_key:
        raise NhanhClientError("Config local chua co secret_key de check access token.")
    url = (
        "https://pos.open.nhanh.vn/v3.0/app/checkaccesstoken?"
        + parse.urlencode({"appId": config.app_id, "businessId": config.business_id})
    )
    return post_json(url, {"secretKey": config.secret_key}, config.access_token, verify_ssl=config.verify_ssl)


def list_conversations(
    config: NhanhConfig,
    size: int = 10,
    conversation_type: int | None = None,
    page_ids: list[str] | None = None,
) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if conversation_type is not None:
        filters["type"] = conversation_type
    if page_ids:
        filters["pageIds"] = page_ids
    payload = {
        "filters": filters,
        "paginator": {
            "size": size,
            "sort": {"updatedAt": "desc"},
        },
    }
    url = build_service_url(config, "conversation/list")
    return post_json(url, payload, config.access_token, verify_ssl=config.verify_ssl)


def list_messages(config: NhanhConfig, conversation_id: str, size: int = 30) -> dict[str, Any]:
    payload = {
        "filters": {"conversationId": conversation_id},
        "paginator": {
            "size": size,
            "sort": {"createdAt": "desc"},
        },
    }
    url = build_service_url(config, "conversation/messages")
    return post_json(url, payload, config.access_token, verify_ssl=config.verify_ssl)


def extract_items(payload: dict[str, Any], *candidate_keys: str) -> list[dict[str, Any]]:
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(payload.get("items"), list):
        return [item for item in payload["items"] if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in candidate_keys:
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def summarize_conversations(payload: dict[str, Any]) -> str:
    items = extract_items(payload, "conversations", "conversation", "items")
    if not items:
        return json.dumps(payload, ensure_ascii=False, indent=2)
    if not items:
        return "Khong co hoi thoai nao tra ve."
    lines = []
    for item in items:
        conversation_id = item.get("conversationId") or item.get("id") or "-"
        page_id = item.get("pageId") or "-"
        updated_at = item.get("updatedAt") or item.get("updatedTime") or "-"
        customer_name = item.get("customerName") or item.get("recipientName") or item.get("name") or "-"
        lines.append(
            f"- conversationId={conversation_id} | customer={customer_name} | pageId={page_id} | updatedAt={updated_at}"
        )
    return "\n".join(lines)


def summarize_messages(payload: dict[str, Any]) -> str:
    items = extract_items(payload, "messages", "items")
    if not items:
        return json.dumps(payload, ensure_ascii=False, indent=2)
    if not items:
        return "Hoi thoai nay chua co tin nhan nao tra ve."
    lines = []
    for item in items:
        message_id = item.get("messageId") or item.get("id") or "-"
        text = str(item.get("message") or item.get("content") or item.get("text") or "").strip()
        sender = item.get("senderName") or item.get("fromName") or item.get("senderId") or "-"
        created_at = item.get("createdAt") or "-"
        lines.append(f"- {created_at} | {sender} | {message_id} | {text[:120]}")
    return "\n".join(lines)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Local Nhanh API helper for Message QA.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to nhanh config JSON.")
    parser.add_argument("--insecure", action="store_true", help="Disable SSL certificate verification for local testing.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("check-token", help="Check current access token using app secret_key.")

    list_parser = subparsers.add_parser("list-conversations", help="Fetch latest Vpage conversations.")
    list_parser.add_argument("--size", type=int, default=10, help="Number of conversations to fetch.")
    list_parser.add_argument("--type", type=int, choices=[1, 2], help="1=comment, 2=message.")
    list_parser.add_argument("--page-id", action="append", dest="page_ids", help="Optional pageId filter.")
    list_parser.add_argument("--json", action="store_true", help="Print raw JSON.")

    message_parser = subparsers.add_parser("list-messages", help="Fetch messages for one conversation.")
    message_parser.add_argument("--conversation-id", required=True, help="Nhanh conversationId.")
    message_parser.add_argument("--size", type=int, default=30, help="Number of messages to fetch.")
    message_parser.add_argument("--json", action="store_true", help="Print raw JSON.")

    args = parser.parse_args()
    config = load_config(args.config)
    if args.insecure:
        config.verify_ssl = False

    try:
        if args.command == "check-token":
            payload = check_access_token(config)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        if args.command == "list-conversations":
            payload = list_conversations(
                config,
                size=args.size,
                conversation_type=args.type,
                page_ids=args.page_ids,
            )
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(summarize_conversations(payload))
            return

        if args.command == "list-messages":
            payload = list_messages(config, conversation_id=args.conversation_id, size=args.size)
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(summarize_messages(payload))
            return
    except NhanhClientError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
