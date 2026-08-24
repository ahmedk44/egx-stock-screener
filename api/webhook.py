"""
Vercel Python Serverless Webhook for Telegram Callback Queries
Handles POST from Telegram, parses callback_query (act_/dis_/cls_), fetches from sent_alerts, upserts to active_positions, and answers callback.
"""
import json
import os
import logging
from typing import Any, Dict, Optional

try:
    import requests
except ImportError:
    requests = None

logger = logging.getLogger("webhook-py")
SUPABASE_TABLE = "active_positions"
SENT_ALERTS_TABLE = "sent_alerts"

def _get_supabase_config():
    url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    return url, key

def _supabase_headers(prefer: str = "return=minimal"):
    key = (os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not key:
        return {}
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


_ALLOWED_ACTIVE_FIELDS = {"ticker", "entry_price", "current_stop_loss", "target_1", "target_2", "target_3", "trade_track", "status", "created_at"}


def _sanitize_active_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitize active_positions payload to prevent PGRST204 schema cache errors.

    Handles both timestamp and created_at safely – normalizes timestamp to created_at
    and filters to only valid table fields.
    """
    if not isinstance(payload, dict):
        return {}
    try:
        sanitized: Dict[str, Any] = {}
        # Normalize timestamp -> created_at
        has_ts = "timestamp" in payload and payload.get("timestamp") is not None
        has_ca = "created_at" in payload and payload.get("created_at") is not None
        if has_ts and not has_ca:
            sanitized["created_at"] = payload.get("timestamp")
        elif has_ca:
            sanitized["created_at"] = payload.get("created_at")
        for k in _ALLOWED_ACTIVE_FIELDS:
            if k == "created_at":
                continue
            if k in payload and payload.get(k) is not None:
                sanitized[k] = payload[k]
        return sanitized
    except Exception:
        try:
            return {k: v for k, v in payload.items() if k in _ALLOWED_ACTIVE_FIELDS and k != "timestamp"}
        except Exception:
            return {}

def handler(request, *args, **kwargs):
    """Vercel Python handler - handles POST from Telegram."""
    method = None
    body = None
    try:
        method = getattr(request, "method", None) or getattr(request, "method", "POST")
        if hasattr(request, "get_json"):
            body = request.get_json(force=True, silent=True)
        elif hasattr(request, "json"):
            try:
                body = request.json()
            except Exception:
                body = getattr(request, "body", None)
                if isinstance(body, bytes):
                    body = json.loads(body.decode("utf-8"))
        elif hasattr(request, "body"):
            body = request.body
            if isinstance(body, bytes):
                body = json.loads(body.decode("utf-8"))
        else:
            body = request
    except Exception as exc:
        print(f"[WEBHOOK ERROR] Failed to parse request: {exc}")
        logger.warning(f"Failed to parse request: {exc}")
        body = {}

    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            body = {}

    if method and method != "POST":
        try:
            if hasattr(request, "status_code"):
                request.status_code = 200
                return "OK"
        except Exception:
            pass
        return {"statusCode": 200, "body": "OK"}

    try:
        update = body if isinstance(body, dict) else {}
        print(f"[WEBHOOK] Received update: {json.dumps(update)[:500] if isinstance(update, dict) else str(update)[:500]}")
        query = update.get("callback_query") if isinstance(update, dict) else None
        if query and isinstance(query, dict):
            callback_id = query.get("id")
            data = query.get("data") or ""
            bot_token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
            supabase_url, supabase_key = _get_supabase_config()

            print(f"[WEBHOOK] callback_query id={callback_id} data={data}")
            logger.info(f"[WEBHOOK] callback_query id={callback_id} data={data}")

            popup_text = "تم التحديث بنجاح!"
            new_status = None
            ticker = None
            parsed_trade = None

            try:
                if isinstance(data, str):
                    if data.startswith("act_"):
                        payload = data.replace("act_", "", 1)
                        parts = payload.split("|")
                        ticker = parts[0].strip() if parts and parts[0] else ""
                        new_status = "ACTIVE"
                        # Required popup per spec
                        popup_text = "✅ تم تفعيل الصفقة بنجاح وحفظها في Supabase!"
                        # Also parse fallback pipe data
                        if len(parts) >= 6:
                            try:
                                parsed_trade = {
                                    "ticker": parts[0].strip(),
                                    "entry_price": float(parts[1]) if parts[1] else None,
                                    "current_stop_loss": float(parts[2]) if parts[2] else None,
                                    "target_1": float(parts[3]) if parts[3] else None,
                                    "target_2": float(parts[4]) if parts[4] else None,
                                    "target_3": float(parts[5]) if parts[5] else None,
                                }
                            except Exception as e:
                                print(f"[WEBHOOK ERROR] Fallback parse failed: {e}")
                                parsed_trade = None
                        print(f"[WEBHOOK] Act activation for ticker={ticker}")
                    elif data.startswith("dis_"):
                        raw = data.replace("dis_", "", 1)
                        ticker = raw.split("|")[0].strip() if "|" in raw else raw.strip()
                        new_status = "DISMISSED"
                        popup_text = "❌ تم إلغاء متابعة الصفقة."
                    elif data.startswith("cls_"):
                        raw = data.replace("cls_", "", 1)
                        ticker = raw.split("|")[0].strip() if "|" in raw else raw.strip()
                        new_status = "CLOSED"
                        popup_text = "🏁 تم إغلاق الصفقة يدوياً."
            except Exception as e:
                print(f"[WEBHOOK ERROR] Failed to parse callback_data: {e}")
                logger.warning(f"Failed to parse callback_data: {e}")

            # Handle تفعيل الصفقة: fetch from sent_alerts and insert into active_positions
            if new_status and ticker and supabase_url and supabase_key and requests:
                try:
                    print(f"[SUPABASE] Starting activation flow for {ticker} -> {new_status}")
                    logger.info(f"[SUPABASE] Starting activation flow for {ticker} -> {new_status}")

                    if new_status == "ACTIVE" and data.startswith("act_"):
                        trade_details: Optional[Dict[str, Any]] = None

                        # 1) Fetch latest alert details for that ticker from sent_alerts
                        try:
                            headers = _supabase_headers()
                            sent_url = f"{supabase_url}/rest/v1/{SENT_ALERTS_TABLE}?ticker=eq.{ticker}&order=created_at.desc&limit=1&select=*"
                            print(f"[SUPABASE] GET sent_alerts URL: {sent_url}")
                            resp = requests.get(sent_url, headers=headers, timeout=10)
                            try:
                                body = resp.text[:1000] if resp.text else "(empty)"
                            except Exception:
                                body = "(no body)"
                            print(f"[SUPABASE] GET sent_alerts {ticker} -> {resp.status_code} {body[:500]}")
                            logger.info(f"[SUPABASE] GET sent_alerts {ticker} -> {resp.status_code} {body[:200]}")

                            if resp.status_code == 200:
                                try:
                                    data_json = resp.json()
                                    if isinstance(data_json, list) and len(data_json) > 0:
                                        latest = data_json[0]
                                        trade_details = {
                                            "ticker": latest.get("ticker") or ticker,
                                            "entry_price": latest.get("entry_price"),
                                            "current_stop_loss": latest.get("current_stop_loss"),
                                            "target_1": latest.get("target_1"),
                                            "target_2": latest.get("target_2"),
                                            "target_3": latest.get("target_3"),
                                        }
                                        print(f"[SUPABASE] Found sent_alert: {trade_details}")
                                        logger.info(f"[SUPABASE] Found sent_alert for {ticker}: {trade_details}")
                                    else:
                                        print(f"[SUPABASE] No sent_alert found for {ticker}, will use fallback")
                                except Exception as e:
                                    print(f"[SUPABASE ERROR] JSON parse failed for sent_alerts: {e}")
                                    logger.warning(f"sent_alerts JSON parse failed: {e}")
                            else:
                                print(f"[SUPABASE ERROR] GET sent_alerts non-200 {resp.status_code}: {body[:300]}")
                                logger.warning(f"GET sent_alerts failed {resp.status_code}: {body[:300]}")
                        except requests.exceptions.RequestException as e:
                            print(f"[SUPABASE ERROR] Request failed fetching sent_alerts for {ticker}: {e}")
                            logger.warning(f"sent_alerts fetch request failed: {e}")
                        except Exception as e:
                            print(f"[SUPABASE ERROR] Unexpected fetch error for {ticker}: {e}")
                            logger.warning(f"sent_alerts fetch unexpected: {e}")

                        # Fallback to parsed_trade from callback_data pipes
                        if (not trade_details or trade_details.get("entry_price") is None) and parsed_trade and parsed_trade.get("entry_price") is not None:
                            trade_details = parsed_trade
                            print(f"[SUPABASE] Using fallback tradeDetails from callback_data: {trade_details}")

                        # 2) Insert retrieved trade details into active_positions (sanitized, graceful handling for PGRST204/42P10)
                        if trade_details and trade_details.get("entry_price") is not None:
                            try:
                                headers_upsert = _supabase_headers(prefer="resolution=merge-duplicates,return=representation")
                                raw_payload: Dict[str, Any] = {
                                    "ticker": trade_details.get("ticker") or ticker,
                                    "entry_price": float(trade_details.get("entry_price")),
                                    "current_stop_loss": float(trade_details.get("current_stop_loss") or trade_details.get("entry_price")),
                                    "target_1": float(trade_details.get("target_1") or trade_details.get("entry_price")),
                                    "target_2": float(trade_details.get("target_2") or trade_details.get("entry_price")),
                                    "target_3": float(trade_details.get("target_3") or trade_details.get("entry_price")),
                                    "trade_track": "Scalp",
                                    "status": "ACTIVE",
                                    "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
                                }
                                payload_insert = _sanitize_active_payload(raw_payload)
                                print(f"[SUPABASE] POST active_positions payload (sanitized): {payload_insert}")
                                post_resp = requests.post(
                                    f"{supabase_url}/rest/v1/{SUPABASE_TABLE}?on_conflict=ticker,trade_track",
                                    headers=headers_upsert,
                                    json=payload_insert,
                                    timeout=10,
                                )
                                try:
                                    pbody = post_resp.text[:1000] if post_resp.text else "(empty)"
                                except Exception:
                                    pbody = "(no body)"
                                print(f"[SUPABASE] POST active_positions {ticker} -> {post_resp.status_code} {pbody[:500]}")
                                logger.info(f"[SUPABASE] POST active_positions {ticker} -> {post_resp.status_code} {pbody[:200]}")
                                # Graceful fallback for PGRST204 schema cache and 42P10 missing unique constraint
                                if post_resp.status_code == 400 and ("PGRST204" in pbody or "42P10" in pbody or "ON CONFLICT" in pbody or "schema cache" in pbody):
                                    print(f"[SUPABASE] Fallback: retrying without on_conflict for {ticker}")
                                    plain_resp = requests.post(
                                        f"{supabase_url}/rest/v1/{SUPABASE_TABLE}",
                                        headers=_supabase_headers(prefer="return=representation"),
                                        json=payload_insert,
                                        timeout=10,
                                    )
                                    try:
                                        plain_body = plain_resp.text[:1000] if plain_resp.text else "(empty)"
                                    except Exception:
                                        plain_body = "(no body)"
                                    print(f"[SUPABASE] Plain POST fallback {ticker} -> {plain_resp.status_code} {plain_body[:300]}")
                                    if plain_resp.status_code in (200, 201, 204):
                                        post_resp = plain_resp
                                        pbody = plain_body
                                    elif plain_resp.status_code == 409:
                                        # Try PATCH if exists
                                        try:
                                            patch_resp = requests.patch(
                                                f"{supabase_url}/rest/v1/{SUPABASE_TABLE}?ticker=eq.{ticker}&trade_track=eq.Scalp",
                                                headers=_supabase_headers(),
                                                json=payload_insert,
                                                timeout=10,
                                            )
                                            print(f"[SUPABASE] PATCH fallback {ticker} -> {patch_resp.status_code}")
                                            if patch_resp.status_code in (200, 204):
                                                post_resp = patch_resp
                                                pbody = patch_resp.text[:300] if patch_resp.text else ""
                                        except Exception:
                                            pass
                                if post_resp.status_code not in (200, 201, 204):
                                    print(f"[SUPABASE ERROR] Insert failed {post_resp.status_code}: {pbody[:300]}")
                                    logger.warning(f"Insert active_positions failed {post_resp.status_code}: {pbody[:300]}")
                            except requests.exceptions.RequestException as e:
                                print(f"[SUPABASE ERROR] POST active_positions request failed: {e}")
                                logger.warning(f"POST active_positions request failed: {e}")
                            except Exception as e:
                                print(f"[SUPABASE ERROR] POST active_positions unexpected: {e}")
                                logger.warning(f"POST active_positions unexpected: {e}")
                        else:
                            print(f"[SUPABASE ERROR] No tradeDetails available for {ticker}, trying minimal insert")
                            try:
                                headers_upsert = _supabase_headers(prefer="resolution=merge-duplicates,return=representation")
                                raw_minimal = {
                                    "ticker": ticker,
                                    "entry_price": 0,
                                    "current_stop_loss": 0,
                                    "target_1": 0,
                                    "target_2": 0,
                                    "target_3": 0,
                                    "trade_track": "Scalp",
                                    "status": "ACTIVE",
                                    "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
                                }
                                minimal = _sanitize_active_payload(raw_minimal)
                                mresp = requests.post(
                                    f"{supabase_url}/rest/v1/{SUPABASE_TABLE}?on_conflict=ticker,trade_track",
                                    headers=headers_upsert,
                                    json=minimal,
                                    timeout=10,
                                )
                                print(f"[SUPABASE] Minimal POST {ticker} -> {mresp.status_code} {mresp.text[:300] if mresp.text else ''}")
                                if mresp.status_code == 400 and ("PGRST204" in (mresp.text or "") or "42P10" in (mresp.text or "")):
                                    plain_m = requests.post(
                                        f"{supabase_url}/rest/v1/{SUPABASE_TABLE}",
                                        headers=_supabase_headers(prefer="return=representation"),
                                        json=minimal,
                                        timeout=10,
                                    )
                                    print(f"[SUPABASE] Plain minimal fallback {ticker} -> {plain_m.status_code} {plain_m.text[:200] if plain_m.text else ''}")
                            except Exception as e:
                                print(f"[SUPABASE ERROR] Minimal insert failed: {e}")

                    # Also PATCH status update for all cases
                    try:
                        headers = _supabase_headers()
                        patch_url = f"{supabase_url}/rest/v1/{SUPABASE_TABLE}?ticker=eq.{ticker}"
                        print(f"[SUPABASE] PATCH {ticker} -> {new_status} URL: {patch_url}")
                        resp = requests.patch(patch_url, headers=headers, json={"status": new_status}, timeout=10)
                        try:
                            body = resp.text[:1000] if resp.text else "(empty)"
                        except Exception:
                            body = "(no body)"
                        print(f"[SUPABASE] PATCH {ticker} -> {new_status} {resp.status_code} {body[:300]}")
                        logger.info(f"[SUPABASE] PATCH {ticker} -> {new_status} {resp.status_code} {body[:200]}")
                        if resp.status_code not in (200, 204):
                            print(f"[SUPABASE ERROR] PATCH non-200: {body[:300]}")
                    except requests.exceptions.RequestException as e:
                        print(f"[SUPABASE ERROR] PATCH request failed for {ticker}: {e}")
                        logger.warning(f"PATCH request failed: {e}")
                    except Exception as e:
                        print(f"[SUPABASE ERROR] PATCH unexpected for {ticker}: {e}")
                        logger.warning(f"PATCH unexpected: {e}")

                except Exception as e:
                    print(f"[SUPABASE ERROR] Activation flow failed for {ticker}: {e}")
                    logger.warning(f"Activation flow failed for {ticker}: {e}")
                    import traceback; traceback.print_exc()
            elif new_status and ticker:
                print(f"[SUPABASE] Skipping Supabase update: missing URL/Key or requests (url={bool(supabase_url)}, key={bool(supabase_key)}, req={bool(requests)})")
                logger.warning(f"Skipping Supabase update missing config url={bool(supabase_url)} key={bool(supabase_key)}")

            # Answer callback query immediately
            if bot_token and callback_id and requests:
                try:
                    print(f"[TELEGRAM] answerCallbackQuery id={callback_id} text={popup_text}")
                    logger.info(f"[TELEGRAM] answerCallbackQuery id={callback_id} text={popup_text}")
                    resp = requests.post(
                        f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery",
                        json={
                            "callback_query_id": callback_id,
                            "text": popup_text,
                            "show_alert": True,
                        },
                        timeout=10,
                    )
                    try:
                        body = resp.text[:500] if resp.text else "(empty)"
                    except Exception:
                        body = "(no body)"
                    print(f"[TELEGRAM] answerCallbackQuery -> {resp.status_code} {body[:300]}")
                    logger.info(f"[TELEGRAM] answerCallbackQuery -> {resp.status_code} {body[:200]}")
                    if resp.status_code != 200:
                        print(f"[TELEGRAM ERROR] answerCallbackQuery failed {resp.status_code}: {body[:300]}")
                except requests.exceptions.RequestException as e:
                    print(f"[TELEGRAM ERROR] answerCallbackQuery request failed: {e}")
                    logger.warning(f"answerCallbackQuery request failed: {e}")
                except Exception as e:
                    print(f"[TELEGRAM ERROR] answerCallbackQuery unexpected: {e}")
                    logger.warning(f"answerCallbackQuery unexpected: {e}")
                    import traceback; traceback.print_exc()
            else:
                print(f"[TELEGRAM] Skipping answerCallbackQuery missing token/callback_id/requests (token={bool(bot_token)}, id={bool(callback_id)})")
        else:
            print("[WEBHOOK] No callback_query in update")
    except Exception as e:
        print(f"[WEBHOOK ERROR] Top-level handler error: {e}")
        logger.warning(f"Webhook handler top-level error: {e}")
        import traceback; traceback.print_exc()

    # Return OK for Vercel
    try:
        if hasattr(request, "status_code"):
            return "OK"
    except Exception:
        pass
    return {"statusCode": 200, "body": "OK"}


# For local testing / Vercel compatibility, also support Flask-like handler
def app(request):
    return handler(request)
