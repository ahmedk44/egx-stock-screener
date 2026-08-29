import json
import os
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
try:
    import requests
    print(f"[IMPORT] requests available version={getattr(requests, '__version__', 'unknown')}")
except ImportError as _req_e:
    print(f"[IMPORT][ERROR] requests import failed: {_req_e}")
    import traceback
    traceback.print_exc()
    requests = None  # type: ignore[assignment]
from http.server import BaseHTTPRequestHandler
# Fallback HTTP client using urllib if requests is None (Vercel pip install may fail)
if 'requests' not in globals() or globals().get('requests') is None:
    print("[IMPORT] requests is None, installing urllib fallback for HTTP")
    try:
        import urllib.request
        import urllib.error
        import urllib.parse

        class _UrllibResponse:
            def __init__(self, status, body, headers=None):
                self.status_code = status
                self.text = body
                self.headers = headers or {}
                self._body = body
            def json(self):
                import json as _j
                return _j.loads(self.text) if self.text else {}

        class _UrllibRequestsFallback:
            @staticmethod
            def _do(method, url, headers=None, json=None, timeout=10):
                import json as _j
                data = None
                hdrs = dict(headers or {})
                if json is not None:
                    data = _j.dumps(json).encode('utf-8')
                    hdrs.setdefault("Content-Type", "application/json")
                req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        body = resp.read().decode('utf-8', errors='replace')
                        return _UrllibResponse(resp.status, body, dict(resp.headers))
                except urllib.error.HTTPError as e:
                    try:
                        body = e.read().decode('utf-8', errors='replace')
                    except:
                        body = str(e)
                    return _UrllibResponse(e.code, body, dict(e.headers) if hasattr(e, 'headers') else {})
                except Exception as e:
                    return _UrllibResponse(0, str(e), {})

            @staticmethod
            def get(url, headers=None, timeout=10, **kwargs):
                return _UrllibRequestsFallback._do("GET", url, headers=headers, timeout=timeout)

            @staticmethod
            def post(url, headers=None, json=None, timeout=10, **kwargs):
                return _UrllibRequestsFallback._do("POST", url, headers=headers, json=json, timeout=timeout)

        requests = _UrllibRequestsFallback()  # type: ignore
        print("[IMPORT] urllib fallback installed as requests")
        # Also provide exceptions for compatibility
        class _ReqExc(Exception):
            pass
        requests.exceptions = type('obj', (), {'RequestException': _ReqExc})  # type: ignore
    except Exception as _url_e:
        print(f"[IMPORT][ERROR] urllib fallback failed: {_url_e}")
        import traceback
        traceback.print_exc()

try:
    logger = logging.getLogger("webhook-py")
    # === Supabase Table Routing (Purged Legacy: scanner pipeline now uses trade_signals + user_portfolio exclusively) ===
    TRADE_SIGNALS_TABLE = "trade_signals"  # Read-only source for trade specs (replaces sent_alerts)
    USER_PORTFOLIO_TABLE = "user_portfolio"  # Write-only target for user joins (exclusive)
    JOIN_CALLBACK_PREFIX = "join_trade"
    # Deprecated legacy tables - writes disabled, kept as aliases for audit logging only
    LEGACY_ACTIVE_POSITIONS_TABLE = "active_positions"  # DEPRECATED: no longer written
    LEGACY_SENT_ALERTS_TABLE = "sent_alerts"  # DEPRECATED: replaced by trade_signals
    # Backward-compat aliases (do not use for new writes)
    SUPABASE_TABLE = LEGACY_ACTIVE_POSITIONS_TABLE
    SENT_ALERTS_TABLE = LEGACY_SENT_ALERTS_TABLE

    TELEGRAM_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
    TELEGRAM_ANSWER_URL = "https://api.telegram.org/bot{token}/answerCallbackQuery"

    # Compact Shariah map (kept self-contained; serverless cannot import main.py).
    _SHARIAH_COMPLIANT_BASE = {
        "ABUK", "AMOC", "SWDY", "TMGH", "HELI", "ORAS", "EFIH", "ADIB", "FAIT",
        "SAUD", "ETEL", "FWRY", "JUFO", "EFID", "ISPH", "SKPC", "OLFI", "ORWE",
    }
    _SHARIAH_NON_COMPLIANT_BASE = {"COMI", "EAST"}

    _TRACK_LABELS: Dict[str, str] = {
        "scalp": "⚡ مضاربة لحظية (Scalp)",
        "scalping": "⚡ مضاربة لحظية (Scalp)",
        "swing": "📈 تداول سوينغ (Swing)",
        "investment": "🏛️ استثمار طويل (Invest)",
        "invest": "🏛️ استثمار طويل (Invest)",
    }


    def _shariah_flag(symbol: str) -> str:
        """Compact compliance badge for a ticker (⚠️ when unlisted)."""
        try:
            base = normalize_ticker(symbol).replace(".CA", "")
        except Exception:
            base = str(symbol).replace(".CA", "").upper()
        if base in _SHARIAH_NON_COMPLIANT_BASE:
            return "⛔ غير متوافق (Non-Compliant)"
        if base in _SHARIAH_COMPLIANT_BASE:
            return "✅ متوافق (Compliant)"
        return "⚠️ قيد المراجعة (Needs Review)"


    def _track_label(strategy: Any) -> str:
        key = str(strategy or "").strip().lower()
        # Normalize common variants: "Scalp ⚡" -> scalp, "Swing 📈" etc.
        if "scalp" in key:
            return _TRACK_LABELS["scalp"]
        if "swing" in key:
            return _TRACK_LABELS["swing"]
        if "invest" in key:
            return _TRACK_LABELS["investment"]
        return _TRACK_LABELS.get(key, "📈 تداول سوينغ (Swing)")


    def _get_supabase_config():
        url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
        # Prioritize SERVICE_ROLE_KEY for privileged REST access (bypasses RLS)
        key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY") or "").strip().strip('"').strip("'")
        return url, key

    def _supabase_headers(prefer: str = "return=minimal"):
        # Prioritize SERVICE_ROLE_KEY for apikey + Authorization Bearer headers
        key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY") or "").strip().strip('"').strip("'")
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

        Handles both timestamp and created_at safely – normalizes timestamp to
        created_at and filters to only valid table fields.
        """
        if not isinstance(payload, dict):
            return {}
        try:
            sanitized: Dict[str, Any] = {}
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
                    if k == "ticker":
                        try:
                            sanitized[k] = normalize_ticker(payload[k])
                        except Exception:
                            sanitized[k] = payload[k]
                    else:
                        sanitized[k] = payload[k]
            if "ticker" not in sanitized and "ticker" in payload:
                try:
                    sanitized["ticker"] = normalize_ticker(payload["ticker"])
                except Exception:
                    pass
            return sanitized
        except Exception:
            try:
                return {k: v for k, v in payload.items() if k in _ALLOWED_ACTIVE_FIELDS and k != "timestamp"}
            except Exception:
                return {}


    def normalize_ticker(symbol: str) -> str:
        """Strict ticker normalization for dedup – strips, upper-cases, handles .CA suffix."""
        try:
            t = str(symbol).strip().upper()
            if not t:
                return ""
            if not t.endswith(".CA"):
                t = f"{t}.CA"
            return t
        except Exception:
            try:
                return str(symbol).strip().upper()
            except Exception:
                return ""


    # --------------------------------------------------------------------------
    # join_trade CallbackQuery flow (multi-tenant opt-in)
    # --------------------------------------------------------------------------
    # join_trade CallbackQuery flow (multi-tenant opt-in)
    # --------------------------------------------------------------------------


    def parse_join_callback(data: str) -> Optional[Tuple[str, int]]:
        """Parse 'join_trade:{TICKER}[:{TRADE_ID}]' -> (normalized_ticker, trade_id).

        Returns None for malformed payloads. Never raises.
        """
        try:
            raw = str(data or "").strip()
            if not raw.startswith(JOIN_CALLBACK_PREFIX + ":"):
                return None
            parts = raw.split(":")
            if len(parts) < 2:
                return None
            ticker = normalize_ticker(parts[1])
            if not ticker:
                return None
            trade_id = 0
            if len(parts) >= 3 and parts[2].strip():
                trade_id = int(parts[2])
                if trade_id < 0:
                    trade_id = 0
            return ticker, trade_id
        except (ValueError, TypeError):
            return None


    def _fetch_trade_signal(supabase_url: str, supabase_key: str, ticker: str, trade_id: int = 0) -> Optional[Dict[str, Any]]:
        """Fetch trade specs from public.trade_signals (exclusive read source). Never raises.

        Priority: if trade_id > 0, query by trade_id; else latest by symbol/ticker_bare.
        Maps trade_signals columns (stop_loss) to webhook card fields (current_stop_loss) for compatibility.
        """
        if requests is None:
            logger.warning("[JOIN][ENV AUDIT] requests library unavailable - cannot fetch trade_signals")
            return None
        if not supabase_url or not supabase_key:
            logger.warning("[JOIN][ENV AUDIT] SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY missing - trade_signals fetch skipped (ticker=%s)", ticker)
            print(f"[JOIN][ENV AUDIT] SUPABASE_URL or key missing - skipping fetch for {ticker}")
            return None
        headers = {
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}",
            "Content-Type": "application/json",
        }
        # 1) Try by trade_id if provided (most precise) - live schema uses id, fallback to trade_id
        if trade_id and trade_id > 0:
            for id_col in ("id", "trade_id"):
                try:
                    url = f"{supabase_url}/rest/v1/{TRADE_SIGNALS_TABLE}?{id_col}=eq.{int(trade_id)}&limit=1&select=*"
                    resp = requests.get(url, headers=headers, timeout=10)
                    if resp.status_code == 200:
                        rows = resp.json()
                        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                            row = dict(rows[0])
                            if "stop_loss" in row and "current_stop_loss" not in row:
                                row["current_stop_loss"] = row.get("stop_loss")
                            return row
                        # No row for this id column - try next
                        continue
                    elif resp.status_code == 400 and ("PGRST204" in (resp.text or "") or "42703" in (resp.text or "") or "column" in (resp.text or "").lower()):
                        continue
                    else:
                        body = (resp.text or "")[:300]
                        if 400 <= resp.status_code < 500:
                            logger.warning("[JOIN][SUPABASE 4xx] trade_signals by %s failed %s: %s", id_col, resp.status_code, body)
                        elif resp.status_code >= 500:
                            logger.warning("[JOIN][SUPABASE 5xx] trade_signals by %s failed %s: %s", id_col, resp.status_code, body)
                except Exception as exc:
                    logger.warning("[JOIN] trade_signals by %s error for %s: %s", id_col, ticker, exc)
                    continue
        # 2) Latest by symbol / ticker_bare (covers join_trade:{TICKER} without trade_id)
        for col in ("symbol", "ticker_bare", "ticker"):
            try:
                url = f"{supabase_url}/rest/v1/{TRADE_SIGNALS_TABLE}?{col}=eq.{ticker}&order=created_at.desc&limit=1&select=*"
                resp = requests.get(url, headers=headers, timeout=10)
                if resp.status_code == 200:
                    rows = resp.json()
                    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                        row = dict(rows[0])
                        if "stop_loss" in row and "current_stop_loss" not in row:
                            row["current_stop_loss"] = row.get("stop_loss")
                        return row
                    # No row for this column - try next column
                    continue
                elif resp.status_code == 400 and ("PGRST204" in (resp.text or "") or "column" in (resp.text or "").lower()):
                    continue  # column not exists, try next
                else:
                    body = (resp.text or "")[:300]
                    if 400 <= resp.status_code < 500:
                        logger.warning("[JOIN][SUPABASE 4xx] trade_signals fetch by %s failed %s: %s", col, resp.status_code, body)
                    elif resp.status_code >= 500:
                        logger.warning("[JOIN][SUPABASE 5xx] trade_signals fetch by %s failed %s: %s", col, resp.status_code, body)
            except Exception as exc:
                logger.warning("[JOIN] trade_signals fetch by %s error for %s: %s", col, ticker, exc)
                continue
        return None


    def _fetch_latest_sent_alert(supabase_url: str, supabase_key: str, ticker: str) -> Optional[Dict[str, Any]]:
        """Legacy fallback: queries sent_alerts (deprecated, replaced by trade_signals). Never raises."""
        if requests is None:
            return None
        if not supabase_url or not supabase_key:
            return None
        try:
            url = f"{supabase_url}/rest/v1/{LEGACY_SENT_ALERTS_TABLE}?ticker=eq.{ticker}&order=created_at.desc&limit=1&select=*"
            resp = requests.get(
                url,
                headers={
                    "apikey": supabase_key,
                    "Authorization": f"Bearer {supabase_key}",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
            if resp.status_code != 200:
                return None
            rows = resp.json()
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                return rows[0]
            return None
        except Exception:
            return None


    def _upsert_user_portfolio(
        supabase_url: str,
        supabase_key: str,
        user_id: str,
        trade_id: int,
        symbol: str,
        snapshot: Dict[str, Any],
    ) -> Tuple[bool, bool]:
        """Idempotently register a user against a symbol in user_portfolio.

        Uses upsert with on_conflict=user_id,symbol to prevent crash on duplicate
        button clicks. Returns (registered_or_exists, already_joined). Never raises.
    
        Logs explicit warnings for missing env or HTTP 4xx/5xx.
        """
        if requests is None:
            logger.warning("[JOIN][ENV AUDIT] requests unavailable - cannot write user_portfolio")
            return False, False
        if not supabase_url or not supabase_key:
            logger.warning("[JOIN][ENV AUDIT] SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY missing - user_portfolio upsert skipped (user=%s symbol=%s)", user_id, symbol)
            print(f"[JOIN][ENV AUDIT] SUPABASE_URL or key missing - skipping upsert for user={user_id} symbol={symbol}")
            return False, False
        # Deployed user_portfolio schema (via Supabase): id, user_id, symbol, trade_id, status, joined_at
        # No `snapshot` column (PGRST204), so omit it; keep payload minimal and schema-compatible
        payload: Dict[str, Any] = {
            "user_id": str(user_id),
            "trade_id": int(trade_id) if trade_id else 0,
            "symbol": normalize_ticker(symbol),
            "status": "TRACKING",
            "joined_at": datetime.now(timezone.utc).isoformat(),
        }
        # Include snapshot only if caller provided and column exists (handled via fallback)
        # To avoid PGRST204, we do NOT send snapshot by default; log it for visibility
        if snapshot:
            print(f"[SUPABASE] Snapshot provided (not sent due to missing column): {json.dumps(snapshot)[:300]}")
        headers = {
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}",
            "Content-Type": "application/json",
        }
        # Preferred path: merge-duplicates on the UNIQUE(user_id, symbol) constraint.
        # Ensures duplicate button clicks are idempotent via on_conflict.
        try:
            upsert_headers = dict(headers)
            upsert_headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
            print(f"[SUPABASE] POST user_portfolio upsert payload={json.dumps(payload)[:500]}")
            resp = requests.post(
                f"{supabase_url}/rest/v1/{USER_PORTFOLIO_TABLE}?on_conflict=user_id,symbol",
                json=payload,
                headers=upsert_headers,
                timeout=10,
            )
            print(f"[SUPABASE] Response code={resp.status_code} body={resp.text[:500]}")
            if resp.status_code in (200, 201, 204):
                print(f"[SUPABASE] Upsert SUCCESS code={resp.status_code} user={user_id} symbol={symbol}")
                return True, False
            if resp.status_code == 409:
                print(f"[SUPABASE] Upsert 409 ALREADY JOINED code=409 user={user_id} symbol={symbol}")
                logger.info("[JOIN] user_portfolio upsert 409 - already joined (user=%s symbol=%s)", user_id, symbol)
                return True, True
            body = (resp.text or "")[:300]
            if 400 <= resp.status_code < 500:
                logger.warning("[JOIN][SUPABASE 4xx] user_portfolio upsert %s (%s) - check SUPABASE_SERVICE_ROLE_KEY / RLS", resp.status_code, body)
                print(f"[JOIN][SUPABASE 4xx] user_portfolio upsert {resp.status_code}: {body[:200]}")
            elif resp.status_code >= 500:
                logger.warning("[JOIN][SUPABASE 5xx] user_portfolio upsert %s (%s)", resp.status_code, body)
                print(f"[JOIN][SUPABASE 5xx] user_portfolio upsert {resp.status_code}: {body[:200]}")
            else:
                logger.warning("[JOIN] user_portfolio upsert %s (%s)", resp.status_code, body)
        except Exception as exc:
            logger.warning("[JOIN] user_portfolio upsert request failed: %s", exc)
        # Fallback: plain insert - 409 here means the user already joined.
        try:
            plain_headers = dict(headers)
            plain_headers["Prefer"] = "return=minimal"
            print(f"[SUPABASE] Fallback POST user_portfolio plain insert payload={json.dumps(payload)[:500]}")
            resp2 = requests.post(
                f"{supabase_url}/rest/v1/{USER_PORTFOLIO_TABLE}",
                json=payload,
                headers=plain_headers,
                timeout=10,
            )
            print(f"[SUPABASE] Fallback Response code={resp2.status_code} body={resp2.text[:500]}")
            if resp2.status_code in (200, 201, 204):
                print(f"[SUPABASE] Plain insert SUCCESS code={resp2.status_code} user={user_id} symbol={symbol}")
                return True, False
            if resp2.status_code == 409:
                print(f"[SUPABASE] Plain insert 409 ALREADY JOINED user={user_id} symbol={symbol}")
                logger.info("[JOIN] user_portfolio insert 409 - already joined (user=%s symbol=%s)", user_id, symbol)
                return True, True
            body2 = (resp2.text or "")[:300]
            if 400 <= resp2.status_code < 500:
                logger.warning("[JOIN][SUPABASE 4xx] user_portfolio insert failed %s: %s", resp2.status_code, body2)
                print(f"[JOIN][SUPABASE 4xx] user_portfolio insert {resp2.status_code}: {body2[:200]}")
            elif resp2.status_code >= 500:
                logger.warning("[JOIN][SUPABASE 5xx] user_portfolio insert failed %s: %s", resp2.status_code, body2)
                print(f"[JOIN][SUPABASE 5xx] user_portfolio insert {resp2.status_code}: {body2[:200]}")
            else:
                logger.warning("[JOIN] user_portfolio insert failed %s: %s", resp2.status_code, body2)
        except Exception as exc:
            logger.warning("[JOIN] user_portfolio insert request failed: %s", exc)
        return False, False


    def _check_portfolio_exists(supabase_url: str, supabase_key: str, user_id: str, symbol: str) -> bool:
        """Idempotent pre-check: query public.user_portfolio for (user_id, symbol).

        Returns True if record ALREADY exists (user previously joined), False otherwise.
        Never raises. Used to prevent duplicate DM + duplicate DB writes.
        """
        if requests is None:
            return False
        if not supabase_url or not supabase_key or not user_id or not symbol:
            return False
        try:
            norm_symbol = normalize_ticker(symbol)
            url = f"{supabase_url}/rest/v1/{USER_PORTFOLIO_TABLE}?user_id=eq.{user_id}&symbol=eq.{norm_symbol}&select=*&limit=1"
            headers = {
                "apikey": supabase_key,
                "Authorization": f"Bearer {supabase_key}",
                "Content-Type": "application/json",
            }
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                try:
                    rows = resp.json()
                    if isinstance(rows, list) and len(rows) > 0:
                        print(f"[IDEMPOTENT][CHECK] user={user_id} symbol={norm_symbol} EXISTS ({len(rows)} rows) - duplicate")
                        logger.info("[IDEMPOTENT] user=%s symbol=%s already exists - duplicate join blocked", user_id, norm_symbol)
                        return True
                    print(f"[IDEMPOTENT][CHECK] user={user_id} symbol={norm_symbol} NOT FOUND - proceed with join")
                    return False
                except Exception as je:
                    print(f"[IDEMPOTENT][CHECK] parse failed: {je} body={resp.text[:200]}")
                    return False
            # Non-200: treat as not exists to allow upsert path (which will log 4xx/5xx)
            print(f"[IDEMPOTENT][CHECK] GET failed code={resp.status_code} body={resp.text[:200]} - treating as not exists")
            if 400 <= resp.status_code < 500:
                logger.warning("[IDEMPOTENT][SUPABASE 4xx] existence check failed %s: %s", resp.status_code, (resp.text or "")[:200])
            elif resp.status_code >= 500:
                logger.warning("[IDEMPOTENT][SUPABASE 5xx] existence check failed %s: %s", resp.status_code, (resp.text or "")[:200])
            return False
        except Exception as exc:
            print(f"[IDEMPOTENT][CHECK][ERROR] {exc}")
            logger.warning("[IDEMPOTENT] existence check error: %s", exc)
            return False

    def _get_first(alert: Optional[Dict[str, Any]], keys: List[str]) -> Any:
        """Fetch first available key from alert without hardcoded fallback."""
        if not isinstance(alert, dict):
            return None
        for k in keys:
            if k in alert and alert[k] is not None and str(alert[k]).strip() != "":
                return alert[k]
        return None

    def _format_price(val: Any) -> str:
        """Format price without hardcoded 0.0 fallback; return '-' if missing."""
        if val is None or val == "":
            return "-"
        try:
            return f"{float(val):.2f}"
        except:
            return str(val)

    def build_full_dm_card(ticker: str, alert: Optional[Dict[str, Any]]) -> str:
        """FULL private detail card DM'd to a user right after they join - strict template."""
        bare = normalize_ticker(ticker).replace(".CA", "")
        # Fetch complete fields from trade_signals without hardcoded fallbacks
        # Strategy: try strategy_type first, then strategy
        strategy_raw = _get_first(alert, ["strategy_type", "strategy", "strategyType", "trade_track"])
        # TQI: try tqi_score, tqi, TQI_score
        tqi_raw = _get_first(alert, ["tqi_score", "tqi", "TQI", "tqiScore"])
        # Shariah: try shariah_status, then fallback to flag map
        shariah_raw = _get_first(alert, ["shariah_status", "shariahStatus", "shariah"])
        # Prices: try multiple keys without defaulting to 0.0
        entry_raw = _get_first(alert, ["entry_price", "entry", "price", "close"])
        sl_raw = _get_first(alert, ["stop_loss", "current_stop_loss", "sl", "stopLoss"])
        t1_raw = _get_first(alert, ["target_1", "target1", "tp1"])
        t2_raw = _get_first(alert, ["target_2", "target2", "tp2"])
        t3_raw = _get_first(alert, ["target_3", "target3", "tp3"])
        # Ticker & Name: try company_name, name, then ticker
        name_raw = _get_first(alert, ["company_name", "name", "company", "symbol_name"])
        # Use alert ticker if available, else passed ticker
        display_ticker = _get_first(alert, ["ticker", "symbol", "ticker_bare"]) or ticker
        display_ticker = normalize_ticker(str(display_ticker)) if display_ticker else ticker
        bare_display = normalize_ticker(str(display_ticker)).replace(".CA", "") if display_ticker else bare

        # Format fields
        strategy_label = _track_label(strategy_raw) if strategy_raw else _track_label("swing")
        # TQI formatting: show as X.X/10 if available
        if tqi_raw is not None:
            try:
                tqi_str = f"{float(tqi_raw):.1f}/10"
            except:
                tqi_str = f"{tqi_raw}/10"
        else:
            # Try to get from alert's tqi field, if still missing show '-'
            tqi_str = "-"
        # Shariah: prefer DB shariah_status, else flag map
        if shariah_raw:
            # Normalize DB value
            sh = str(shariah_raw).strip().upper()
            if sh in ("COMPLIANT", "COMPLIANT_BASE", "HALAL"):
                shariah_text = "✅ متوافق (Compliant)"
            elif sh in ("NON_COMPLIANT", "NON-COMPLIANT", "HARAM"):
                shariah_text = "⛔ غير متوافق (Non-Compliant)"
            else:
                shariah_text = f"{shariah_raw}"
            shariah_line = f"⚖️ <b>التوافق الشرعي:</b> {shariah_text}"
        else:
            shariah_line = f"⚖️ <b>التوافق الشرعي:</b> {_shariah_flag(display_ticker)}"

        # Ticker & Name line
        if name_raw and str(name_raw).strip() and str(name_raw).strip().upper() != bare_display.upper():
            ticker_line = f"🔹 <b>السهم:</b> <code>{bare_display}</code> - {name_raw}"
        else:
            # Show ticker with bare and full
            ticker_line = f"🔹 <b>السهم:</b> <code>{bare_display}</code> ({display_ticker})"

        sep = "------------------------------------"
        lines: List[str] = [
            "🟢 <b>[كارت انضمام للصفقة]</b>",
            sep,
            ticker_line,
            f"🧠 <b>الاستراتيجية:</b> {strategy_label}",
            f"🎯 <b>تقييم الجودة (TQI):</b> {tqi_str}",
            shariah_line,
            sep,
            f"💵 <b>الدخول:</b> {_format_price(entry_raw)} EGP",
            f"🔴 <b>وقف الخسارة (SL):</b> <b>{_format_price(sl_raw)}</b> EGP",
            f"🥇 الهدف الأول: <b>{_format_price(t1_raw)}</b> EGP",
            f"🥈 الهدف الثاني: <b>{_format_price(t2_raw)}</b> EGP",
            f"🥉 الهدف الثالث: <b>{_format_price(t3_raw)}</b> EGP",
            sep,
            "🔒 تداول فوري (Spot) فقط - تم إضافة الصفقة لمحفظتك للمتابعة.",
        ]
        return "\n".join(lines)

    def build_dm_inline_keyboard(ticker: str, trade_id: int) -> Dict[str, Any]:
        """Build inline keyboard for private DM with portfolio management buttons."""
        # Normalize ticker for callback
        try:
            norm_ticker = normalize_ticker(ticker).replace(".CA", "") if ticker else ""
        except:
            norm_ticker = str(ticker).replace(".CA", "").upper() if ticker else ""
        # Ensure trade_id is int
        try:
            tid = int(trade_id) if trade_id else 0
        except:
            tid = 0
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "📊 حالة الصفقة | Check Status",
                        "callback_data": f"portfolio_status:{norm_ticker}"
                    }
                ],
                [
                    {
                        "text": "🛑 خروج من الصفقة | Exit Trade",
                        "callback_data": f"leave_trade:{norm_ticker}:{tid}"
                    }
                ]
            ]
        }


    def send_private_dm(bot_token: str, chat_id: str, text: str) -> bool:
        """Deliver an HTML card to one private chat. Never raises.

        Wrapped in try/except catching Telegram 403 Forbidden (bot can't initiate
        conversation) so caller can fallback to answerCallbackQuery alert.
        """
        if requests is None or not bot_token or not chat_id:
            return False
        try:
            resp = requests.post(
                TELEGRAM_SEND_URL.format(token=bot_token),
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            ok = resp.status_code == 200
            if not ok:
                body = (resp.text or "")[:300]
                # 403 Forbidden guard: user has not started bot
                if resp.status_code == 403 or "Forbidden" in body or "can't initiate conversation" in body or "bot can't initiate" in body.lower():
                    logger.warning("[JOIN][403 GUARD] DM to %s forbidden (403): %s - user has not started bot", str(chat_id)[:8], body[:160])
                else:
                    logger.warning("[JOIN] DM to %s failed (%s): %s", str(chat_id)[:8], resp.status_code, body[:160])
            return ok
        except Exception as exc:
            # Catch 403 in exception path as well (some clients raise for 403)
            msg = str(exc)
            if "403" in msg or "Forbidden" in msg or "can't initiate" in msg.lower():
                logger.warning("[JOIN][403 GUARD] DM request 403 to %s: %s", str(chat_id)[:8], exc)
            else:
                logger.warning("[JOIN] DM request failed for %s: %s", str(chat_id)[:8], exc)
            return False


    def _is_telegram_forbidden(resp: Any) -> bool:
        """Detect Telegram 403 Forbidden 'bot can't initiate conversation' from response or exception."""
        try:
            if resp is None:
                return False
            status = getattr(resp, "status_code", 0) or 0
            text = str(getattr(resp, "text", "") or "")
            if status == 403:
                return True
            low = text.lower()
            if "forbidden" in low and ("can't initiate" in low or "bot can't initiate" in low or "have not started" in low):
                return True
            if "forbidden: bot can't initiate conversation" in low:
                return True
        except Exception:
            pass
        return False


    def handle_join_trade(
        query: Dict[str, Any],
        data: str,
        bot_token: str,
        supabase_url: str,
        supabase_key: str,
    ) -> Tuple[bool, str]:
        """Full join_trade flow: register in user_portfolio + DM the full card.

        Guarantees Telegram spinner is killed IMMEDIATELY (answerCallbackQuery
        before any network), handles Supabase upserts idempotently, and never
        raises into the Vercel handler.
        """
        callback_query_id = ""
        try:
            try:
                callback_query_id = str((query or {}).get("id", "")).strip()
            except Exception:
                callback_query_id = ""
            # Instant feedback: stop spinner right away regardless of downstream outcome.
            if callback_query_id and bot_token:
                try:
                    _answer_callback(callback_query_id, bot_token, "⏳ جاري تسجيل متابعتك...")
                except Exception as _immediate_exc:
                    logger.warning("[JOIN] Immediate answerCallbackQuery failed: %s", _immediate_exc)

            parsed = parse_join_callback(data)
            if parsed is None:
                logger.warning("[JOIN] Unrecognized join payload ignored: %r", str(data)[:60])
                if callback_query_id and bot_token:
                    try:
                        _answer_callback(callback_query_id, bot_token, "⚠️ صيغة غير صحيحة - حاول مرة أخرى")
                    except Exception:
                        pass
                return False, "unrecognized-payload"
            ticker_bare, trade_id = parsed

            from_user = query.get("from") or {}
            user_id = str(from_user.get("id", "")).strip()
            if not user_id:
                if callback_query_id and bot_token:
                    try:
                        _answer_callback(callback_query_id, bot_token, "⚠️ تعذر تحديد هويتك - حاول مرة أخرى")
                    except Exception:
                        pass
                return False, "missing-user-id"

            # Environment audit: explicit warnings if Supabase credentials are absent.
            if not supabase_url:
                logger.warning("[JOIN][ENV AUDIT] SUPABASE_URL is missing - user_portfolio write will be skipped")
                print("[JOIN][ENV AUDIT] SUPABASE_URL is missing - user_portfolio write will be skipped")
            if not supabase_key:
                logger.warning("[JOIN][ENV AUDIT] SUPABASE_SERVICE_ROLE_KEY / SUPABASE_KEY is missing - user_portfolio write will be skipped")
                print("[JOIN][ENV AUDIT] SUPABASE_SERVICE_ROLE_KEY / SUPABASE_KEY is missing - user_portfolio write will be skipped")

            # === Idempotent Join Check: query public.user_portfolio for (user_id, symbol) ===
            # If record ALREADY exists -> immediate popup (show_alert=True) with Arabic message, NO DM, NO duplicate DB write.
            if supabase_url and supabase_key:
                try:
                    if _check_portfolio_exists(supabase_url, supabase_key, user_id, ticker_bare):
                        try:
                            _answer_callback(
                                callback_query_id,
                                bot_token,
                                "ℹ️ أنت تتابع هذه الصفقة بالفعل في محفظتك! يمكنك متابعة تحديثاتها عبر المحادثة الخاصة مع البوت.",
                                show_alert=True,
                            )
                            print(f"[IDEMPOTENT] Duplicate blocked for user={user_id} symbol={ticker_bare} - popup sent, no DM, no DB write")
                        except Exception as _dup_exc:
                            logger.warning("[JOIN] Duplicate popup failed: %s", _dup_exc)
                        logger.info("[JOIN] user=%s trade=%s id=%s -> already-joined (pre-check) no DB write no DM", user_id, ticker_bare, trade_id)
                        return True, "already-joined (pre-check) dm=False"
                except Exception as _chk_exc:
                    logger.warning("[JOIN] pre-check failed: %s - proceeding to upsert", _chk_exc)
                    print(f"[IDEMPOTENT][ERROR] pre-check failed: {_chk_exc}")

            registered = False
            already = False
            alert: Optional[Dict[str, Any]] = None
            if supabase_url and supabase_key:
                # Exclusive read from trade_signals (scanner pipeline sync)
                alert = _fetch_trade_signal(supabase_url, supabase_key, ticker_bare, trade_id=trade_id)
                # Fallback to legacy sent_alerts only if trade_signals yields nothing (backward compat, logs warning)
                if alert is None:
                    logger.info("[JOIN] trade_signals miss for %s id=%s, trying legacy sent_alerts fallback", ticker_bare, trade_id)
                    alert = _fetch_latest_sent_alert(supabase_url, supabase_key, ticker_bare)
                # Normalize stop_loss column naming (trade_signals uses stop_loss, webhook card uses current_stop_loss)
                sl_value = (alert or {}).get("current_stop_loss")
                if sl_value is None:
                    sl_value = (alert or {}).get("stop_loss")
                snapshot = {
                    "strategy": str((alert or {}).get("strategy") or ""),
                    "entry_price": (alert or {}).get("entry_price"),
                    "current_stop_loss": sl_value,
                    "target_1": (alert or {}).get("target_1"),
                    "target_2": (alert or {}).get("target_2"),
                    "target_3": (alert or {}).get("target_3"),
                    "source": "vercel_webhook",
                }
                registered, already = _upsert_user_portfolio(
                    supabase_url=supabase_url,
                    supabase_key=supabase_key,
                    user_id=user_id,
                    trade_id=trade_id,
                    symbol=ticker_bare,
                    snapshot=snapshot,
                )
                if not registered and not already:
                    logger.warning("[JOIN] Supabase upsert returned no success for user=%s symbol=%s (check logs for 4xx/5xx)", user_id, ticker_bare)
                # Race-condition duplicate (409) -> treat as already-joined, NO DM, popup with show_alert=True
                if already:
                    try:
                        _answer_callback(
                            callback_query_id,
                            bot_token,
                            "ℹ️ أنت تتابع هذه الصفقة بالفعل في محفظتك! يمكنك متابعة تحديثاتها عبر المحادثة الخاصة مع البوت.",
                            show_alert=True,
                        )
                        print(f"[IDEMPOTENT] 409 race duplicate for user={user_id} symbol={ticker_bare} - popup sent, no DM")
                    except Exception as _race_exc:
                        logger.warning("[JOIN] Race duplicate popup failed: %s", _race_exc)
                    logger.info("[JOIN] user=%s trade=%s id=%s -> already-joined (409) no DM", user_id, ticker_bare, trade_id)
                    return True, "already-joined (409) dm=False"
            else:
                logger.warning("[JOIN] Supabase config missing - registration skipped (check SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY)")

            # Proceed only for NEW joins -> build DM card + send private DM with keyboard
            card = build_full_dm_card(ticker_bare, alert)
            # === DM dispatch with 403 Forbidden guard (only for new joins) ===
            delivered = False
            is_forbidden = False
            try:
                if requests is None or not bot_token or not user_id:
                    print(f"[DM][SKIP] requests={bool(requests)} bot_token={bool(bot_token)} user_id={user_id}")
                    delivered = False
                else:
                    # Build inline portfolio management buttons per spec
                    try:
                        dm_keyboard = build_dm_inline_keyboard(ticker_bare, trade_id)
                        print(f"[DM] Built inline keyboard: {json.dumps(dm_keyboard)[:300]}")
                    except Exception as kb_e:
                        print(f"[DM][WARN] Failed to build keyboard: {kb_e}")
                        dm_keyboard = None
                    dm_payload: Dict[str, Any] = {"chat_id": user_id, "text": card, "parse_mode": "HTML"}
                    if dm_keyboard:
                        dm_payload["reply_markup"] = dm_keyboard
                    print(f"[DM] Sending DM to user_id={user_id} card_len={len(card)} with keyboard={bool(dm_keyboard)}")
                    print(f"[DM] Payload reply_markup={json.dumps(dm_payload.get('reply_markup', {}))[:500]}")
                    resp_dm = requests.post(
                        TELEGRAM_SEND_URL.format(token=bot_token),
                        json=dm_payload,
                        timeout=10,
                    )
                    print(f"[DM] Response code={resp_dm.status_code} body={resp_dm.text[:500]}")
                    if resp_dm.status_code == 200:
                        print(f"[DM] SUCCESS DM delivered to {user_id} code=200 with buttons")
                        delivered = True
                    elif _is_telegram_forbidden(resp_dm):
                        is_forbidden = True
                        delivered = False
                        print(f"[DM][403] Forbidden for user {str(user_id)[:8]} code=403 body={resp_dm.text[:300]}")
                        logger.warning("[JOIN][403 GUARD] DM forbidden for user %s (403) - needs /start @EGX.signals", str(user_id)[:8])
                    else:
                        body = (resp_dm.text or "")[:160]
                        print(f"[DM][FAIL] code={resp_dm.status_code} body={body} user={str(user_id)[:8]}")
                        logger.warning("[JOIN] DM to %s failed (%s): %s", str(user_id)[:8], resp_dm.status_code, body)
                        delivered = False
            except Exception as exc:
                txt = str(exc).lower()
                if "403" in txt or "forbidden" in txt or "can't initiate" in txt:
                    is_forbidden = True
                    logger.warning("[JOIN][403 GUARD] DM exception 403 for user %s: %s", str(user_id)[:8], exc)
                else:
                    logger.warning("[JOIN] DM request failed for %s: %s", str(user_id)[:8], exc)
                delivered = False

            # 403 fallback: user has not started bot -> show alert popup
            if is_forbidden:
                try:
                    _answer_callback(
                        callback_query_id,
                        bot_token,
                        "⚠️ يرجى بدء المحادثة مع البوت أولاً عبر إرسال /start إلى @EGX.signals ثم إعادة الضغط.",
                        show_alert=True,
                    )
                    print(f"[JOIN][403 GUARD] Fallback alert sent to {str(user_id)[:8]}")
                except Exception as e:
                    logger.warning("[JOIN][403 GUARD] Fallback alert failed: %s", e)
                logger.info("[JOIN] user=%s trade=%s id=%s -> dm_forbidden (403) needs /start", user_id, ticker_bare, trade_id)
                return True, f"dm_forbidden trade={ticker_bare} id={trade_id} already={already} registered={registered}"

            # Success path: upsert succeeded, DM sent (or attempted), now show success popup
            if registered:
                _answer_callback(callback_query_id, bot_token, "✅ تم تسجيل الصفقة بنجاح! راجع المحادثة الخاصة.")
                detail = f"registered dm={delivered}"
            else:
                # Fallback when supabase missing or upsert not confirmed but DM still sent
                _answer_callback(callback_query_id, bot_token, "✅ تم تسجيل الصفقة بنجاح! راجع المحادثة الخاصة.")
                detail = f"unregistered dm={delivered}"
            logger.info("[JOIN] user=%s trade=%s id=%s -> %s", user_id, ticker_bare, trade_id, detail)
            return True, detail
        except Exception as exc:
            logger.error("[JOIN] handle_join_trade crashed: %s", exc, exc_info=True)
            # Ensure spinner is killed even on crash.
            try:
                if callback_query_id and bot_token:
                    _answer_callback(callback_query_id, bot_token, "⚠️ حدث خطأ - حاول مرة أخرى")
            except Exception:
                pass
            return False, f"error:{exc}"


    def _answer_callback(callback_query_id: str, bot_token: str, text: str, show_alert: bool = False) -> bool:
        """AnswerCallbackQuery wrapper used by the join flow. Never raises."""
        if requests is None or not bot_token or not callback_query_id:
            print(f"[CALLBACK] Skipped answerCallbackQuery missing token/cb_id requests={bool(requests)}")
            return False
        try:
            print(f"[CALLBACK] answerCallbackQuery id={callback_query_id} text={text[:80]} alert={show_alert}")
            resp = requests.post(
                TELEGRAM_ANSWER_URL.format(token=bot_token),
                json={"callback_query_id": callback_query_id, "text": text[:200], "show_alert": show_alert},
                timeout=10,
            )
            print(f"[CALLBACK] Response code={resp.status_code} body={resp.text[:300]}")
            return resp.status_code == 200
        except Exception as exc:
            print(f"[CALLBACK][ERROR] answerCallbackQuery failed: {exc}")
            logger.warning("[JOIN] answerCallbackQuery failed: %s", exc)
            return False


    def _handler_impl(request, *args, **kwargs):
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
                data = str(query.get("data") or "")
                bot_token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()

                # === ZERO-LATENCY answerCallbackQuery: absolute FIRST op before any DB/complex logic ===
                _immediate_done = False
                if bot_token and callback_id and requests:
                    try:
                        print(f"[WEBHOOK][JOIN] Immediate answerCallbackQuery id={callback_id} (zero-latency)")
                        logger.info("[WEBHOOK][JOIN] Immediate answerCallbackQuery id=%s (zero-latency first op)", callback_id)
                        _ans = requests.post(
                            TELEGRAM_ANSWER_URL.format(token=bot_token),
                            json={"callback_query_id": callback_id, "text": "⏳ جاري تسجيل متابعتك...", "show_alert": False},
                            timeout=5,
                        )
                        _immediate_done = _ans.status_code == 200
                        print(f"[WEBHOOK][JOIN] Immediate answer -> {_ans.status_code}")
                    except Exception as _e:
                        print(f"[WEBHOOK][JOIN] Immediate answer failed: {_e}")
                        logger.warning("[WEBHOOK][JOIN] Immediate answer failed: %s", _e)

                # DB config fetched AFTER instant answer to guarantee spinner stops
                supabase_url, supabase_key = _get_supabase_config()

                print(f"[WEBHOOK] callback_query id={callback_id} data={data}")
                logger.info(f"[WEBHOOK] callback_query id={callback_id} data={data}")

                # ---- Multi-tenant join_trade branch: user_portfolio + private full card DM ----
                if data.startswith(JOIN_CALLBACK_PREFIX):
                    # Wrapped execution so any downstream exception still returns 200 OK.
                    handled, join_detail = False, "not-executed"
                    try:
                        handled, join_detail = handle_join_trade(
                            query=query,
                            data=data,
                            bot_token=bot_token,
                            supabase_url=supabase_url,
                            supabase_key=supabase_key,
                        )
                    except Exception as exc:
                        print(f"[WEBHOOK][JOIN][ERROR] handle_join_trade crashed: {exc}")
                        logger.error("[WEBHOOK][JOIN] handle_join_trade crashed: %s", exc, exc_info=True)
                        # Fallback answer if immediate didn't succeed
                        if not _immediate_done and bot_token and callback_id:
                            try:
                                _answer_callback(str(callback_id), bot_token, "⚠️ حدث خطأ - حاول مرة أخرى")
                            except Exception:
                                pass
                        join_detail = f"error:{exc}"
                    print(f"[WEBHOOK][JOIN] handled={handled} detail={join_detail} immediate={_immediate_done}")
                    logger.info("[WEBHOOK][JOIN] handled=%s detail=%s immediate=%s", handled, join_detail, _immediate_done)
                    try:
                        if hasattr(request, "status_code"):
                            request.status_code = 200
                            return "OK"
                    except Exception:
                        pass
                    return {"statusCode": 200, "body": "OK"}

                # === Legacy 3-button path DISABLED ===
                # All public broadcasts now strictly use build_channel_short_card + single join_trade button.
                # Any act_/dis_/cls_ payload is deprecated and will NOT touch Supabase.
                if isinstance(data, str) and data.startswith(("act_", "dis_", "cls_")):
                    logger.warning("[DEPRECATED] Legacy callback %r received - 3-button path purged; no DB write", str(data)[:60])
                    print(f"[DEPRECATED] Legacy callback {str(data)[:60]!r} ignored (use join_trade)")
                    if bot_token and callback_id and requests:
                        try:
                            legacy_text = "⚠️ هذا الزر قديم - استخدم زر الانضمام الجديد 📥"
                            _ans = requests.post(
                                TELEGRAM_ANSWER_URL.format(token=bot_token),
                                json={"callback_query_id": callback_id, "text": legacy_text[:200], "show_alert": False},
                                timeout=10,
                            )
                            print(f"[TELEGRAM] Legacy deprecation answer -> {_ans.status_code}")
                            logger.info("[TELEGRAM] Legacy deprecation answer -> %s", _ans.status_code)
                        except Exception as exc:
                            print(f"[TELEGRAM ERROR] Legacy answer failed: {exc}")
                            logger.warning("Legacy answer failed: %s", exc)
                    try:
                        if hasattr(request, "status_code"):
                            request.status_code = 200
                            return "OK"
                    except Exception:
                        pass
                    return {"statusCode": 200, "body": "OK"}

                # No legacy DB handling - webhook writes exclusively to user_portfolio / reads from trade_signals.
                # Any unrecognized callback (non join_trade) is acknowledged without DB side-effects.
                if bot_token and callback_id and requests and data:
                    try:
                        print(f"[TELEGRAM] answerCallbackQuery unrecognized id={callback_id} data={data[:40]}")
                        logger.info("[TELEGRAM] answerCallbackQuery unrecognized id=%s", callback_id)
                        _ans2 = requests.post(
                            TELEGRAM_ANSWER_URL.format(token=bot_token),
                            json={"callback_query_id": callback_id, "text": "تم الاستلام", "show_alert": False},
                            timeout=10,
                        )
                        print(f"[TELEGRAM] Unrecognized answer -> {_ans2.status_code}")
                    except Exception as exc:
                        print(f"[TELEGRAM ERROR] Unrecognized answer failed: {exc}")
                    try:
                        if hasattr(request, "status_code"):
                            request.status_code = 200
                            return "OK"
                    except Exception:
                        pass
                    return {"statusCode": 200, "body": "OK"}
                elif bot_token and callback_id and requests:
                    try:
                        print(f"[TELEGRAM] answerCallbackQuery id={callback_id} text=unhandled (fallback)")
                        logger.info(f"[TELEGRAM] answerCallbackQuery id={callback_id} text=unhandled")
                        resp = requests.post(
                            f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery",
                            json={
                                "callback_query_id": callback_id,
                                "text": "تم الاستلام",
                                "show_alert": False,
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



except Exception as e:
    import traceback
    print("[IMPORT ERROR] helpers failed:", e)
    traceback.print_exc()

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            # Diagnostic: return env presence for verification
            supa_url = os.environ.get("SUPABASE_URL", "")
            supa_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY", "")
            bot_tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            print(f"[WEBHOOK][GET] Incoming GET - health check env supa_url={bool(supa_url)} supa_key={bool(supa_key)} bot_tok={bool(bot_tok)}")
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            diag = {
                "statusCode": 200,
                "body": "OK",
                "diag": {
                    "supabase_url_present": bool(supa_url),
                    "supabase_key_present": bool(supa_key),
                    "telegram_token_present": bool(bot_tok),
                    "supabase_url_preview": supa_url[:30] if supa_url else None,
                }
            }
            self.wfile.write(json.dumps(diag).encode())
            print(f"[WEBHOOK][GET] Response 200 OK diag={diag}")
        except Exception as e:
            print(f"[WEBHOOK][GET][ERROR] {e}")
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")
            except:
                pass
        return

    def do_POST(self):
        # === SYNCHRONOUS EXECUTION - Vercel kills background threads, so do work BEFORE response ===
        raw_body = b""
        body = {}
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw_body = self.rfile.read(length) if length else b""
            if raw_body:
                try:
                    body = json.loads(raw_body.decode("utf-8"))
                except Exception as je:
                    print(f"[WEBHOOK][POST][PARSE] Failed to parse JSON: {je} raw={raw_body[:500]}")
                    body = {}
            print(f"[WEBHOOK][POST] Incoming callback_query payload: {json.dumps(body)[:2000] if isinstance(body, dict) else str(body)[:2000]}")
            print(f"[WEBHOOK][POST] Raw body length={len(raw_body)} headers={dict(self.headers)}")
        except Exception as e:
            print(f"[WEBHOOK][POST][ERROR] Failed to read body: {e}")
            body = {}

        # Build mock request for synchronous handler
        class _Req:
            pass
        req = _Req()
        req.method = "POST"
        req.body = body
        req.headers = dict(self.headers)
        req.get_json = lambda *a, **k: body
        req.json = lambda: body

        # === SYNCHRONOUS BUSINESS LOGIC BEFORE HTTP 200 ===
        # This ensures Supabase upsert + Telegram DM complete before Vercel terminates
        print(f"[WEBHOOK][POST] Starting synchronous handler execution")
        result = None
        try:
            # Resolve target - _handler_impl is the core logic (defined inside try wrapper)
            _target = None
            try:
                _target = _handler_impl  # type: ignore
                print(f"[WEBHOOK][POST] Using _handler_impl target")
            except NameError:
                try:
                    _target = _py_handler  # type: ignore
                    print(f"[WEBHOOK][POST] Using _py_handler target")
                except NameError:
                    print(f"[WEBHOOK][POST][ERROR] No handler target found")
                    _target = None
            if _target:
                print(f"[WEBHOOK][POST] Executing synchronous _handler_impl...")
                result = _target(req)
                print(f"[WEBHOOK][POST] Synchronous handler completed result={result}")
            else:
                print(f"[WEBHOOK][POST][WARN] No target to execute, skipping business logic")
                result = {"statusCode": 200, "body": "OK - no target"}
        except Exception as e:
            print(f"[WEBHOOK][POST][ERROR] Synchronous handler crashed: {e}")
            import traceback
            traceback.print_exc()
            result = {"statusCode": 200, "body": "OK - error handled"}

        # === HTTP 200 AFTER synchronous work - include diagnostic ===
        # Verify Supabase insertion synchronously for response visibility
        supa_diag = {}
        try:
            # Check env
            try:
                supa_url_check, supa_key_check = _get_supabase_config()  # type: ignore
            except Exception as ge:
                supa_url_check, supa_key_check = "", ""
                print(f"[WEBHOOK][POST][DIAG] get_supabase_config failed: {ge}")
                supa_diag["config_error"] = str(ge)
            supa_diag["supabase_url_present"] = bool(supa_url_check)
            supa_diag["supabase_key_present"] = bool(supa_key_check)
            supa_diag["requests_available"] = bool(requests)
            print(f"[WEBHOOK][POST][DIAG] Supabase env present url={bool(supa_url_check)} key={bool(supa_key_check)} requests={bool(requests)}")
            print(f"[WEBHOOK][POST][DIAG] Body type={type(body)} has_callback={bool(isinstance(body, dict) and body.get('callback_query'))}")
            # If body was join_trade, verify DB row was written and test direct insert
            if isinstance(body, dict) and body.get("callback_query"):
                try:
                    cq = body["callback_query"]
                    data = str(cq.get("data",""))
                    print(f"[WEBHOOK][POST][DIAG] callback data={data}")
                    try:
                        parsed = parse_join_callback(data)  # type: ignore
                        print(f"[WEBHOOK][POST][DIAG] parsed={parsed}")
                    except Exception as pe:
                        parsed = None
                        print(f"[WEBHOOK][POST][DIAG] parse failed: {pe}")
                        supa_diag["parse_error"] = str(pe)
                    if parsed:
                        ticker, trade_id = parsed
                        from_user = cq.get("from",{})
                        uid = str(from_user.get("id",""))
                        print(f"[WEBHOOK][POST][DIAG] Verifying DB insertion for user={uid} ticker={ticker} trade_id={trade_id}")
                        if supa_url_check and supa_key_check and requests:
                            # Query user_portfolio for this user+ticker
                            try:
                                q_url = f"{supa_url_check}/rest/v1/user_portfolio?user_id=eq.{uid}&symbol=eq.{ticker}&select=*"
                                q_headers = {"apikey": supa_key_check, "Authorization": f"Bearer {supa_key_check}", "Content-Type": "application/json"}
                                print(f"[SUPABASE][VERIFY] GET {q_url}")
                                q_resp = requests.get(q_url, headers=q_headers, timeout=5)
                                supa_diag["verify_query_status"] = q_resp.status_code
                                supa_diag["verify_query_body"] = q_resp.text[:800]
                                print(f"[SUPABASE][VERIFY] Query after upsert code={q_resp.status_code} body={q_resp.text[:800]}")
                            except Exception as ve:
                                supa_diag["verify_error"] = str(ve)
                                print(f"[SUPABASE][VERIFY][ERROR] {ve}")
                            # Also try direct test insert to see if Vercel can write at all
                            try:
                                test_payload = {"user_id": uid, "symbol": ticker, "trade_id": int(trade_id) if trade_id else 0, "status": "TRACKING", "joined_at": datetime.now(timezone.utc).isoformat()}
                                test_url = f"{supa_url_check}/rest/v1/user_portfolio?on_conflict=user_id,symbol"
                                test_headers = {"apikey": supa_key_check, "Authorization": f"Bearer {supa_key_check}", "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=representation"}
                                print(f"[SUPABASE][DIRECT] POST test insert payload={test_payload}")
                                test_resp = requests.post(test_url, headers=test_headers, json=test_payload, timeout=5)
                                supa_diag["direct_insert_status"] = test_resp.status_code
                                supa_diag["direct_insert_body"] = test_resp.text[:800]
                                print(f"[SUPABASE][DIRECT] POST code={test_resp.status_code} body={test_resp.text[:800]}")
                            except Exception as de:
                                supa_diag["direct_error"] = str(de)
                                print(f"[SUPABASE][DIRECT][ERROR] {de}")
                        else:
                            print(f"[WEBHOOK][POST][DIAG] Skipping verify - missing env or requests")
                            supa_diag["skip_reason"] = f"url={bool(supa_url_check)} key={bool(supa_key_check)} requests={bool(requests)}"
                    else:
                        print(f"[WEBHOOK][POST][DIAG] Parsed is None, not verifying")
                        supa_diag["parsed"] = str(parsed)
                except Exception as ve:
                    print(f"[WEBHOOK][POST][DIAG][ERROR] inner {ve}")
                    import traceback
                    traceback.print_exc()
                    supa_diag["inner_error"] = str(ve)
            else:
                print(f"[WEBHOOK][POST][DIAG] No callback_query in body, not verifying")
                supa_diag["no_callback"] = True
        except Exception as de:
            print(f"[WEBHOOK][POST][DIAG][ERROR] outer {de}")
            import traceback
            traceback.print_exc()
            supa_diag["outer_error"] = str(de)

        try:
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            # Include supa_diag in response for live test visibility
            resp_body = result if isinstance(result, dict) else {"statusCode": 200, "body": str(result) if result else "OK"}
            if isinstance(resp_body, dict):
                resp_body["supabase_diag"] = supa_diag
                resp_body["callback_payload"] = body if isinstance(body, dict) else str(body)[:500]
            payload = json.dumps(resp_body).encode()
            self.wfile.write(payload)
            print(f"[WEBHOOK][POST] HTTP 200 sent synchronously after business logic, payload={payload[:800]}")
        except Exception as e:
            print(f"[WEBHOOK][POST][ERROR] Failed to send HTTP 200: {e}")
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")
            except:
                pass
        return

    def log_message(self, format, *args):
        # Suppress default http.server logging, use print for Vercel logs
        try:
            print(f"[VERCEL] {format % args}")
        except:
            pass

# Keep aliases for local imports / tests
try:
    py_handler = _handler_impl  # type: ignore
except NameError:
    try:
        py_handler = _py_handler  # type: ignore
    except NameError:
        py_handler = None
try:
    Handler = handler
except NameError:
    pass
# Expose function handler for non-class invocation (e.g., local tests)
# Note: class handler shadows function handler; py_handler preserves function access
