import json
import math
import os
import re
import threading
import traceback
import uuid
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from scraper import (
    FlightOption,
    QUERY_STATUS_SUCCESS,
    QUERY_STATUS_NO_RESULTS,
    QUERY_STATUS_BLOCKED,
    QUERY_STATUS_TIMEOUT,
    QUERY_STATUS_PARSE_ERROR,
    scrape_decoupled_roundtrip,
    get_today_cached_queries,
    clear_daily_cache,
    load_daily_cache,
    generate_date_range,
    is_cache_entry_usable,
    get_last_cache_error,
    recheck_combination,
)
from combiner import (
    build_combinations_dataframe,
    compute_effective_date_ranges,
    sanitize_records_for_json,
    DEFAULT_TRANSFER_PRESETS_EUR,
)

PORT = 8000
IATA_REGEX = re.compile(r"^[A-Z]{3}$")
DATE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VALID_CURRENCIES = {"TRY", "EUR", "USD"}

JOB_LOCK = threading.RLock()
ACTIVE_STOP_EVENT: Optional[threading.Event] = None

SCRAPE_STATUS: Dict[str, Any] = {
    "running": False,
    "stopping": False,
    "state": "idle",  # idle | running | completed | partial | stopped | error
    "search_id": None,
    "params_snapshot": None,
    "done": 0,
    "total": 0,
    "success": 0,
    "no_results": 0,
    "failed": 0,
    "blocked": 0,
    "timeout": 0,
    "parse_error": 0,
    "from_cache": 0,
    "label": "Hazır",
    "error": None,
    "cache_error": None,
}


def _sanitize_json_value(obj: Any) -> Any:
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_json_value(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_json_value(x) for x in obj]
    return obj


def validate_search_request(
    body: Dict[str, Any],
    require_scrape_limits: bool = True,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Girdi doğrulaması yapar:
    - Kalkış havalimanı geçerli 3 harfli IATA kodu olmalı.
    - Kalkış havalimanı hedef listesinden çıkarılır.
    - Benzersiz hedef havalimanı sayısı en az 1, en fazla 3 olmalı.
    - Tarihler, gece sayıları, sonuç limiti ve eşzamanlılık pozitif/geçerli aralıkta olmalı.
    """
    if not isinstance(body, dict):
        return None, "Geçersiz JSON gövdesi."

    home_airport = str(body.get("home_airport", "")).upper().strip()
    if not IATA_REGEX.match(home_airport):
        return None, f"Geçersiz kalkış havalimanı IATA kodu: '{home_airport}' (3 harf olmalı, ör. ESB)."

    raw_targets = body.get("target_airports", [])
    if not isinstance(raw_targets, list):
        return None, "target_airports bir liste olmalıdır."

    target_airports: List[str] = []
    for item in raw_targets:
        code = str(item).upper().strip()
        if not code or code == home_airport:
            continue
        if not IATA_REGEX.match(code):
            return None, f"Geçersiz hedef havalimanı IATA kodu: '{code}' (3 harf olmalı, ör. AMS)."
        if code not in target_airports:
            target_airports.append(code)

    if len(target_airports) == 0:
        return None, "Kalkış havalimanı hariç en az 1 hedef havalimanı seçilmelidir."
    if len(target_airports) > 3:
        return None, f"En fazla 3 benzersiz hedef havalimanı seçilebilir (seçilen: {len(target_airports)})."

    currency = str(body.get("currency", "TRY")).upper().strip()
    if currency not in VALID_CURRENCIES:
        return None, f"Desteklenmeyen para birimi: '{currency}'. Geçerli değerler: TRY, EUR, USD."

    try:
        min_stay_days = int(body.get("min_stay_days", 4))
        max_stay_days = int(body.get("max_stay_days", 6))
    except (ValueError, TypeError):
        return None, "Min/Maks kalış gece sayısı tam sayı olmalıdır."

    if min_stay_days < 1 or max_stay_days < 1 or min_stay_days > max_stay_days or max_stay_days > 60:
        return None, "Kalış süresi 1 ile 60 gece arasında olmalı ve Min. Gece <= Maks. Gece olmalıdır."

    try:
        max_results_per_route = int(body.get("max_results_per_route", 10))
        max_concurrent_pages = int(body.get("max_concurrent_pages", 4))
    except (ValueError, TypeError):
        return None, "Rota başı sonuç limiti ve paralel sekme sayısı tam sayı olmalıdır."

    if max_results_per_route < 1 or max_results_per_route > 50:
        return None, "Rota başı sonuç limiti 1 ile 50 arasında olmalıdır."
    if max_concurrent_pages < 1 or max_concurrent_pages > 6:
        return None, "Paralel sekme sayısı 1 ile 6 arasında olmalıdır."

    window_start = str(body.get("window_start", "")).strip()
    window_end = str(body.get("window_end", "")).strip()
    out_start = str(body.get("outbound_start_date", "")).strip()
    out_end = str(body.get("outbound_end_date", "")).strip()
    in_start = str(body.get("inbound_start_date", "")).strip()
    in_end = str(body.get("inbound_end_date", "")).strip()

    if window_start and window_end:
        if not DATE_REGEX.match(window_start) or not DATE_REGEX.match(window_end):
            return None, "Seyahat tarih aralığı YYYY-MM-DD biçiminde olmalıdır."
        try:
            ws_dt = datetime.strptime(window_start, "%Y-%m-%d")
            we_dt = datetime.strptime(window_end, "%Y-%m-%d")
        except ValueError:
            return None, "Geçersiz takvim tarihi."
        if (we_dt - ws_dt).days < min_stay_days:
            return None, "Seyahat tarih aralığı minimum kalış süresinden kısa olamaz."
        c_out_s, c_out_e, c_in_s, c_in_e = compute_effective_date_ranges(
            window_start, window_end, min_stay_days
        )
        out_start = out_start or c_out_s
        out_end = out_end or c_out_e
        in_start = in_start or c_in_s
        in_end = in_end or c_in_e
    else:
        if not all(DATE_REGEX.match(d) for d in (out_start, out_end, in_start, in_end)):
            return None, "Gidiş ve dönüş başlangıç/bitiş tarihleri YYYY-MM-DD biçiminde olmalıdır."
        window_start = out_start
        window_end = in_end

    try:
        os_dt = datetime.strptime(out_start, "%Y-%m-%d")
        oe_dt = datetime.strptime(out_end, "%Y-%m-%d")
        is_dt = datetime.strptime(in_start, "%Y-%m-%d")
        ie_dt = datetime.strptime(in_end, "%Y-%m-%d")
    except ValueError:
        return None, "Geçersiz tarih değeri."

    if oe_dt < os_dt or ie_dt < is_dt:
        return None, "Bitiş tarihleri başlangıç tarihlerinden önce olamaz."

    def _parse_optional_stops(val: Any, label: str) -> Tuple[Optional[int], Optional[str]]:
        if val is None or str(val).strip() == "":
            return None, None
        try:
            parsed = int(val)
            if parsed < 0 or parsed > 3:
                return None, f"{label} 0 ile 3 arasında olmalıdır."
            return parsed, None
        except (ValueError, TypeError):
            return None, f"Geçersiz {label} değeri."

    max_stops, err_s = _parse_optional_stops(body.get("max_stops", None), "Maksimum aktarma sayısı")
    if err_s:
        return None, err_s
    out_max_stops, err_os = _parse_optional_stops(body.get("out_max_stops", None), "Gidiş maksimum aktarma sayısı")
    if err_os:
        return None, err_os
    in_max_stops, err_is = _parse_optional_stops(body.get("in_max_stops", None), "Dönüş maksimum aktarma sayısı")
    if err_is:
        return None, err_is

    max_flight_hours_raw = body.get("max_flight_hours", None)
    max_flight_hours: Optional[float] = None
    if max_flight_hours_raw is not None and str(max_flight_hours_raw).strip() != "":
        try:
            max_flight_hours = float(max_flight_hours_raw)
            if math.isnan(max_flight_hours) or math.isinf(max_flight_hours) or max_flight_hours <= 0:
                return None, "Maksimum uçuş süresi pozitif bir sayı olmalıdır."
        except (ValueError, TypeError):
            return None, "Geçersiz maksimum uçuş süresi."

    max_total_travel_hours_raw = body.get("max_total_travel_hours", None)
    max_total_travel_hours: Optional[float] = None
    if max_total_travel_hours_raw is not None and str(max_total_travel_hours_raw).strip() != "":
        try:
            max_total_travel_hours = float(max_total_travel_hours_raw)
            if math.isnan(max_total_travel_hours) or math.isinf(max_total_travel_hours) or max_total_travel_hours <= 0:
                return None, "Maksimum toplam seyahat süresi pozitif bir sayı olmalıdır."
        except (ValueError, TypeError):
            return None, "Geçersiz maksimum toplam seyahat süresi."

    try:
        cabin_bag_fee = float(body.get("cabin_bag_fee", 0.0) or 0.0)
        checked_bag_fee = float(body.get("checked_bag_fee", 0.0) or 0.0)
        if (
            math.isnan(cabin_bag_fee)
            or math.isnan(checked_bag_fee)
            or cabin_bag_fee < 0
            or checked_bag_fee < 0
        ):
            return None, "Bagaj ek ücretleri negatif olamaz."
    except (ValueError, TypeError):
        return None, "Geçersiz bagaj ek ücreti."

    validated = {
        "home_airport": home_airport,
        "target_airports": target_airports,
        "window_start": window_start,
        "window_end": window_end,
        "outbound_start_date": out_start,
        "outbound_end_date": out_end,
        "inbound_start_date": in_start,
        "inbound_end_date": in_end,
        "currency": currency,
        "min_stay_days": min_stay_days,
        "max_stay_days": max_stay_days,
        "max_results_per_route": max_results_per_route,
        "max_concurrent_pages": max_concurrent_pages,
        "max_stops": max_stops,
        "out_max_stops": out_max_stops,
        "in_max_stops": in_max_stops,
        "max_flight_hours": max_flight_hours,
        "max_total_travel_hours": max_total_travel_hours,
        "out_dep_min_time": str(body.get("out_dep_min_time", "00:00")).strip() or "00:00",
        "out_dep_max_time": str(body.get("out_dep_max_time", "23:59")).strip() or "23:59",
        "in_dep_min_time": str(body.get("in_dep_min_time", "00:00")).strip() or "00:00",
        "in_dep_max_time": str(body.get("in_dep_max_time", "23:59")).strip() or "23:59",
        "baggage_mode": str(body.get("baggage_mode", "none")).strip() or "none",
        "cabin_bag_fee": cabin_bag_fee,
        "checked_bag_fee": checked_bag_fee,
        "excluded_airlines": body.get("excluded_airlines", []) or [],
        "only_asymmetric": bool(body.get("only_asymmetric", False)),
        "include_transfer": bool(body.get("include_transfer", False)),
        "transfer_costs": body.get("transfer_costs", {}) or {},
        "transfer_durations": body.get("transfer_durations", {}) or {},
        "client_request_id": body.get("client_request_id"),
    }
    return validated, None


def collect_search_state_from_daily_cache(
    home_airport: str,
    target_airports: List[str],
    out_start: str,
    out_end: str,
    in_start: str,
    in_end: str,
    currency: str,
    max_results: int = 10,
    cache_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Yalnızca istenen (home_airport, target_airports, tarih aralığı, para birimi, max_results)
    ile tam eşleşen ve kullanılabilir (usable) olan günlük önbellek kayıtlarını toplar.
    Önceki bir aramanın verilerini asla yedek olarak kullanmaz.
    """
    cache_data = load_daily_cache(cache_path=cache_path)
    entries = cache_data.get("entries", {})

    home = home_airport.upper().strip()
    targets = [t.upper().strip() for t in target_airports if t.strip() and t.upper().strip() != home]
    curr = currency.upper().strip()

    out_dates = generate_date_range(out_start, out_end)
    in_dates = generate_date_range(in_start, in_end)

    planned_keys = [
        ("OUT", home, target, d, f"{home}_{target}_{d}_{curr}")
        for target in targets
        for d in out_dates
    ] + [
        ("IN", target, home, d, f"{target}_{home}_{d}_{curr}")
        for target in targets
        for d in in_dates
    ]

    out_flights: List[FlightOption] = []
    in_flights: List[FlightOption] = []

    summary = {
        "total_planned_queries": len(planned_keys),
        "usable_cached_queries": 0,
        "success_queries": 0,
        "no_results_queries": 0,
        "failed_queries": 0,
        "blocked_queries": 0,
        "timeout_queries": 0,
        "parse_error_queries": 0,
        "insufficient_scope_queries": 0,
        "missing_queries": 0,
        "incomplete_scope_notes": [],
    }

    for direction, orig, dest, d, ckey in planned_keys:
        entry = entries.get(ckey)
        if not entry:
            summary["missing_queries"] += 1
            continue

        status = entry.get("status")
        if is_cache_entry_usable(entry, max_results):
            summary["usable_cached_queries"] += 1
            if status == QUERY_STATUS_NO_RESULTS:
                summary["no_results_queries"] += 1
            else:
                summary["success_queries"] += 1
                items = entry.get("flights", [])[:max_results]
                flights_objs = [FlightOption.from_dict(x) for x in items if x.get("currency", curr) == curr]
                if direction == "OUT":
                    out_flights.extend(flights_objs)
                else:
                    in_flights.extend(flights_objs)
                if not entry.get("scope_complete", True):
                    summary["incomplete_scope_notes"].append(f"{orig}->{dest} ({d})")
        else:
            if status == QUERY_STATUS_SUCCESS:
                summary["insufficient_scope_queries"] += 1
            elif status in (QUERY_STATUS_BLOCKED, QUERY_STATUS_TIMEOUT, QUERY_STATUS_PARSE_ERROR):
                summary["failed_queries"] += 1
                if status == QUERY_STATUS_BLOCKED:
                    summary["blocked_queries"] += 1
                elif status == QUERY_STATUS_TIMEOUT:
                    summary["timeout_queries"] += 1
                elif status == QUERY_STATUS_PARSE_ERROR:
                    summary["parse_error_queries"] += 1
            else:
                summary["missing_queries"] += 1

    has_data = summary["usable_cached_queries"] > 0
    needs_rescrape = (
        summary["missing_queries"] > 0
        or summary["failed_queries"] > 0
        or summary["insufficient_scope_queries"] > 0
    )

    return {
        "has_data": has_data,
        "needs_rescrape": needs_rescrape,
        "outbound_flights": out_flights,
        "inbound_flights": in_flights,
        "query_summary": summary,
    }


class FlightFinderHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        if len(args) > 0 and "/api/progress" in str(args[0]):
            return
        super().log_message(format, *args)

    def _send_json(self, data: Any, status: int = 200):
        sanitized = _sanitize_json_value(data)
        payload = json.dumps(sanitized, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        route_path = urlsplit(self.path).path

        if route_path in ("/", "/index.html"):
            try:
                with open("index.html", "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.end_headers()
                self.wfile.write(content)
                return
            except Exception as exc:
                return self._send_json({"error": str(exc)}, status=500)

        if route_path == "/api/progress":
            with JOB_LOCK:
                status_copy = dict(SCRAPE_STATUS)
                status_copy["cache_error"] = get_last_cache_error()
            return self._send_json(status_copy)

        if route_path == "/api/presets":
            return self._send_json(DEFAULT_TRANSFER_PRESETS_EUR)

        if route_path == "/api/cache":
            return self._send_json(
                {
                    "cached_queries": get_today_cached_queries(),
                    "cache_error": get_last_cache_error(),
                }
            )

        return super().do_GET()

    def do_POST(self):
        route_path = urlsplit(self.path).path
        content_len = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_len) if content_len > 0 else b"{}"
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except Exception:
            return self._send_json({"error": "Geçersiz JSON formatı."}, status=400)

        if route_path == "/api/scrape":
            return self._handle_scrape_start(body)

        if route_path == "/api/scrape/stop":
            return self._handle_scrape_stop()

        if route_path == "/api/combine":
            return self._handle_combine(body)

        if route_path == "/api/recheck":
            return self._handle_recheck(body)

        if route_path == "/api/cache/clear":
            try:
                clear_daily_cache()
                return self._send_json(
                    {
                        "cached_queries": [],
                        "cache_error": get_last_cache_error(),
                    }
                )
            except Exception as exc:
                return self._send_json({"error": f"Önbellek temizlenemedi: {exc}"}, status=500)

        self._send_json({"error": "Bulunamadı"}, status=404)

    def _handle_scrape_stop(self):
        global ACTIVE_STOP_EVENT, SCRAPE_STATUS
        with JOB_LOCK:
            if ACTIVE_STOP_EVENT is not None and SCRAPE_STATUS.get("running"):
                ACTIVE_STOP_EVENT.set()
                SCRAPE_STATUS["stopping"] = True
                SCRAPE_STATUS["label"] = "Durduruluyor (mevcut sekmelerin kapanması bekleniyor)..."
                return self._send_json({"status": "stopping", **SCRAPE_STATUS})
            return self._send_json({"status": "not_running", **SCRAPE_STATUS})

    def _handle_scrape_start(self, body: Dict[str, Any]):
        global SCRAPE_STATUS, ACTIVE_STOP_EVENT

        validated, err = validate_search_request(body, require_scrape_limits=True)
        if err or validated is None:
            return self._send_json({"error": err or "Geçersiz istek."}, status=400)

        with JOB_LOCK:
            if SCRAPE_STATUS.get("running"):
                return self._send_json(
                    {
                        "status": "already_running",
                        "error": "Şu anda devam eden bir tarama işlemi var.",
                        **SCRAPE_STATUS,
                    },
                    status=409,
                )

            search_id = str(uuid.uuid4())[:8]
            stop_event = threading.Event()
            ACTIVE_STOP_EVENT = stop_event

            params_snapshot = {
                "home_airport": validated["home_airport"],
                "target_airports": list(validated["target_airports"]),
                "window_start": validated["window_start"],
                "window_end": validated["window_end"],
                "outbound_start_date": validated["outbound_start_date"],
                "outbound_end_date": validated["outbound_end_date"],
                "inbound_start_date": validated["inbound_start_date"],
                "inbound_end_date": validated["inbound_end_date"],
                "currency": validated["currency"],
                "min_stay_days": validated["min_stay_days"],
                "max_stay_days": validated["max_stay_days"],
                "max_results_per_route": validated["max_results_per_route"],
                "max_concurrent_pages": validated["max_concurrent_pages"],
            }

            SCRAPE_STATUS = {
                "running": True,
                "stopping": False,
                "state": "running",
                "search_id": search_id,
                "params_snapshot": params_snapshot,
                "done": 0,
                "total": 1,
                "success": 0,
                "no_results": 0,
                "failed": 0,
                "blocked": 0,
                "timeout": 0,
                "parse_error": 0,
                "from_cache": 0,
                "label": "Tarayıcı başlatılıyor...",
                "error": None,
                "cache_error": None,
            }

        def progress_cb(done: int, total: int, label: str, stats: Optional[Dict[str, int]] = None):
            with JOB_LOCK:
                SCRAPE_STATUS["done"] = done
                SCRAPE_STATUS["total"] = total
                SCRAPE_STATUS["label"] = label
                if stats:
                    for k in ("success", "no_results", "failed", "blocked", "timeout", "parse_error", "from_cache"):
                        if k in stats:
                            SCRAPE_STATUS[k] = stats[k]
                SCRAPE_STATUS["cache_error"] = get_last_cache_error()

        def worker():
            global SCRAPE_STATUS
            try:
                _, _, stats = scrape_decoupled_roundtrip(
                    home_airport=params_snapshot["home_airport"],
                    target_airports=params_snapshot["target_airports"],
                    outbound_start_date=params_snapshot["outbound_start_date"],
                    outbound_end_date=params_snapshot["outbound_end_date"],
                    inbound_start_date=params_snapshot["inbound_start_date"],
                    inbound_end_date=params_snapshot["inbound_end_date"],
                    currency=params_snapshot["currency"],
                    max_results_per_route=params_snapshot["max_results_per_route"],
                    max_concurrent_pages=params_snapshot["max_concurrent_pages"],
                    headless=True,
                    progress_callback=progress_cb,
                    stop_event=stop_event,
                )
                with JOB_LOCK:
                    SCRAPE_STATUS["running"] = False
                    SCRAPE_STATUS["stopping"] = False
                    for k in ("done", "total", "success", "no_results", "failed", "blocked", "timeout", "parse_error", "from_cache"):
                        if k in stats:
                            SCRAPE_STATUS[k] = stats[k]
                    SCRAPE_STATUS["cache_error"] = get_last_cache_error()

                    if stop_event.is_set():
                        SCRAPE_STATUS["state"] = "stopped"
                        SCRAPE_STATUS["label"] = (
                            f"Durduruldu ({stats.get('done', 0)}/{stats.get('total', 0)} sorgu işlendi)"
                        )
                    elif stats.get("failed", 0) > 0:
                        SCRAPE_STATUS["state"] = "partial"
                        blocked_msg = (
                            f" ({stats['blocked']} CAPTCHA/engel!)" if stats.get("blocked", 0) > 0 else ""
                        )
                        SCRAPE_STATUS["label"] = (
                            f"Kısmi tamamlandı: {stats.get('success', 0)} başarılı, "
                            f"{stats.get('no_results', 0)} uçuş yok, "
                            f"{stats.get('failed', 0)} hatalı{blocked_msg}"
                        )
                    else:
                        SCRAPE_STATUS["state"] = "completed"
                        SCRAPE_STATUS["label"] = (
                            f"Tamamlandı: {stats.get('success', 0)} başarılı, "
                            f"{stats.get('no_results', 0)} uçuş yok"
                        )
            except Exception as exc:
                traceback.print_exc()
                with JOB_LOCK:
                    SCRAPE_STATUS["running"] = False
                    SCRAPE_STATUS["stopping"] = False
                    SCRAPE_STATUS["state"] = "error"
                    SCRAPE_STATUS["error"] = str(exc)
                    SCRAPE_STATUS["label"] = f"Hata: {exc}"
                    SCRAPE_STATUS["cache_error"] = get_last_cache_error()

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        with JOB_LOCK:
            return self._send_json({"status": "started", **SCRAPE_STATUS})

    def _handle_combine(self, body: Dict[str, Any]):
        validated, err = validate_search_request(body, require_scrape_limits=False)
        if err or validated is None:
            return self._send_json({"error": err or "Geçersiz istek."}, status=400)

        cache_state = collect_search_state_from_daily_cache(
            home_airport=validated["home_airport"],
            target_airports=validated["target_airports"],
            out_start=validated["outbound_start_date"],
            out_end=validated["outbound_end_date"],
            in_start=validated["inbound_start_date"],
            in_end=validated["inbound_end_date"],
            currency=validated["currency"],
            max_results=validated["max_results_per_route"],
        )

        out_flights: List[FlightOption] = cache_state["outbound_flights"]
        in_flights: List[FlightOption] = cache_state["inbound_flights"]
        has_data: bool = cache_state["has_data"]
        q_summary = cache_state["query_summary"]

        available_airlines = sorted(
            {
                f.airline.strip()
                for f in (out_flights + in_flights)
                if f.airline and f.airline.strip() and f.airline.strip() != "Doğrulanmadı"
            }
        )

        if not has_data:
            return self._send_json(
                {
                    "has_data": False,
                    "needs_rescrape": True,
                    "message": "Bu arama için henüz veri yok.",
                    "currency": validated["currency"],
                    "outbound_count": 0,
                    "inbound_count": 0,
                    "combinations_count": 0,
                    "combinations": [],
                    "available_airlines": [],
                    "outbound_flights": [],
                    "inbound_flights": [],
                    "query_summary": q_summary,
                    "cached_queries": get_today_cached_queries(),
                    "cache_error": get_last_cache_error(),
                    "client_request_id": validated["client_request_id"],
                }
            )

        combos_df = build_combinations_dataframe(
            outbound_flights=out_flights,
            inbound_flights=in_flights,
            min_stay_days=validated["min_stay_days"],
            max_stay_days=validated["max_stay_days"],
            window_start=validated["window_start"],
            window_end=validated["window_end"],
            expected_currency=validated["currency"],
            max_stops=validated["max_stops"],
            out_max_stops=validated["out_max_stops"],
            in_max_stops=validated["in_max_stops"],
            max_flight_hours=validated["max_flight_hours"],
            max_total_travel_hours=validated["max_total_travel_hours"],
            out_dep_min_time=validated["out_dep_min_time"],
            out_dep_max_time=validated["out_dep_max_time"],
            in_dep_min_time=validated["in_dep_min_time"],
            in_dep_max_time=validated["in_dep_max_time"],
            baggage_mode=validated["baggage_mode"],
            cabin_bag_fee=validated["cabin_bag_fee"],
            checked_bag_fee=validated["checked_bag_fee"],
            excluded_airlines=validated["excluded_airlines"],
            include_transfer=validated["include_transfer"],
            transfer_costs=validated["transfer_costs"],
            transfer_durations=validated["transfer_durations"],
        )

        if validated["only_asymmetric"] and not combos_df.empty:
            combos_df = combos_df[combos_df["is_asymmetric"]].reset_index(drop=True)

        combinations = (
            sanitize_records_for_json(combos_df.head(250).to_dict(orient="records"))
            if not combos_df.empty
            else []
        )

        return self._send_json(
            {
                "has_data": True,
                "needs_rescrape": cache_state["needs_rescrape"],
                "message": None,
                "currency": validated["currency"],
                "outbound_count": len(out_flights),
                "inbound_count": len(in_flights),
                "combinations_count": len(combos_df),
                "combinations": combinations,
                "available_airlines": available_airlines,
                "outbound_flights": sanitize_records_for_json([f.to_dict() for f in out_flights]),
                "inbound_flights": sanitize_records_for_json([f.to_dict() for f in in_flights]),
                "query_summary": q_summary,
                "cached_queries": get_today_cached_queries(),
                "cache_error": get_last_cache_error(),
                "client_request_id": validated["client_request_id"],
            }
        )

    def _handle_recheck(self, body: Dict[str, Any]):
        if not isinstance(body, dict):
            return self._send_json({"error": "Geçersiz istek."}, status=400)
        out_spec = body.get("outbound")
        in_spec = body.get("inbound")
        currency = str(body.get("currency", "TRY")).upper().strip()
        if not isinstance(out_spec, dict) or not isinstance(in_spec, dict):
            return self._send_json({"error": "Gidiş ve dönüş uçuş bilgileri (outbound, inbound) gereklidir."}, status=400)
        if currency not in VALID_CURRENCIES:
            return self._send_json({"error": "Geçersiz para birimi."}, status=400)

        try:
            result = recheck_combination(
                outbound_spec=out_spec,
                inbound_spec=in_spec,
                currency=currency,
                max_results_per_route=int(body.get("max_results_per_route", 20)),
                headless=True,
            )
            return self._send_json(result)
        except Exception as exc:
            return self._send_json({"error": f"Yeniden kontrol sırasında hata: {exc}"}, status=500)


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    server = ThreadingHTTPServer(("127.0.0.1", PORT), FlightFinderHandler)
    print(f"Smart Flight Finder calisiyor: http://localhost:{PORT}")
    server.serve_forever()
