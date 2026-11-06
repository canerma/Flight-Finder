import asyncio
import json
import os
import re
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Callable, Any, Tuple
from urllib.parse import quote_plus
from playwright.async_api import async_playwright, Page, BrowserContext, TimeoutError as PlaywrightTimeoutError

CACHE_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flight_cache.json")
CACHE_SCHEMA_VERSION = 2
CACHE_TTL_HOURS = 48

QUERY_STATUS_SUCCESS = "success"
QUERY_STATUS_NO_RESULTS = "no_results"
QUERY_STATUS_TIMEOUT = "timeout"
QUERY_STATUS_BLOCKED = "blocked"
QUERY_STATUS_PARSE_ERROR = "parse_error"

VALID_QUERY_STATUSES = {
    QUERY_STATUS_SUCCESS,
    QUERY_STATUS_NO_RESULTS,
    QUERY_STATUS_TIMEOUT,
    QUERY_STATUS_BLOCKED,
    QUERY_STATUS_PARSE_ERROR,
}

_CACHE_LOCK = threading.RLock()
_CACHE_EPOCH = 0
_LAST_CACHE_ERROR: Optional[str] = None


@dataclass
class FlightOption:
    origin: str
    destination: str
    date: str                                     # Kalkış yerel tarihi (YYYY-MM-DD)
    airline: str
    departure_time: Optional[str]                 # Örn: "08:35" veya doğrulanmadıysa None
    arrival_time: Optional[str]                   # Örn: "12:15" veya doğrulanmadıysa None
    duration: Optional[str]                       # Örn: "3 sa 40 dk" veya None
    duration_minutes: Optional[int]               # Doğrulanmadıysa None (asla 0 atanmaz)
    stops: Optional[int]                          # 0 = Direkt, 1 = 1 Aktarma, doğrulanmadıysa None
    stops_text: Optional[str]                     # Doğrulanmadıysa None
    price: float
    currency: str
    booking_url: str
    arrival_day_offset: Optional[int] = None      # 0 = aynı gün, 1 = +1 gün, doğrulanmadıysa None
    arrival_date: Optional[str] = None            # Varış yerel tarihi (YYYY-MM-DD) veya None
    cabin_baggage_included: Optional[bool] = None # Yalnızca kart metninden doğrulanırsa True/False, aksi halde None
    checked_baggage_included: Optional[bool] = None
    checked_baggage_kg: Optional[int] = None
    flight_number: Optional[str] = None
    layover_airports: Optional[List[str]] = None
    self_transfer: Optional[bool] = None
    segments: Optional[List[str]] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FlightOption":
        dep_date = str(data.get("date", ""))
        offset = data.get("arrival_day_offset", None)
        arr_date = data.get("arrival_date", None)
        if arr_date is None and offset is not None and dep_date:
            try:
                dt = datetime.strptime(dep_date, "%Y-%m-%d") + timedelta(days=int(offset))
                arr_date = dt.strftime("%Y-%m-%d")
            except Exception:
                arr_date = None
        return cls(
            origin=str(data.get("origin", "")).upper().strip(),
            destination=str(data.get("destination", "")).upper().strip(),
            date=dep_date,
            airline=str(data.get("airline") or "Doğrulanmadı"),
            departure_time=data.get("departure_time") or None,
            arrival_time=data.get("arrival_time") or None,
            duration=data.get("duration") or None,
            duration_minutes=int(data["duration_minutes"]) if data.get("duration_minutes") is not None else None,
            stops=int(data["stops"]) if data.get("stops") is not None else None,
            stops_text=data.get("stops_text") or None,
            price=float(data["price"]),
            currency=str(data.get("currency", "TRY")).upper().strip(),
            booking_url=str(data.get("booking_url", "")),
            arrival_day_offset=int(offset) if offset is not None else None,
            arrival_date=arr_date,
            cabin_baggage_included=data.get("cabin_baggage_included", None),
            checked_baggage_included=data.get("checked_baggage_included", None),
            checked_baggage_kg=int(data["checked_baggage_kg"]) if data.get("checked_baggage_kg") is not None else None,
            flight_number=data.get("flight_number") or None,
            layover_airports=data.get("layover_airports") or None,
            self_transfer=data.get("self_transfer", None),
            segments=data.get("segments") or None,
        )


@dataclass
class RouteQueryResult:
    origin: str
    destination: str
    date: str
    currency: str
    status: str                                   # success | no_results | timeout | blocked | parse_error
    flights: List[FlightOption]
    requested_limit: int
    collected_count: int
    total_dom_cards: int
    scope_complete: bool
    scope_note: str
    fetched_at: str
    error_message: Optional[str] = None
    from_cache: bool = False
    limit_reached: bool = False
    all_loaded_parsed: bool = False
    schema_version: int = CACHE_SCHEMA_VERSION

    def to_cache_entry(self) -> Dict[str, Any]:
        cached_date = (
            self.fetched_at.split("T")[0]
            if "T" in self.fetched_at and len(self.fetched_at) >= 10
            else _today_str()
        )
        return {
            "schema_version": self.schema_version,
            "origin": self.origin,
            "destination": self.destination,
            "date": self.date,
            "currency": self.currency,
            "status": self.status,
            "requested_limit": self.requested_limit,
            "collected_count": self.collected_count,
            "total_dom_cards": self.total_dom_cards,
            "scope_complete": self.scope_complete,
            "limit_reached": self.limit_reached,
            "all_loaded_parsed": self.all_loaded_parsed,
            "scope_note": self.scope_note,
            "cached_date": cached_date,
            "fetched_at": self.fetched_at,
            "scraped_at": self.fetched_at.split("T")[-1][:8] if "T" in self.fetched_at else self.fetched_at,
            "error_message": self.error_message,
            "flights": [f.to_dict() for f in self.flights],
        }


def build_google_flights_url(
    origin: str,
    destination: str,
    date_str: str,
    currency: str = "TRY",
    lang: str = "tr",
) -> str:
    query = f"Flights to {destination.upper().strip()} from {origin.upper().strip()} on {date_str} oneway"
    return (
        f"https://www.google.com/travel/flights"
        f"?q={quote_plus(query)}&curr={currency.upper().strip()}&hl={lang}"
    )


def parse_price(price_str: Optional[str]) -> Optional[float]:
    if not price_str:
        return None
    cleaned = re.sub(r"[^\d.,]", "", str(price_str).strip())
    if not cleaned:
        return None

    if "." in cleaned and "," in cleaned:
        if cleaned.rfind(".") < cleaned.rfind(","):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "." in cleaned:
        parts = cleaned.split(".")
        if len(parts[-1]) == 3:
            cleaned = cleaned.replace(".", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        if len(parts[-1]) == 3:
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(",", ".")

    try:
        val = float(cleaned)
        return val if val > 0 else None
    except ValueError:
        return None


def parse_duration_to_minutes(duration_str: Optional[str]) -> Optional[int]:
    """
    Süre metnini dakikaya çevirir. Okunamazsa veya geçersizse 0 yerine None döner.
    """
    if not duration_str or not str(duration_str).strip():
        return None
    text = str(duration_str)
    hr_match = re.search(r"(\d+)\s*(?:sa\.?|saat|hr\.?|hours?|h\b)", text, re.IGNORECASE)
    min_match = re.search(r"(\d+)\s*(?:dk\.?|dakika|min\.?|minutes?|m\b)", text, re.IGNORECASE)

    if not hr_match and not min_match:
        return None

    hours = int(hr_match.group(1)) if hr_match else 0
    minutes = int(min_match.group(1)) if min_match else 0
    total = hours * 60 + minutes
    return total if total > 0 else None


def parse_stops(stops_str: Optional[str]) -> Optional[int]:
    """
    Aktarma metnini sayıya çevirir. Okunamazsa veya doğrulanamazsa 0 yerine None döner.
    """
    if not stops_str or not str(stops_str).strip():
        return None
    lower = str(stops_str).strip().lower()
    if any(k in lower for k in ("aktarmasız", "nonstop", "non-stop", "direkt", "direct")):
        return 0
    match = re.search(r"(\d+)\s*(?:aktarma|stop)", lower)
    if match:
        return int(match.group(1))
    return None


def _parse_arrival_marker_snippet(snippet: Optional[str]) -> Tuple[Optional[int], bool]:
    """
    Varış saatine bağlı kısa metin parçasındaki gün farkı işaretini ayrıştırır.
    Dönüş: (day_offset, has_unparseable_marker)
    - Desteklenenler: +1, +2, +3, ertesi gün, sonraki gün, next day, N gün sonra, N days later, aynı gün, same day, +0
    - Ayrıştırılamayan veya çelişkili bir gün ifadesi varsa (None, True) döner (asla 0 varsayılmaz).
    """
    if not snippet or not snippet.strip():
        return None, False

    s = snippet.strip().lower()
    # Başka bir saate veya cümleye geçmeden önceki ilk 32 karakterlik varış bağlamını al
    s = re.split(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b|[;|•\n]", s)[0][:35].strip()
    if not s:
        return None, False

    found_offsets = set()
    unparseable = False

    # 1) +N veya -N işaretleri
    for m in re.finditer(r"([+-])\s*(\d+)\b", s):
        sign = m.group(1)
        val = int(m.group(2))
        if sign == "+" and val in (0, 1, 2, 3):
            found_offsets.add(val)
        else:
            unparseable = True

    # 2) Açık metinsel ifadeler (+1)
    if re.search(r"\b(?:ertesi\s+gün|ertesi\s+gun|sonraki\s+gün|sonraki\s+gun|next\s+day)\b", s):
        found_offsets.add(1)

    # 3) N gün sonra / N days later
    for m in re.finditer(r"\b(\d+)\s*(?:gün\s*sonra|gun\s*sonra|days?\s*later|gün\s*fark|gun\s*fark)\b", s):
        val = int(m.group(1))
        if val in (1, 2, 3):
            found_offsets.add(val)
        else:
            unparseable = True

    # 4) Açık aynı gün ifadeleri (0)
    if re.search(r"\b(?:aynı\s+gün|ayni\s+gun|same\s+day)\b", s):
        found_offsets.add(0)

    # 5) Desteklenmeyen / belirsiz gün ifadeleri (örn. önceki gün, farklı gün, N gün önce)
    if re.search(
        r"\b(?:önceki\s+gün|onceki\s+gun|previous\s+day|farklı\s+gün|farkli\s+gun|different\s+day|\d+\s*gün\s*önce|\d+\s*days?\s*ago)\b",
        s,
    ):
        unparseable = True

    if unparseable or len(found_offsets) > 1:
        return None, True
    if len(found_offsets) == 1:
        return next(iter(found_offsets)), False
    return None, False


def parse_time_and_day_offset(
    dep_raw: Optional[str],
    arr_raw: Optional[str],
    card_text: Optional[str] = None,
    aria_label: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """
    Kalkış saati, varış saati ve varış gün farkını (+1, +2 veya 0) çıkarır.
    Kurallar:
    - "ertesi gün", "sonraki gün", "next day", "+1", "+2" ve açık tarih işaretlerini ayrıştırır.
    - İşareti ilgili varış alanına bağlar; başka segmentin gün bilgisini kullanmaz.
    - Ayrıştırılamayan işareti sessizce aynı gün (0) kabul etmez -> None döner.
    - Sadece varış saatinin kalkış saatinden küçük olmasına bakarak +1 üretmez; açık işaret yoksa None döner.
    - Aynı gün (0) kararını yalnızca güvenilir DOM/kart bağlamı ve tutarlı saatlerle doğrular.
    """
    dep_clean: Optional[str] = None
    arr_clean: Optional[str] = None
    day_offset: Optional[int] = None
    has_unparseable_marker = False
    has_structured_dom_times = False

    if dep_raw:
        m_dep = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", str(dep_raw))
        if m_dep:
            dep_clean = f"{int(m_dep.group(1)):02d}:{m_dep.group(2)}"

    if arr_raw:
        m_arr = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b(.*)$", str(arr_raw).strip(), flags=re.DOTALL)
        if m_arr:
            arr_clean = f"{int(m_arr.group(1)):02d}:{m_arr.group(2)}"
            trailing_raw = m_arr.group(3) or ""
            off, bad = _parse_arrival_marker_snippet(trailing_raw)
            if bad:
                has_unparseable_marker = True
            elif off is not None:
                day_offset = off

    if dep_clean is not None and arr_clean is not None:
        has_structured_dom_times = True

    combined_text = f"{card_text or ''} {aria_label or ''}".strip()

    # Eğer dep_clean veya arr_clean henüz bulunmadıysa önce etiketli ("Kalkış:", "Varış:") alanları ara
    if (not dep_clean or not arr_clean) and combined_text:
        m_labeled_dep = re.search(
            r"(?:kalkış(?:\s+saati)?|departure(?:\s+time)?)\s*[:\-]?\s*\b([01]?\d|2[0-3]):([0-5]\d)\b",
            combined_text,
            flags=re.IGNORECASE,
        )
        m_labeled_arr = re.search(
            r"(?:varış(?:\s+saati)?|arrival(?:\s+time)?)\s*[:\-]?\s*\b([01]?\d|2[0-3]):([0-5]\d)\b([^,.;|\n]{0,35})",
            combined_text,
            flags=re.IGNORECASE,
        )
        if not dep_clean and m_labeled_dep:
            dep_clean = f"{int(m_labeled_dep.group(1)):02d}:{m_labeled_dep.group(2)}"
        if not arr_clean and m_labeled_arr:
            arr_clean = f"{int(m_labeled_arr.group(1)):02d}:{m_labeled_arr.group(2)}"
            off, bad = _parse_arrival_marker_snippet(m_labeled_arr.group(3))
            if bad:
                has_unparseable_marker = True
            elif off is not None and day_offset is None:
                day_offset = off

    # Hâlâ bulunmadıysa metindeki ardışık saat çiftini kontrol et
    if (not dep_clean or not arr_clean) and combined_text:
        matches = list(
            re.finditer(
                r"\b([01]?\d|2[0-3]):([0-5]\d)\b([^,.;|\n0-9]{0,25}(?:[+-]\s*\d+|\b[123]\s*(?:gün\s*sonra|days?\s*later)\b)?[^,.;|\n0-9]{0,15})",
                combined_text,
            )
        )
        unique_matches = []
        for m in matches:
            hhmm = f"{int(m.group(1)):02d}:{m.group(2)}"
            trail = m.group(3) or ""
            if not unique_matches or unique_matches[-1][0] != hhmm:
                unique_matches.append((hhmm, trail))
        if not dep_clean and len(unique_matches) >= 1:
            dep_clean = unique_matches[0][0]
        if not arr_clean and len(unique_matches) >= 2:
            arr_clean = unique_matches[1][0]
            off, bad = _parse_arrival_marker_snippet(unique_matches[1][1])
            if bad:
                has_unparseable_marker = True
            elif off is not None and day_offset is None:
                day_offset = off

    # Eğer arr_clean biliniyorsa, combined_text içinde özellikle bu varış saatine bağlı işareti kontrol et
    if arr_clean is not None and day_offset is None and not has_unparseable_marker and combined_text:
        # Önce "Varış: <arr_clean> <marker>" kalıbına bak
        labeled_arr_pat = re.compile(
            rf"(?:varış(?:\s+saati)?|arrival(?:\s+time)?)\s*[:\-]?\s*{re.escape(arr_clean)}\b([^,.;|\n]{{0,35}})",
            flags=re.IGNORECASE,
        )
        m_lab = labeled_arr_pat.search(combined_text)
        if m_lab:
            off, bad = _parse_arrival_marker_snippet(m_lab.group(1))
            if bad:
                has_unparseable_marker = True
            elif off is not None:
                day_offset = off

        # Yoksa dep_clean ... arr_clean diziliminde arr_clean'in hemen sonrasındaki parçaya bak
        if day_offset is None and not has_unparseable_marker:
            arr_occ_pat = re.compile(rf"\b{re.escape(arr_clean)}\b([^,.;|\n]{{0,35}})", flags=re.IGNORECASE)
            # Son (nihai varış) eşleşmesini değerlendir
            occ_matches = list(arr_occ_pat.finditer(combined_text))
            if occ_matches:
                last_trail = occ_matches[-1].group(1)
                off, bad = _parse_arrival_marker_snippet(last_trail)
                if bad:
                    has_unparseable_marker = True
                elif off is not None:
                    day_offset = off

    if has_unparseable_marker:
        return dep_clean, arr_clean, None

    # Açık bir gün farkı işareti yoksa, aynı gün (0) olup olmadığını doğrula:
    # - Hem kalkış hem varış saati bulunmalı
    # - Doğrulanabilir bir metin/DOM bağlamı (combined_text) olmalı
    # - Varış saati kalkış saatinden küçükse (örn. 23:00 -> 02:00) ve açık işaret yoksa
    #   saat dilimi / gece geçişi belirsizliği nedeniyle asla +1 veya 0 uydurma -> None bırak!
    if day_offset is None and dep_clean is not None and arr_clean is not None and combined_text:
        dep_mins = int(dep_clean[:2]) * 60 + int(dep_clean[3:5])
        arr_mins = int(arr_clean[:2]) * 60 + int(arr_clean[3:5])
        dur_mins = parse_duration_to_minutes(combined_text)

        if arr_mins >= dep_mins:
            # Eğer toplam süre biliniyorsa ve gece yarısını aşacak kadar uzunsa (örn. dep + dur >= 1440),
            # açık gün işareti olmadan aynı gün kabul etme
            if dur_mins is not None and (dep_mins + dur_mins >= 24 * 60 or dur_mins >= 18 * 60):
                day_offset = None
            elif has_structured_dom_times or re.search(
                r"(?:kalkış|varış|departure|arrival|aktarma|nonstop|direkt|sa\b|dk\b|hr\b|min\b)",
                combined_text,
                flags=re.IGNORECASE,
            ):
                day_offset = 0

    return dep_clean, arr_clean, day_offset


def compute_arrival_date(departure_date: str, arrival_day_offset: Optional[int]) -> Optional[str]:
    """
    Kalkış tarihi (YYYY-MM-DD) ve doğrulanmış gün farkı (arrival_day_offset) varsa
    varış yerel tarihini hesaplar. Gün farkı doğrulanmamışsa (None) uydurmaz, None döner.
    """
    if not departure_date or arrival_day_offset is None:
        return None
    try:
        dt = datetime.strptime(departure_date, "%Y-%m-%d") + timedelta(days=int(arrival_day_offset))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


NEGATIVE_BAGGAGE_PATTERNS = [
    r"\bkabin\s+bagaj[ıi]\s+(?:dahil\s+değil(?:dir)?|dahil\s+degil(?:dir)?|yok(?:tur)?|hariç(?:tir)?|haric(?:tir)?|ücretli(?:dir)?|ucretli(?:dir)?|ek\s+ücretli|bulunmaz)\b",
    r"\bücretsiz\s+kabin\s+bagaj[ıi]\s+(?:yok(?:tur)?|dahil\s+değil(?:dir)?|dahil\s+degil(?:dir)?|bulunmaz)\b",
    r"\bno\s+(?:free\s+)?carry[- ]?on(?:\s+bag(?:gage)?)?\b",
    r"\bcarry[- ]?on(?:\s+bag(?:gage)?)?\s+(?:not\s+included|is\s+not\s+included|excluded|for\s+a\s+fee|extra)\b",
    r"\boverhead\s+bin\s+(?:not\s+included|excluded|no\s+access)\b",
    r"\bpersonal\s+item\s+only\b",
    r"\bsadece\s+(?:kişisel\s+eşya|kisisel\s+esya|koltuk\s+alt[ıi](?:\s+çanta|\s+canta|\s+bagaj)?)\b",
    r"\byalnızca\s+(?:kişisel\s+eşya|kisisel\s+esya|koltuk\s+alt[ıi](?:\s+çanta|\s+canta|\s+bagaj)?)\b",
]

POSITIVE_BAGGAGE_PATTERNS = [
    r"\bücretsiz\s+kabin\s+bagaj[ıi](?!\s*(?:yok|dahil\s+değil|dahil\s+degil|hariç|haric|bulunmaz))\b",
    r"\bkabin\s+bagaj[ıi]\s+(?:dahil(?:dir)?|ücretsiz(?:dir)?)(?!\s*(?:değil|degil|yok))\b",
    r"\bfree\s+carry[- ]?on(?:\s+bag(?:gage)?)?(?!\s*(?:not\s+included|excluded))\b",
    r"\bcarry[- ]?on(?:\s+bag(?:gage)?)?\s+included\b",
    r"\boverhead\s+bin\s+included\b",
]


def extract_verified_baggage_flag(card_text: Optional[str], aria_label: Optional[str]) -> Optional[bool]:
    """
    Yalnızca kart metninde veya aria-label içinde açıkça kabin bagajı bilgisi varsa True/False döner.
    - Olumsuz ifadeleri önce yakalar ve maskeler ("Kabin bagajı dahil değil" içindeki "Kabin bagajı dahil"
      alt metnini olumlu kanıt saymaz).
    - Bağımsız olumlu ve olumsuz ifadeler bir arada çelişiyorsa None döner.
    - Bilgi yoksa None döner.
    """
    blob = re.sub(r"\s+", " ", f"{card_text or ''} {aria_label or ''}".lower()).strip()
    if not blob:
        return None

    has_negative = False
    masked_blob = blob
    for pat in NEGATIVE_BAGGAGE_PATTERNS:
        if re.search(pat, masked_blob, flags=re.IGNORECASE):
            has_negative = True
            masked_blob = re.sub(pat, " [NEG_BAG] ", masked_blob, flags=re.IGNORECASE)

    has_positive = False
    for pat in POSITIVE_BAGGAGE_PATTERNS:
        if re.search(pat, masked_blob, flags=re.IGNORECASE):
            has_positive = True
            break

    if has_positive and has_negative:
        return None
    if has_negative:
        return False
    if has_positive:
        return True
    return None


def extract_extra_metadata(
    card_text: Optional[str],
    aria_label: Optional[str],
    origin: str,
    destination: str,
) -> Tuple[Optional[str], Optional[List[str]], Optional[bool], Optional[List[str]]]:
    """
    Kart metninden (varsa) uçuş numarası, aktarma havalimanı kodları, self-transfer ve sıralı segment
    numaralarını çıkarır. Yoksa uydurmaz, None döner.
    """
    blob = f"{card_text or ''} {aria_label or ''}"
    if not blob.strip():
        return None, None, None, None

    # Uçuş numarası ve sıralı segmentler (örn. TK 1951, PC 1253, VF 53, LH 1305)
    fn_matches = re.findall(r"\b([A-Z0-9]{2})\s?(\d{2,4})\b", blob)
    flight_numbers: List[str] = []
    excluded_prefixes = {"SA", "DK", "HR", "TL", "EU", "US", "KG", "AM", "PM"}
    for prefix, num in fn_matches:
        if prefix not in excluded_prefixes and not prefix.isdigit():
            code = f"{prefix}{num}"
            if code not in flight_numbers:
                flight_numbers.append(code)
    flight_number = ", ".join(flight_numbers) if flight_numbers else None
    segments = list(flight_numbers) if flight_numbers else None

    # Aktarma havalimanı (örn. "SAW havalimanında 2 sa 15 dk", "1 stop in IST", "Stopover at FRA", "1 aktarma (SAW)")
    layover_codes: List[str] = []
    for m in re.finditer(
        r"\b([A-Z]{3})\b(?:\s+havalimanında|\s+üzerinden|\s+aktarmalı)|(?:stop(?:over)?\s+(?:in|at)\s+)([A-Z]{3})\b|(?:aktarma\s*\(\s*([A-Z]{3})\s*\))",
        blob,
    ):
        code = (m.group(1) or m.group(2) or m.group(3) or "").upper()
        if code and code not in (origin.upper(), destination.upper()) and code not in layover_codes:
            layover_codes.append(code)

    # Self-transfer kontrolü
    lower_blob = blob.lower()
    self_transfer: Optional[bool] = None
    if any(
        k in lower_blob
        for k in (
            "kendi aktarmanızı",
            "self-transfer",
            "self transfer",
            "ayrı bilet",
            "separate tickets",
        )
    ):
        self_transfer = True

    return flight_number, (layover_codes if layover_codes else None), self_transfer, segments


def detect_page_block_or_no_results(page_url: str, page_text: str) -> Optional[str]:
    """
    Sayfa URL'si ve metnine bakarak CAPTCHA/erişim engeli ('blocked') veya
    gerçek uçuş bulunamama ('no_results') durumunu tespit eder.
    """
    url_lower = (page_url or "").lower()
    text_lower = (page_text or "").lower()

    if (
        "/sorry/" in url_lower
        or "recaptcha" in text_lower
        or "unusual traffic" in text_lower
        or "sıra dışı trafik" in text_lower
        or "automated queries" in text_lower
    ):
        return QUERY_STATUS_BLOCKED

    no_result_phrases = (
        "aramanızla eşleşen uçuş bulunamadı",
        "hiçbir uçuş bulunamadı",
        "uygun uçuş bulunamadı",
        "no options matching your search",
        "no flights found",
        "we couldn't find any flights",
        "bu arama için sonuç bulunamadı",
    )
    if any(phrase in text_lower for phrase in no_result_phrases):
        return QUERY_STATUS_NO_RESULTS

    return None


def parse_raw_card_dicts(
    raw_cards: List[Dict[str, Any]],
    origin: str,
    destination: str,
    date_str: str,
    currency: str,
    booking_url: str,
) -> List[FlightOption]:
    """
    DOM'dan çekilen ham kart sözlüklerini doğrular, mükerrer kayıtları eler ve fiyata göre sıralar.
    """
    parsed: List[FlightOption] = []
    seen_signatures = set()
    orig_clean = origin.upper().strip()
    dest_clean = destination.upper().strip()
    curr_clean = currency.upper().strip()

    for item in raw_cards:
        try:
            price_val = parse_price(item.get("price_raw"))
            if price_val is None:
                continue

            card_text = item.get("card_text") or ""
            aria_label = item.get("aria_label") or ""

            dep_time, arr_time, day_offset = parse_time_and_day_offset(
                item.get("dep_time"),
                item.get("arr_time"),
                card_text=card_text,
                aria_label=aria_label,
            )

            arr_date: Optional[str] = None
            if day_offset is not None:
                try:
                    dt = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=int(day_offset))
                    arr_date = dt.strftime("%Y-%m-%d")
                except Exception:
                    arr_date = None

            airline_raw = (item.get("airline") or "").strip()
            airline = airline_raw if airline_raw else "Doğrulanmadı"

            duration_raw = (item.get("duration_text") or "").strip()
            duration_minutes = parse_duration_to_minutes(duration_raw)
            duration_text = duration_raw if duration_minutes is not None else None

            stops_raw = (item.get("stops_text") or "").strip()
            stops = parse_stops(stops_raw)
            stops_text = stops_raw if stops is not None else None

            cabin_bag = extract_verified_baggage_flag(card_text, aria_label)
            flight_num, layovers, self_tr, segments = extract_extra_metadata(
                card_text, aria_label, orig_clean, dest_clean
            )

            sig = (
                orig_clean,
                dest_clean,
                date_str,
                airline.lower(),
                dep_time or "",
                arr_time or "",
                stops if stops is not None else -1,
                round(price_val, 2),
                curr_clean,
            )
            if sig in seen_signatures:
                continue
            seen_signatures.add(sig)

            parsed.append(
                FlightOption(
                    origin=orig_clean,
                    destination=dest_clean,
                    date=date_str,
                    airline=airline,
                    departure_time=dep_time,
                    arrival_time=arr_time,
                    duration=duration_text,
                    duration_minutes=duration_minutes,
                    stops=stops,
                    stops_text=stops_text,
                    price=price_val,
                    currency=curr_clean,
                    booking_url=booking_url,
                    arrival_day_offset=day_offset,
                    arrival_date=arr_date,
                    cabin_baggage_included=cabin_bag,
                    flight_number=flight_num,
                    layover_airports=layovers,
                    self_transfer=self_tr,
                    segments=segments,
                )
            )
        except Exception:
            continue

    # Fiyata ve ardından (biliniyorsa) uçuş süresine göre sırala (bilinmeyen süreler sona)
    parsed.sort(
        key=lambda f: (
            f.price,
            f.duration_minutes if f.duration_minutes is not None else 10**9,
        )
    )
    return parsed


def _html_has_more_flights_button(html_content: str) -> bool:
    for m in re.finditer(r"<button[^>]*>(.*?)</button>", html_content or "", flags=re.DOTALL | re.IGNORECASE):
        btn_full = m.group(0).lower()
        if any(
            k in btn_full
            for k in (
                "daha fazla uçuş",
                "more flights",
                "diğer uçuşlar",
                "tüm uçuşları göster",
                "show more flights",
                "view more flights",
            )
        ) and not any(k in btn_full for k in ("daha az", "fewer")):
            return True
    return False


def parse_flights_from_html(
    html_content: str,
    origin: str,
    destination: str,
    date_str: str,
    currency: str,
    booking_url: str,
    requested_limit: int = 10,
    page_url: str = "https://www.google.com/travel/flights",
) -> RouteQueryResult:
    """
    Kaydedilmiş örnek HTML metni üzerinden uçuş kartlarını ve sayfa durumunu (success, no_results,
    blocked, parse_error) ayrıştırır. (Hem testler hem de çevrimdışı doğrulama için kullanılır).
    """
    now_iso = datetime.now().isoformat(timespec="seconds")
    text_only = re.sub(r"<[^>]+>", " ", html_content or "")
    text_only = re.sub(r"\s+", " ", text_only).strip()

    status_check = detect_page_block_or_no_results(page_url, text_only)
    if status_check == QUERY_STATUS_BLOCKED:
        return RouteQueryResult(
            origin=origin.upper().strip(),
            destination=destination.upper().strip(),
            date=date_str,
            currency=currency.upper().strip(),
            status=QUERY_STATUS_BLOCKED,
            flights=[],
            requested_limit=requested_limit,
            collected_count=0,
            total_dom_cards=0,
            scope_complete=False,
            scope_note="Erişim engeli / CAPTCHA tespit edildi.",
            fetched_at=now_iso,
            error_message="Google Flights CAPTCHA veya erişim engeli döndürdü.",
        )

    # li.pIav2d bloklarını yakala
    card_blocks = re.findall(
        r'<li[^>]*class="[^"]*\bpIav2d\b[^"]*"[^>]*>(.*?)</li>',
        html_content or "",
        flags=re.DOTALL | re.IGNORECASE,
    )
    total_dom_cards = len(card_blocks)

    if total_dom_cards == 0:
        if status_check == QUERY_STATUS_NO_RESULTS:
            return RouteQueryResult(
                origin=origin.upper().strip(),
                destination=destination.upper().strip(),
                date=date_str,
                currency=currency.upper().strip(),
                status=QUERY_STATUS_NO_RESULTS,
                flights=[],
                requested_limit=requested_limit,
                collected_count=0,
                total_dom_cards=0,
                scope_complete=True,
                limit_reached=False,
                all_loaded_parsed=True,
                scope_note="Sayfada bu rota/gün için uçuş olmadığı doğrulandı.",
                fetched_at=now_iso,
            )
        return RouteQueryResult(
            origin=origin.upper().strip(),
            destination=destination.upper().strip(),
            date=date_str,
            currency=currency.upper().strip(),
            status=QUERY_STATUS_PARSE_ERROR,
            flights=[],
            requested_limit=requested_limit,
            collected_count=0,
            total_dom_cards=0,
            scope_complete=False,
            scope_note="Uçuş kartı veya 'uçuş yok' mesajı doğrulanamadı.",
            fetched_at=now_iso,
            error_message="Sayfa yapısı doğrulanamadı (kart veya sonuç yok mesajı bulunamadı).",
        )

    raw_cards: List[Dict[str, Any]] = []
    valid_price_cards = 0
    for block in card_blocks:
        def _extract_tag_text(pattern: str) -> str:
            m = re.search(pattern, block, flags=re.DOTALL | re.IGNORECASE)
            if not m:
                return ""
            return re.sub(r"<[^>]+>", "", m.group(1)).strip()

        dep_t = _extract_tag_text(r'<span[^>]*aria-label="[^"]*(?:Kalkış saati|Departure time)[^"]*"[^>]*>(.*?)</span>')
        arr_t = _extract_tag_text(r'<span[^>]*aria-label="[^"]*(?:Varış saati|Arrival time)[^"]*"[^>]*>(.*?)</span>')
        airline_t = _extract_tag_text(r'<div[^>]*class="[^"]*\bsSHqwe\b[^"]*"[^>]*>(.*?)</div>')
        dur_t = _extract_tag_text(r'<div[^>]*class="[^"]*\bgvkrdb\b[^"]*"[^>]*>(.*?)</div>')
        stops_t = _extract_tag_text(r'<div[^>]*class="[^"]*\bEfT7Ae\b[^"]*"[^>]*>(.*?)</div>')
        price_t = _extract_tag_text(r'<div[^>]*class="[^"]*\bFpEdX\b[^"]*"[^>]*>(.*?)</div>')
        aria_matches = re.findall(r'aria-label="([^"]+)"', block, flags=re.IGNORECASE)
        aria_t = " ".join(aria_matches)
        c_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", block)).strip()

        if parse_price(price_t) is not None:
            valid_price_cards += 1

        raw_cards.append(
            {
                "dep_time": dep_t,
                "arr_time": arr_t,
                "airline": airline_t.splitlines()[0].strip() if airline_t else "",
                "duration_text": dur_t,
                "stops_text": stops_t,
                "price_raw": price_t,
                "card_text": c_text,
                "aria_label": aria_t,
            }
        )

    parsed_all = parse_raw_card_dicts(
        raw_cards=raw_cards,
        origin=origin,
        destination=destination,
        date_str=date_str,
        currency=currency,
        booking_url=booking_url,
    )

    if not parsed_all:
        return RouteQueryResult(
            origin=origin.upper().strip(),
            destination=destination.upper().strip(),
            date=date_str,
            currency=currency.upper().strip(),
            status=QUERY_STATUS_PARSE_ERROR,
            flights=[],
            requested_limit=requested_limit,
            collected_count=0,
            total_dom_cards=total_dom_cards,
            scope_complete=False,
            scope_note=f"Sayfada {total_dom_cards} kart bulundu ancak fiyat/uçuş bilgisi ayrıştırılamadı.",
            fetched_at=now_iso,
            error_message="Uçuş kartları ayrıştırılamadı (parse_error).",
        )

    more_btn_remaining = _html_has_more_flights_button(html_content)
    limit_reached = len(parsed_all) >= requested_limit
    all_loaded_parsed = (valid_price_cards == total_dom_cards) and len(parsed_all) > 0
    scope_complete = (not more_btn_remaining) and all_loaded_parsed
    scope_note = (
        f"Sayfadaki {total_dom_cards} karttan {len(parsed_all)} tekil uçuş doğrulandı "
        f"(tüm sonuçlar yüklendi: {'evet' if scope_complete else 'hayır'})."
    )
    return RouteQueryResult(
        origin=origin.upper().strip(),
        destination=destination.upper().strip(),
        date=date_str,
        currency=currency.upper().strip(),
        status=QUERY_STATUS_SUCCESS,
        flights=parsed_all,
        requested_limit=requested_limit,
        collected_count=len(parsed_all),
        total_dom_cards=total_dom_cards,
        scope_complete=scope_complete,
        limit_reached=limit_reached,
        all_loaded_parsed=all_loaded_parsed,
        scope_note=scope_note,
        fetched_at=now_iso,
    )


async def _handle_cookie_consent(page: Page) -> None:
    try:
        if "consent.google.com" in page.url:
            consent_buttons = [
                'button:has-text("Tümünü kabul et")',
                'button:has-text("Accept all")',
                'button:has-text("Kabul ediyorum")',
                'button[aria-label*="Accept"]',
                'form[action*="consent.google.com"] button',
            ]
            for selector in consent_buttons:
                btn = page.locator(selector).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                    break
    except Exception:
        pass


async def _extract_query_result_from_page(
    page: Page,
    origin: str,
    destination: str,
    date_str: str,
    currency: str,
    booking_url: str,
    requested_limit: int = 10,
    max_expansions: int = 3,
    expansion_timeout_ms: int = 1200,
) -> RouteQueryResult:
    """
    Canlı Playwright sayfasından tüm yüklenmiş uçuş kartlarını toplar, doğrular,
    ve durumu (success, no_results, timeout, blocked, parse_error) açıkça sınıflandırır.
    - "Daha fazla uçuş" butonuna tıklama ve kart okuma adımlarını birbirinden ayırır.
    - Tıklamadan sonra kart sayısının artmasını veya butonun kaybolmasını sınırlı timeout ile bekler.
    - limit_reached, all_loaded_parsed ve scope_complete kavramlarını ayrı tutar.
    """
    now_iso = datetime.now().isoformat(timespec="seconds")
    orig_clean = origin.upper().strip()
    dest_clean = destination.upper().strip()
    curr_clean = currency.upper().strip()

    selector_timed_out = False
    try:
        await page.wait_for_selector("li.pIav2d", timeout=7500)
    except PlaywrightTimeoutError:
        selector_timed_out = True
    except Exception:
        selector_timed_out = True

    expansion_timed_out = False
    expansions_done = 0

    # Aşama 1: Varsa "Daha fazla uçuş" butonuna sınırlı sayıda tıkla ve asenkron yüklemeyi bekle
    try:
        for _ in range(max_expansions):
            state_before = await page.evaluate(
                """() => {
                    const cards = document.querySelectorAll('li.pIav2d');
                    const moreBtns = Array.from(document.querySelectorAll('button')).filter(b => {
                        const t = (b.innerText || b.getAttribute('aria-label') || '').toLowerCase();
                        return (
                            t.includes('daha fazla uçuş') ||
                            t.includes('more flights') ||
                            t.includes('diğer uçuşlar') ||
                            t.includes('tüm uçuşları göster') ||
                            t.includes('show more flights') ||
                            t.includes('view more flights')
                        ) && !t.includes('daha az') && !t.includes('fewer');
                    });
                    if (moreBtns.length === 0) {
                        return { clicked: false, prev_count: cards.length };
                    }
                    try {
                        moreBtns[0].click();
                        return { clicked: true, prev_count: cards.length };
                    } catch (e) {
                        return { clicked: false, prev_count: cards.length, click_error: true };
                    }
                }"""
            )
            if not state_before or not state_before.get("clicked"):
                if state_before and state_before.get("click_error"):
                    expansion_timed_out = True
                break

            prev_count = int(state_before.get("prev_count", 0))
            try:
                await page.wait_for_function(
                    """(prevCount) => {
                        const curCount = document.querySelectorAll('li.pIav2d').length;
                        if (curCount > prevCount) return true;
                        if (document.body && document.body.getAttribute('data-all-flights-loaded') === 'true') return true;
                        const moreBtns = Array.from(document.querySelectorAll('button')).filter(b => {
                            const t = (b.innerText || b.getAttribute('aria-label') || '').toLowerCase();
                            return (
                                t.includes('daha fazla uçuş') ||
                                t.includes('more flights') ||
                                t.includes('diğer uçuşlar') ||
                                t.includes('tüm uçuşları göster') ||
                                t.includes('show more flights') ||
                                t.includes('view more flights')
                            ) && !t.includes('daha az') && !t.includes('fewer');
                        });
                        return moreBtns.length === 0;
                    }""",
                    arg=prev_count,
                    timeout=expansion_timeout_ms,
                )
                expansions_done += 1
                await page.wait_for_timeout(80)
            except PlaywrightTimeoutError:
                expansion_timed_out = True
                break
            except Exception:
                expansion_timed_out = True
                break
    except Exception:
        expansion_timed_out = True

    # Aşama 2: Yüklenmiş kartları ve sayfa durumunu ayrı bir adımda oku
    try:
        page_info = await page.evaluate(
            """() => {
                const moreBtns = Array.from(document.querySelectorAll('button')).filter(b => {
                    const t = (b.innerText || b.getAttribute('aria-label') || '').toLowerCase();
                    return (
                        t.includes('daha fazla uçuş') ||
                        t.includes('more flights') ||
                        t.includes('diğer uçuşlar') ||
                        t.includes('tüm uçuşları göster') ||
                        t.includes('show more flights') ||
                        t.includes('view more flights')
                    ) && !t.includes('daha az') && !t.includes('fewer');
                });
                const cards = Array.from(document.querySelectorAll('li.pIav2d'));
                const extracted = cards.map(card => {
                    const depEl = card.querySelector('span[aria-label*="Kalkış saati"], span[aria-label*="Departure time"]');
                    const arrEl = card.querySelector('span[aria-label*="Varış saati"], span[aria-label*="Arrival time"]');
                    const airlineEl = card.querySelector('div.sSHqwe.tPgKwe.ogfYpf span') || card.querySelector('div.sSHqwe');
                    const durEl = card.querySelector('div.gvkrdb.AdWm1c.tPgKwe.ogfYpf') || card.querySelector('div.gvkrdb');
                    const stopsEl = card.querySelector('div.EfT7Ae.AdWm1c.tPgKwe span.ogfYpf') || card.querySelector('div.EfT7Ae');
                    const priceEl = card.querySelector('div.YMlIz.FpEdX span') || card.querySelector('div.FpEdX');
                    const ariaEl = card.querySelector('[aria-label]');
                    return {
                        dep_time: depEl ? depEl.innerText.trim() : '',
                        arr_time: arrEl ? arrEl.innerText.trim() : '',
                        airline: airlineEl ? airlineEl.innerText.split('\\n')[0].trim() : '',
                        duration_text: durEl ? durEl.innerText.trim() : '',
                        stops_text: stopsEl ? stopsEl.innerText.trim() : '',
                        price_raw: priceEl ? priceEl.innerText.trim() : '',
                        card_text: card.innerText || '',
                        aria_label: (card.getAttribute('aria-label') || (ariaEl ? ariaEl.getAttribute('aria-label') : '') || '')
                    };
                });
                return {
                    url: window.location.href,
                    body_text: document.body ? document.body.innerText.slice(0, 6000) : '',
                    total_dom_cards: cards.length,
                    more_button_remaining: moreBtns.length > 0,
                    cards: extracted
                };
            }"""
        )
    except Exception as exc:
        return RouteQueryResult(
            origin=orig_clean,
            destination=dest_clean,
            date=date_str,
            currency=curr_clean,
            status=QUERY_STATUS_PARSE_ERROR,
            flights=[],
            requested_limit=requested_limit,
            collected_count=0,
            total_dom_cards=0,
            scope_complete=False,
            scope_note="Sayfa DOM okuma hatası.",
            fetched_at=now_iso,
            error_message=str(exc),
        )

    page_url = page_info.get("url", page.url)
    body_text = page_info.get("body_text", "")
    total_dom_cards = int(page_info.get("total_dom_cards", 0))
    more_button_remaining = bool(page_info.get("more_button_remaining", False))
    raw_cards = page_info.get("cards", [])

    block_or_empty = detect_page_block_or_no_results(page_url, body_text)
    if block_or_empty == QUERY_STATUS_BLOCKED:
        return RouteQueryResult(
            origin=orig_clean,
            destination=dest_clean,
            date=date_str,
            currency=curr_clean,
            status=QUERY_STATUS_BLOCKED,
            flights=[],
            requested_limit=requested_limit,
            collected_count=0,
            total_dom_cards=total_dom_cards,
            scope_complete=False,
            scope_note="Google Flights CAPTCHA / erişim engeli tespit edildi.",
            fetched_at=now_iso,
            error_message="CAPTCHA veya erişim engeli algılandı. Lütfen daha sonra tekrar deneyin.",
        )

    if total_dom_cards == 0:
        if block_or_empty == QUERY_STATUS_NO_RESULTS:
            return RouteQueryResult(
                origin=orig_clean,
                destination=dest_clean,
                date=date_str,
                currency=curr_clean,
                status=QUERY_STATUS_NO_RESULTS,
                flights=[],
                requested_limit=requested_limit,
                collected_count=0,
                total_dom_cards=0,
                scope_complete=True,
                limit_reached=False,
                all_loaded_parsed=True,
                scope_note="Sayfada bu rota/tarih için uygun uçuş olmadığı doğrulandı.",
                fetched_at=now_iso,
            )
        status = QUERY_STATUS_TIMEOUT if selector_timed_out else QUERY_STATUS_PARSE_ERROR
        return RouteQueryResult(
            origin=orig_clean,
            destination=dest_clean,
            date=date_str,
            currency=curr_clean,
            status=status,
            flights=[],
            requested_limit=requested_limit,
            collected_count=0,
            total_dom_cards=0,
            scope_complete=False,
            scope_note="Uçuş listesi zaman aşımına uğradı veya doğrulanamadı.",
            fetched_at=now_iso,
            error_message="Uçuş kartları yüklenemedi veya 'sonuç yok' durumu doğrulanamadı.",
        )

    valid_price_cards = sum(1 for item in raw_cards if parse_price(item.get("price_raw")) is not None)
    parsed_all = parse_raw_card_dicts(
        raw_cards=raw_cards,
        origin=orig_clean,
        destination=dest_clean,
        date_str=date_str,
        currency=curr_clean,
        booking_url=booking_url,
    )

    if not parsed_all:
        return RouteQueryResult(
            origin=orig_clean,
            destination=dest_clean,
            date=date_str,
            currency=curr_clean,
            status=QUERY_STATUS_PARSE_ERROR,
            flights=[],
            requested_limit=requested_limit,
            collected_count=0,
            total_dom_cards=total_dom_cards,
            scope_complete=False,
            scope_note=f"{total_dom_cards} DOM kartı bulundu fakat geçerli fiyat/uçuş okunamadı.",
            fetched_at=now_iso,
            error_message="DOM kartları ayrıştırılamadı (parse_error).",
        )

    # Üç kavramı birbirinden ayır:
    # 1) limit_reached: İstenen sayıya ulaşıldı mı?
    # 2) all_loaded_parsed: Yüklenen tüm DOM kartlarının fiyatı geçerli şekilde ayrıştırıldı mı?
    # 3) scope_complete: Sayfadaki tüm sonuçlar eksiksiz yüklendi ve ayrıştırıldı mı?
    limit_reached = len(parsed_all) >= requested_limit
    all_loaded_parsed = (valid_price_cards == total_dom_cards) and len(parsed_all) > 0
    scope_complete = (
        (not expansion_timed_out)
        and (not more_button_remaining)
        and all_loaded_parsed
    )

    if scope_complete:
        scope_note = (
            f"Sayfadaki tüm sonuçlar yüklendi ({total_dom_cards} DOM kartından {len(parsed_all)} tekil uçuş doğrulandı)."
        )
    else:
        reasons = []
        if expansion_timed_out:
            reasons.append("'Daha fazla uçuş' genişletmesi zaman aşımına uğradı")
        if more_button_remaining:
            reasons.append("sayfada yüklenmemiş ek uçuşlar var")
        if not all_loaded_parsed:
            reasons.append(f"{total_dom_cards - valid_price_cards} kart ayrıştırılamadı")
        reason_str = ", ".join(reasons) if reasons else "kısmi yükleme"
        scope_note = (
            f"Kısmi kapsam ({reason_str}): Yüklenen {total_dom_cards} karttan {len(parsed_all)} tekil uçuş toplandı."
        )

    return RouteQueryResult(
        origin=orig_clean,
        destination=dest_clean,
        date=date_str,
        currency=curr_clean,
        status=QUERY_STATUS_SUCCESS,
        flights=parsed_all,
        requested_limit=requested_limit,
        collected_count=len(parsed_all),
        total_dom_cards=total_dom_cards,
        scope_complete=scope_complete,
        limit_reached=limit_reached,
        all_loaded_parsed=all_loaded_parsed,
        scope_note=scope_note,
        fetched_at=now_iso,
    )


async def scrape_route_on_date_result(
    context: BrowserContext,
    origin: str,
    destination: str,
    date_str: str,
    currency: str = "TRY",
    max_results_per_route: int = 10,
    semaphore: Optional[asyncio.Semaphore] = None,
    max_retries: int = 1,
) -> RouteQueryResult:
    """
    Belirli bir (Kalkış -> Varış, Tarih) için tek yön uçuşları çeker ve RouteQueryResult döner.
    Geçici hatalarda (timeout, parse_error) sınırlı sayıda (max_retries) yeniden deneme yapar;
    CAPTCHA/blocked durumunda yeniden denemez.
    """
    orig_clean = origin.upper().strip()
    dest_clean = destination.upper().strip()
    curr_clean = currency.upper().strip()
    url = build_google_flights_url(orig_clean, dest_clean, date_str, currency=curr_clean)

    async def _single_attempt() -> RouteQueryResult:
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            await _handle_cookie_consent(page)
            return await _extract_query_result_from_page(
                page=page,
                origin=orig_clean,
                destination=dest_clean,
                date_str=date_str,
                currency=curr_clean,
                booking_url=url,
                requested_limit=max_results_per_route,
            )
        except PlaywrightTimeoutError as exc:
            return RouteQueryResult(
                origin=orig_clean,
                destination=dest_clean,
                date=date_str,
                currency=curr_clean,
                status=QUERY_STATUS_TIMEOUT,
                flights=[],
                requested_limit=max_results_per_route,
                collected_count=0,
                total_dom_cards=0,
                scope_complete=False,
                scope_note="Sayfa yükleme zaman aşımına uğradı.",
                fetched_at=datetime.now().isoformat(timespec="seconds"),
                error_message=f"Zaman aşımı: {exc}",
            )
        except Exception as exc:
            return RouteQueryResult(
                origin=orig_clean,
                destination=dest_clean,
                date=date_str,
                currency=curr_clean,
                status=QUERY_STATUS_PARSE_ERROR,
                flights=[],
                requested_limit=max_results_per_route,
                collected_count=0,
                total_dom_cards=0,
                scope_complete=False,
                scope_note="Beklenmeyen tarayıcı hatası.",
                fetched_at=datetime.now().isoformat(timespec="seconds"),
                error_message=str(exc),
            )
        finally:
            await page.close()

    async def _task_with_retry() -> RouteQueryResult:
        last_res: Optional[RouteQueryResult] = None
        for attempt in range(max_retries + 1):
            last_res = await _single_attempt()
            if last_res.status in (
                QUERY_STATUS_SUCCESS,
                QUERY_STATUS_NO_RESULTS,
                QUERY_STATUS_BLOCKED,
            ):
                return last_res
            if attempt < max_retries:
                await asyncio.sleep(0.8)
        assert last_res is not None
        return last_res

    if semaphore:
        async with semaphore:
            return await _task_with_retry()
    return await _task_with_retry()


async def scrape_route_on_date(
    context: BrowserContext,
    origin: str,
    destination: str,
    date_str: str,
    currency: str = "TRY",
    max_results_per_route: int = 10,
    semaphore: Optional[asyncio.Semaphore] = None,
) -> List[FlightOption]:
    res = await scrape_route_on_date_result(
        context=context,
        origin=origin,
        destination=destination,
        date_str=date_str,
        currency=currency,
        max_results_per_route=max_results_per_route,
        semaphore=semaphore,
    )
    return list(res.flights)


def generate_date_range(start_date: str, end_date: str) -> List[str]:
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    if end_dt < start_dt:
        raise ValueError("Bitiş tarihi başlangıç tarihinden önce olamaz.")

    days = (end_dt - start_dt).days + 1
    return [(start_dt + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _cache_key(origin: str, destination: str, date_str: str, currency: str) -> str:
    return f"{origin.upper().strip()}_{destination.upper().strip()}_{date_str}_{currency.upper().strip()}"


def get_cache_epoch() -> int:
    with _CACHE_LOCK:
        return _CACHE_EPOCH


def get_last_cache_error() -> Optional[str]:
    with _CACHE_LOCK:
        return _LAST_CACHE_ERROR


def is_cache_entry_usable(entry: Optional[Dict[str, Any]], requested_limit: int) -> bool:
    """
    Önbellek kaydının geçerli bir şemaya sahip olup olmadığını, başarılı veya doğrulanmış
    'no_results' durumunda olduğunu ve istenen sonuç limitini karşıladığını kontrol eder.
    Hatalı sorgular (timeout, blocked, parse_error) veya eski şemalı/düşük kapsamlı kayıtlar
    asla yeterli kabul edilmez.
    """
    if not entry or not isinstance(entry, dict):
        return False
    if entry.get("schema_version") != CACHE_SCHEMA_VERSION:
        return False
    status = entry.get("status")
    if status not in (QUERY_STATUS_SUCCESS, QUERY_STATUS_NO_RESULTS):
        return False
    if status == QUERY_STATUS_NO_RESULTS:
        return True

    scope_complete = bool(entry.get("scope_complete", False))
    cached_req_limit = int(entry.get("requested_limit", 0) or 0)
    collected_count = int(entry.get("collected_count", len(entry.get("flights", []))) or 0)
    if scope_complete or requested_limit <= cached_req_limit or requested_limit <= collected_count:
        return True
    return False


def _extract_entry_day(entry: Dict[str, Any], fallback_day: Optional[str] = None) -> str:
    """
    Önbellek kaydının ait olduğu yerel günü (YYYY-MM-DD) çıkarır.
    """
    cdate = str(entry.get("cached_date") or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", cdate):
        return cdate
    fetched = str(entry.get("fetched_at") or "").strip()
    if len(fetched) >= 10 and re.match(r"^\d{4}-\d{2}-\d{2}", fetched[:10]):
        return fetched[:10]
    if fallback_day and re.match(r"^\d{4}-\d{2}-\d{2}$", str(fallback_day).strip()):
        return str(fallback_day).strip()
    return _today_str()


def _parse_entry_datetime(entry: Dict[str, Any], fallback_day: Optional[str] = None) -> Optional[datetime]:
    fetched = str(entry.get("fetched_at") or "").strip()
    if fetched:
        try:
            return datetime.fromisoformat(fetched[:19])
        except Exception:
            pass
    day_str = _extract_entry_day(entry, fallback_day=fallback_day)
    scraped_at = str(entry.get("scraped_at") or "12:00:00").strip()[:8]
    try:
        return datetime.fromisoformat(f"{day_str}T{scraped_at}")
    except Exception:
        try:
            return datetime.strptime(day_str, "%Y-%m-%d")
        except Exception:
            return None


def _is_entry_within_ttl(
    entry: Dict[str, Any],
    fallback_day: Optional[str] = None,
    now_dt: Optional[datetime] = None,
    ttl_hours: int = CACHE_TTL_HOURS,
) -> bool:
    """
    Önbellek kaydının son 48 saat (CACHE_TTL_HOURS) içinde oluşturulup oluşturulmadığını kontrol eder.
    """
    ref_now = now_dt or datetime.now()
    dt = _parse_entry_datetime(entry, fallback_day=fallback_day)
    if dt is None:
        return False
    age_seconds = (ref_now - dt).total_seconds()
    return age_seconds <= (ttl_hours * 3600)


def load_daily_cache(cache_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Önbellek dosyasını okur ve son 48 saat (CACHE_TTL_HOURS) içindeki geçerli kayıtları korur.
    48 saatten eski kayıtlar otomatik olarak ayıklanır.
    """
    target_path = cache_path or CACHE_FILE_PATH
    today = _today_str()
    now_dt = datetime.now()
    with _CACHE_LOCK:
        if not os.path.exists(target_path):
            return {
                "cache_day": today,
                "ttl_hours": CACHE_TTL_HOURS,
                "schema_version": CACHE_SCHEMA_VERSION,
                "entries": {},
            }
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            file_day = data.get("cache_day") or today
            raw_entries = data.get("entries", {})
            if not isinstance(raw_entries, dict):
                raw_entries = {}

            valid_entries: Dict[str, Any] = {}
            for k, val in raw_entries.items():
                if not isinstance(val, dict):
                    continue
                if _is_entry_within_ttl(val, fallback_day=file_day, now_dt=now_dt, ttl_hours=CACHE_TTL_HOURS):
                    if "cached_date" not in val:
                        val["cached_date"] = _extract_entry_day(val, fallback_day=file_day)
                    valid_entries[k] = val

            data["cache_day"] = today
            data["ttl_hours"] = CACHE_TTL_HOURS
            data["schema_version"] = CACHE_SCHEMA_VERSION
            data["entries"] = valid_entries
            return data
        except Exception as exc:
            global _LAST_CACHE_ERROR
            _LAST_CACHE_ERROR = f"Önbellek okuma hatası: {exc}"
            return {
                "cache_day": today,
                "ttl_hours": CACHE_TTL_HOURS,
                "schema_version": CACHE_SCHEMA_VERSION,
                "entries": {},
            }


def save_daily_cache(cache_data: Dict[str, Any], cache_path: Optional[str] = None) -> None:
    """
    Önbelleği geçici dosyaya yazıp atomik olarak (os.replace) hedef dosyayla değiştirir.
    Kaydetme hatası oluşursa _LAST_CACHE_ERROR içine kaydeder ve görünür kılar.
    """
    global _LAST_CACHE_ERROR
    target_path = cache_path or CACHE_FILE_PATH
    with _CACHE_LOCK:
        tmp_path = f"{target_path}.tmp.{os.getpid()}.{threading.get_ident()}"
        try:
            cache_data["schema_version"] = CACHE_SCHEMA_VERSION
            cache_data["ttl_hours"] = CACHE_TTL_HOURS
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, target_path)
            _LAST_CACHE_ERROR = None
        except Exception as exc:
            _LAST_CACHE_ERROR = f"Önbellek kaydetme hatası: {exc}"
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            raise


def update_single_cache_entry(
    key: str,
    entry_dict: Dict[str, Any],
    expected_epoch: Optional[int] = None,
    cache_path: Optional[str] = None,
) -> bool:
    """
    Tek bir sorgu sonucunu atomik olarak önbelleğe yazar.
    Eğer tarama sürerken clear_daily_cache() çağrıldıysa (expected_epoch != _CACHE_EPOCH),
    eski önbellek verilerini geri getirmez; yalnızca temizleme sonrası yeni kaydı veya iptali uygular.
    """
    with _CACHE_LOCK:
        if expected_epoch is not None and expected_epoch != _CACHE_EPOCH:
            return False
        current = load_daily_cache(cache_path=cache_path)
        if "cached_date" not in entry_dict:
            entry_dict["cached_date"] = _extract_entry_day(entry_dict, fallback_day=_today_str())
        current["entries"][key] = entry_dict
        save_daily_cache(current, cache_path=cache_path)
        return True


def clear_daily_cache(cache_path: Optional[str] = None, keep_today: bool = False) -> Dict[str, int]:
    """
    keep_today=True ise içinde bulunduğumuz gün (_today_str()) hariç diğer tüm önbellek
    kayıtlarını siler. keep_today=False (varsayılan) ise tüm önbelleği sıfırlar.
    """
    global _CACHE_EPOCH
    with _CACHE_LOCK:
        today = _today_str()
        current = load_daily_cache(cache_path=cache_path)
        entries = current.get("entries", {})
        if not keep_today:
            removed_count = len(entries)
            _CACHE_EPOCH += 1
            save_daily_cache(
                {
                    "cache_day": today,
                    "ttl_hours": CACHE_TTL_HOURS,
                    "schema_version": CACHE_SCHEMA_VERSION,
                    "entries": {},
                },
                cache_path=cache_path,
            )
            return {"removed_count": removed_count, "kept_count": 0}

        kept_entries: Dict[str, Any] = {}
        removed_count = 0
        for k, val in entries.items():
            if not isinstance(val, dict):
                removed_count += 1
                continue
            entry_day = _extract_entry_day(val, fallback_day=today)
            if entry_day == today:
                kept_entries[k] = val
            else:
                removed_count += 1

        _CACHE_EPOCH += 1
        save_daily_cache(
            {
                "cache_day": today,
                "ttl_hours": CACHE_TTL_HOURS,
                "schema_version": CACHE_SCHEMA_VERSION,
                "entries": kept_entries,
            },
            cache_path=cache_path,
        )
        return {"removed_count": removed_count, "kept_count": len(kept_entries)}


def clear_cache_except_today(cache_path: Optional[str] = None) -> Dict[str, int]:
    """
    İçinde bulunduğumuz gün (bugün) taranmış kayıtları korur; önceki günlere ait tüm kayıtları siler.
    """
    return clear_daily_cache(cache_path=cache_path, keep_today=True)


def get_today_cached_queries(cache_path: Optional[str] = None) -> List[Dict[str, Any]]:
    data = load_daily_cache(cache_path=cache_path)
    entries = data.get("entries", {})
    today = _today_str()
    summary = []
    for key, val in entries.items():
        if not isinstance(val, dict):
            continue
        status = val.get("status", QUERY_STATUS_SUCCESS if val.get("schema_version") == CACHE_SCHEMA_VERSION else "legacy")
        cached_date = _extract_entry_day(val, fallback_day=today)
        summary.append(
            {
                "key": key,
                "origin": val.get("origin", ""),
                "destination": val.get("destination", ""),
                "date": val.get("date", ""),
                "currency": val.get("currency", "TRY"),
                "status": status,
                "usable": is_cache_entry_usable(val, int(val.get("requested_limit", 1) or 1)),
                "requested_limit": val.get("requested_limit", 0),
                "collected_count": val.get("collected_count", len(val.get("flights", []))),
                "flight_count": len(val.get("flights", [])),
                "cached_date": cached_date,
                "is_today": cached_date == today,
                "fetched_at": val.get("fetched_at", ""),
                "scraped_at": val.get("scraped_at", ""),
                "error_message": val.get("error_message"),
            }
        )
    summary.sort(key=lambda x: (x["origin"], x["destination"], x["date"]))
    return summary


async def _launch_browser(p, headless: bool = True):
    try:
        return await p.chromium.launch(headless=headless)
    except Exception:
        for channel in ("msedge", "chrome"):
            try:
                return await p.chromium.launch(channel=channel, headless=headless)
            except Exception:
                continue
        raise


async def scrape_one_way_flights_async(
    origins: List[str],
    destinations: List[str],
    start_date: str,
    end_date: str,
    currency: str = "TRY",
    max_results_per_route: int = 10,
    max_concurrent_pages: int = 3,
    headless: bool = True,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    bypass_cache: bool = False,
    cache_path: Optional[str] = None,
) -> List[FlightOption]:
    dates = generate_date_range(start_date, end_date)
    tasks_meta = [
        (orig.upper().strip(), dest.upper().strip(), d)
        for orig in origins
        for dest in destinations
        if orig.upper().strip() != dest.upper().strip()
        for d in dates
    ]

    total_tasks = len(tasks_meta)
    if total_tasks == 0:
        return []

    start_epoch = get_cache_epoch()
    cache_data = load_daily_cache(cache_path=cache_path)
    entries = cache_data["entries"]

    all_flights: List[FlightOption] = []
    uncached_tasks = []
    completed = 0

    for orig, dest, d in tasks_meta:
        ckey = _cache_key(orig, dest, d, currency)
        entry = entries.get(ckey)
        if not bypass_cache and is_cache_entry_usable(entry, max_results_per_route):
            cached_list = [FlightOption.from_dict(item) for item in entry.get("flights", [])]
            all_flights.extend(cached_list)
            completed += 1
            if progress_callback:
                progress_callback(completed, total_tasks, f"[Önbellekten ⚡] {orig} -> {dest} ({d})")
        else:
            uncached_tasks.append((orig, dest, d))

    if not uncached_tasks:
        return all_flights

    semaphore = asyncio.Semaphore(max_concurrent_pages)

    async with async_playwright() as p:
        browser = await _launch_browser(p, headless=headless)
        context = await browser.new_context(
            locale="tr-TR",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )

        async def _wrapped_scrape(orig: str, dest: str, d: str) -> List[FlightOption]:
            nonlocal completed
            qres = await scrape_route_on_date_result(
                context=context,
                origin=orig,
                destination=dest,
                date_str=d,
                currency=currency,
                max_results_per_route=max_results_per_route,
                semaphore=semaphore,
            )
            ckey = _cache_key(orig, dest, d, currency)
            update_single_cache_entry(
                ckey,
                qres.to_cache_entry(),
                expected_epoch=start_epoch,
                cache_path=cache_path,
            )

            completed += 1
            if progress_callback:
                progress_callback(
                    completed,
                    total_tasks,
                    f"{orig} -> {dest} ({d}) [{qres.status}]",
                )
            return list(qres.flights)

        results = await asyncio.gather(
            *[_wrapped_scrape(orig, dest, d) for orig, dest, d in uncached_tasks]
        )

        for route_flights in results:
            all_flights.extend(route_flights)

        await context.close()
        await browser.close()

    return all_flights


def scrape_one_way_flights(
    origins: List[str],
    destinations: List[str],
    start_date: str,
    end_date: str,
    currency: str = "TRY",
    max_results_per_route: int = 10,
    max_concurrent_pages: int = 3,
    headless: bool = True,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    bypass_cache: bool = False,
    cache_path: Optional[str] = None,
) -> List[FlightOption]:
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    return asyncio.run(
        scrape_one_way_flights_async(
            origins=origins,
            destinations=destinations,
            start_date=start_date,
            end_date=end_date,
            currency=currency,
            max_results_per_route=max_results_per_route,
            max_concurrent_pages=max_concurrent_pages,
            headless=headless,
            progress_callback=progress_callback,
            bypass_cache=bypass_cache,
            cache_path=cache_path,
        )
    )


async def scrape_decoupled_roundtrip_async(
    home_airport: str,
    target_airports: List[str],
    outbound_start_date: str,
    outbound_end_date: str,
    inbound_start_date: str,
    inbound_end_date: str,
    currency: str = "TRY",
    max_results_per_route: int = 10,
    max_concurrent_pages: int = 4,
    headless: bool = True,
    progress_callback: Optional[Callable[[int, int, str, Dict[str, int]], None]] = None,
    stop_event: Optional[threading.Event] = None,
    cache_path: Optional[str] = None,
) -> Tuple[List[FlightOption], List[FlightOption], Dict[str, Any]]:
    """
    Ayrık Tek Yön (Decoupled One-Way) mantığıyla gidiş ve dönüş rotalarını dönüşümlü tarar.
    Yalnızca kullanılabilir önbellek kayıtlarını (success / no_results ve yeterli requested_limit)
    önbellekten alır; hatalı veya eksik kapsamlı sorguları yeniden tarar.
    Toplanan ham uçuşları filtreleme öncesi kesmez; tamamını döndürür.
    """
    home = home_airport.upper().strip()
    targets = []
    for t in target_airports:
        code = t.upper().strip()
        if code and code != home and code not in targets:
            targets.append(code)

    out_dates = generate_date_range(outbound_start_date, outbound_end_date)
    in_dates = generate_date_range(inbound_start_date, inbound_end_date)

    out_tasks = [("OUT", home, target, d) for d in out_dates for target in targets]
    in_tasks = [("IN", target, home, d) for d in in_dates for target in targets]

    tasks_meta = []
    max_len = max(len(out_tasks), len(in_tasks))
    for i in range(max_len):
        if i < len(out_tasks):
            tasks_meta.append(out_tasks[i])
        if i < len(in_tasks):
            tasks_meta.append(in_tasks[i])

    total_tasks = len(tasks_meta)
    stats: Dict[str, int] = {
        "total": total_tasks,
        "done": 0,
        "success": 0,
        "no_results": 0,
        "failed": 0,
        "blocked": 0,
        "timeout": 0,
        "parse_error": 0,
        "from_cache": 0,
    }
    if total_tasks == 0:
        return [], [], stats

    start_epoch = get_cache_epoch()
    cache_data = load_daily_cache(cache_path=cache_path)
    entries = cache_data["entries"]

    outbound_flights: List[FlightOption] = []
    inbound_flights: List[FlightOption] = []
    uncached_tasks = []

    def _notify(label: str):
        if progress_callback:
            try:
                progress_callback(stats["done"], total_tasks, label, dict(stats))
            except TypeError:
                progress_callback(stats["done"], total_tasks, label)  # Geriye dönük imza desteği

    for direction, orig, dest, d in tasks_meta:
        ckey = _cache_key(orig, dest, d, currency)
        entry = entries.get(ckey)
        if is_cache_entry_usable(entry, max_results_per_route):
            cached_list = [FlightOption.from_dict(item) for item in entry.get("flights", [])]
            if direction == "OUT":
                outbound_flights.extend(cached_list)
            else:
                inbound_flights.extend(cached_list)
            stats["done"] += 1
            stats["from_cache"] += 1
            if entry.get("status") == QUERY_STATUS_NO_RESULTS:
                stats["no_results"] += 1
            else:
                stats["success"] += 1
            dir_label = "Gidiş" if direction == "OUT" else "Dönüş"
            _notify(f"[Önbellekten ⚡] [{dir_label}] {orig} -> {dest} ({d})")
        else:
            uncached_tasks.append((direction, orig, dest, d))

    if not uncached_tasks:
        return outbound_flights, inbound_flights, stats

    semaphore = asyncio.Semaphore(max_concurrent_pages)

    async with async_playwright() as p:
        browser = await _launch_browser(p, headless=headless)
        context = await browser.new_context(
            locale="tr-TR",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )

        stop_listener_task = None
        if stop_event:
            async def _watch_stop():
                while not stop_event.is_set():
                    await asyncio.sleep(0.12)
                # Durdurma sinyali alındığında context'i kapatarak açık tüm sekmeleri anında sonlandır
                try:
                    await context.close()
                except Exception:
                    pass

            stop_listener_task = asyncio.create_task(_watch_stop())

        async def _wrapped_scrape(direction: str, orig: str, dest: str, d: str):
            if stop_event and stop_event.is_set():
                return direction, None
            try:
                async with semaphore:
                    if stop_event and stop_event.is_set():
                        return direction, None
                    qres = await scrape_route_on_date_result(
                        context=context,
                        origin=orig,
                        destination=dest,
                        date_str=d,
                        currency=currency,
                        max_results_per_route=max_results_per_route,
                        semaphore=None,
                    )
            except Exception:
                if stop_event and stop_event.is_set():
                    return direction, None
                raise

            ckey = _cache_key(orig, dest, d, currency)
            update_single_cache_entry(
                ckey,
                qres.to_cache_entry(),
                expected_epoch=start_epoch,
                cache_path=cache_path,
            )

            stats["done"] += 1
            if qres.status == QUERY_STATUS_SUCCESS:
                stats["success"] += 1
            elif qres.status == QUERY_STATUS_NO_RESULTS:
                stats["no_results"] += 1
            else:
                stats["failed"] += 1
                if qres.status in stats:
                    stats[qres.status] += 1

            dir_label = "Gidiş" if direction == "OUT" else "Dönüş"
            _notify(f"[{dir_label}] {orig} -> {dest} ({d}) [{qres.status}]")
            return direction, qres

        results = await asyncio.gather(
            *[_wrapped_scrape(direction, orig, dest, d) for direction, orig, dest, d in uncached_tasks],
            return_exceptions=True,
        )

        if stop_listener_task and not stop_listener_task.done():
            stop_listener_task.cancel()

        for res in results:
            if isinstance(res, Exception) or res is None:
                continue
            direction, qres = res
            if qres is None or qres.status != QUERY_STATUS_SUCCESS:
                continue
            if direction == "OUT":
                outbound_flights.extend(qres.flights)
            else:
                inbound_flights.extend(qres.flights)

        try:
            await context.close()
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass

    return outbound_flights, inbound_flights, stats


def scrape_decoupled_roundtrip(
    home_airport: str,
    target_airports: List[str],
    outbound_start_date: str,
    outbound_end_date: str,
    inbound_start_date: str,
    inbound_end_date: str,
    currency: str = "TRY",
    max_results_per_route: int = 10,
    max_concurrent_pages: int = 4,
    headless: bool = True,
    progress_callback: Optional[Callable[..., None]] = None,
    stop_event: Optional[threading.Event] = None,
    cache_path: Optional[str] = None,
) -> Tuple[List[FlightOption], List[FlightOption], Dict[str, Any]]:
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    return asyncio.run(
        scrape_decoupled_roundtrip_async(
            home_airport=home_airport,
            target_airports=target_airports,
            outbound_start_date=outbound_start_date,
            outbound_end_date=outbound_end_date,
            inbound_start_date=inbound_start_date,
            inbound_end_date=inbound_end_date,
            currency=currency,
            max_results_per_route=max_results_per_route,
            max_concurrent_pages=max_concurrent_pages,
            headless=headless,
            progress_callback=progress_callback,
            stop_event=stop_event,
            cache_path=cache_path,
        )
    )


def _normalize_str_list(val: Any) -> List[str]:
    if not val or not isinstance(val, (list, tuple)):
        return []
    return [str(x).strip().upper() for x in val if str(x).strip()]


def evaluate_selected_flight_match(
    target_spec: Dict[str, Any],
    candidates: List[FlightOption],
) -> Tuple[str, Optional[FlightOption], Optional[str]]:
    """
    Seçili uçuşun güncel adaylar arasındaki eşleşmesini sıkı kimlik doğrulamasıyla değerlendirir.
    Dönüş: (match_status, matched_flight, message)
      - "matched": Tek ve kimliği doğrulanmış aday eşleşti.
      - "not_found": Hiçbir aday eşleşmedi.
      - "identity_unverified": Aday(lar) saat/rota bazında benziyor fakat uçuş numarası, aktarma
        segmenti veya varış günü/kimlik bilgisi yetersiz ya da birden fazla aday belirsiz eşleşiyor.
    """
    orig = str(target_spec.get("origin", "")).upper().strip()
    dest = str(target_spec.get("destination", "")).upper().strip()
    date_str = str(target_spec.get("date", "")).strip()
    airline = str(target_spec.get("airline", "")).strip().lower()
    dep_time = (str(target_spec.get("departure_time") or "").strip()) or None
    arr_time = (str(target_spec.get("arrival_time") or "").strip()) or None
    arr_date = (str(target_spec.get("arrival_date") or "").strip()) or None

    raw_offset = target_spec.get("arrival_day_offset", None)
    if raw_offset is not None and raw_offset != "":
        try:
            arr_offset: Optional[int] = int(raw_offset)
        except (TypeError, ValueError):
            arr_offset = None
    else:
        arr_offset = None

    raw_stops = target_spec.get("stops", None)
    if raw_stops is not None and raw_stops != "":
        try:
            stops: Optional[int] = int(raw_stops)
        except (TypeError, ValueError):
            stops = None
    else:
        stops = None

    # Eğer varış tarihi açıkça verilmemiş ama offset + kalkış tarihi varsa hesapla
    if not arr_date and date_str and arr_offset is not None:
        arr_date = compute_arrival_date(date_str, arr_offset)

    flight_num = (str(target_spec.get("flight_number") or "").strip().upper()) or None
    target_layovers = _normalize_str_list(target_spec.get("layover_airports"))
    target_segments = _normalize_str_list(target_spec.get("segments"))
    target_self_transfer = target_spec.get("self_transfer", None)

    # Temel kimlik alanları (kalkış/varış saati, aktarma sayısı, havayolu) eksikse kesin doğrulama yapılamaz
    if not orig or not dest or not date_str or not airline or not dep_time or not arr_time or stops is None:
        return (
            "identity_unverified",
            None,
            "Seçili uçuşun temel kimlik bilgileri (saat, aktarma veya havayolu) eksik olduğundan doğrulanamadı.",
        )

    strict_matches: List[FlightOption] = []
    partial_unverified_matches: List[FlightOption] = []

    for cand in candidates:
        if cand.origin != orig or cand.destination != dest or cand.date != date_str:
            continue
        if cand.airline.strip().lower() != airline:
            continue
        if (cand.departure_time or None) != dep_time:
            continue
        if (cand.arrival_time or None) != arr_time:
            continue
        if cand.stops != stops:
            continue

        cand_flight_num = (cand.flight_number or "").strip().upper() or None
        # 1) Her iki tarafta uçuş numarası varsa ve farklıysa kesinlikle ele!
        if flight_num and cand_flight_num and flight_num != cand_flight_num:
            continue

        # 2) Varış tarihi ve gün farkı kontrolü (farklı varış günleri asla aynı uçuş kabul edilmez)
        cand_arr_date = (cand.arrival_date or "").strip() or None
        cand_arr_offset = cand.arrival_day_offset
        if arr_date and cand_arr_date and arr_date != cand_arr_date:
            continue
        if arr_offset is not None and cand_arr_offset is not None and arr_offset != cand_arr_offset:
            continue

        # 3) Aktarma segmentleri ve aktarma havalimanları kontrolü
        cand_layovers = _normalize_str_list(cand.layover_airports)
        cand_segments = _normalize_str_list(cand.segments)
        if target_layovers and cand_layovers and target_layovers != cand_layovers:
            continue
        if target_segments and cand_segments and target_segments != cand_segments:
            continue
        if target_self_transfer is not None and cand.self_transfer is not None:
            if bool(target_self_transfer) != bool(cand.self_transfer):
                continue

        # Şimdi kimlik kanıtının yeterliliğini değerlendir:
        # Varış günü en az bir yolla (arrival_date veya arrival_day_offset) her iki tarafta da doğrulanmış olmalı
        has_verified_arrival_day = (
            (arr_date is not None and cand_arr_date is not None and arr_date == cand_arr_date)
            or (arr_offset is not None and cand_arr_offset is not None and arr_offset == cand_arr_offset)
        )
        if not has_verified_arrival_day:
            partial_unverified_matches.append(cand)
            continue

        # Eğer hedefte uçuş numarası, aktarma havalimanı veya segment listesi varsa ve adayda yoksa -> doğrulanamadı
        if flight_num and not cand_flight_num:
            partial_unverified_matches.append(cand)
            continue
        if target_layovers and not cand_layovers:
            partial_unverified_matches.append(cand)
            continue
        if target_segments and not cand_segments:
            partial_unverified_matches.append(cand)
            continue

        # Aktarmalı uçuşlarda (stops > 0), uçuş numarası, sıralı segmentler veya aktarma havalimanlarından
        # en az biri her iki tarafta da mevcut olup eşleşmeli; aksi halde farklı aktarma kombinasyonları ayırt edilemez.
        if stops > 0:
            has_connecting_identity = (
                (flight_num is not None and cand_flight_num is not None and flight_num == cand_flight_num)
                or (bool(target_segments) and bool(cand_segments) and target_segments == cand_segments)
                or (bool(target_layovers) and bool(cand_layovers) and target_layovers == cand_layovers)
            )
            if not has_connecting_identity:
                partial_unverified_matches.append(cand)
                continue

        strict_matches.append(cand)

    if len(strict_matches) == 1:
        return "matched", strict_matches[0], None

    if len(strict_matches) > 1:
        # Birden fazla aday tüm mevcut alanlarla eşleşiyorsa ve ayırt edici uçuş numarası/segment yoksa belirsizdir
        return (
            "identity_unverified",
            None,
            f"Aynı saat ve kriterlere sahip {len(strict_matches)} farklı aday bulundu; uçuş kimliği tekil olarak doğrulanamadı.",
        )

    if partial_unverified_matches:
        return (
            "identity_unverified",
            None,
            "Benzer saatli uçuş bulundu ancak uçuş numarası, varış günü veya aktarma detayları doğrulanamadığı için fiyat kesinleştirilemedi.",
        )

    return "not_found", None, "Seçili uçuş güncel arama sonuçlarında bulunamadı."


def match_selected_flight(
    target_spec: Dict[str, Any],
    candidates: List[FlightOption],
) -> Optional[FlightOption]:
    """
    Yeniden kontrol (recheck) sırasında yalnızca kimliği tam doğrulanmış tekil uçuşu eşleştirir;
    farklı uçuş numarasını, farklı varış gününü, farklı aktarma segmentini veya kimliği belirsiz
    adayları asla eşleştirmez.
    """
    status, matched, _ = evaluate_selected_flight_match(target_spec, candidates)
    if status == "matched":
        return matched
    return None


async def recheck_combination_async(
    outbound_spec: Dict[str, Any],
    inbound_spec: Dict[str, Any],
    currency: str = "TRY",
    max_results_per_route: int = 20,
    headless: bool = True,
    cache_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Seçili kombinasyonun yalnızca gidiş ve dönüş rota/günlerini önbelleği atlayarak yeniden tarar.
    Aynı uçuşun bulunup bulunmadığını, kimlik doğrulama durumunu ve fiyat farkını doğrular.
    """
    curr_clean = currency.upper().strip()
    start_epoch = get_cache_epoch()

    async with async_playwright() as p:
        browser = await _launch_browser(p, headless=headless)
        context = await browser.new_context(
            locale="tr-TR",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        try:
            out_qres, in_qres = await asyncio.gather(
                scrape_route_on_date_result(
                    context=context,
                    origin=str(outbound_spec.get("origin", "")),
                    destination=str(outbound_spec.get("destination", "")),
                    date_str=str(outbound_spec.get("date", "")),
                    currency=curr_clean,
                    max_results_per_route=max_results_per_route,
                ),
                scrape_route_on_date_result(
                    context=context,
                    origin=str(inbound_spec.get("origin", "")),
                    destination=str(inbound_spec.get("destination", "")),
                    date_str=str(inbound_spec.get("date", "")),
                    currency=curr_clean,
                    max_results_per_route=max_results_per_route,
                ),
            )
        finally:
            await context.close()
            await browser.close()

    # Başarılı veya no_results ise önbelleği taze veriyle güncelle
    for qres in (out_qres, in_qres):
        if qres.status in (QUERY_STATUS_SUCCESS, QUERY_STATUS_NO_RESULTS):
            ckey = _cache_key(qres.origin, qres.destination, qres.date, qres.currency)
            update_single_cache_entry(
                ckey, qres.to_cache_entry(), expected_epoch=start_epoch, cache_path=cache_path
            )

    def _eval_leg(spec: Dict[str, Any], qres: RouteQueryResult) -> Dict[str, Any]:
        old_price = float(spec.get("price", 0.0))
        if qres.status not in (QUERY_STATUS_SUCCESS, QUERY_STATUS_NO_RESULTS):
            return {
                "status": "scrape_error",
                "query_status": qres.status,
                "error_message": qres.error_message,
                "old_price": old_price,
                "new_price": None,
                "price_diff": None,
                "matched_flight": None,
            }
        match_status, matched, match_msg = evaluate_selected_flight_match(spec, qres.flights)
        if match_status == "identity_unverified":
            return {
                "status": "identity_unverified",
                "query_status": qres.status,
                "error_message": match_msg or "Uçuş kimliği doğrulanamadı.",
                "old_price": old_price,
                "new_price": None,
                "price_diff": None,
                "matched_flight": None,
            }
        if match_status != "matched" or matched is None:
            return {
                "status": "flight_not_found",
                "query_status": qres.status,
                "error_message": match_msg or "Seçili uçuş güncel arama sonuçlarında bulunamadı.",
                "old_price": old_price,
                "new_price": None,
                "price_diff": None,
                "matched_flight": None,
            }
        diff = round(matched.price - old_price, 2)
        status = "matched_unchanged" if abs(diff) < 0.01 else "matched_price_changed"
        return {
            "status": status,
            "query_status": qres.status,
            "error_message": None,
            "old_price": old_price,
            "new_price": matched.price,
            "price_diff": diff,
            "matched_flight": matched.to_dict(),
        }

    out_eval = _eval_leg(outbound_spec, out_qres)
    in_eval = _eval_leg(inbound_spec, in_qres)

    if out_eval["status"] == "scrape_error" or in_eval["status"] == "scrape_error":
        overall = "scrape_error"
    elif out_eval["status"] == "flight_not_found" or in_eval["status"] == "flight_not_found":
        overall = "flight_not_found"
    elif out_eval["status"] == "identity_unverified" or in_eval["status"] == "identity_unverified":
        overall = "identity_unverified"
    elif out_eval["status"] == "matched_price_changed" or in_eval["status"] == "matched_price_changed":
        overall = "matched_price_changed"
    else:
        overall = "matched_unchanged"

    old_total = round(out_eval["old_price"] + in_eval["old_price"], 2)
    new_total = (
        round(out_eval["new_price"] + in_eval["new_price"], 2)
        if out_eval["new_price"] is not None and in_eval["new_price"] is not None
        else None
    )
    total_diff = round(new_total - old_total, 2) if new_total is not None else None

    return {
        "overall_status": overall,
        "currency": curr_clean,
        "old_flight_total": old_total,
        "new_flight_total": new_total,
        "total_price_diff": total_diff,
        "outbound": out_eval,
        "inbound": in_eval,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }


def recheck_combination(
    outbound_spec: Dict[str, Any],
    inbound_spec: Dict[str, Any],
    currency: str = "TRY",
    max_results_per_route: int = 20,
    headless: bool = True,
    cache_path: Optional[str] = None,
) -> Dict[str, Any]:
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    return asyncio.run(
        recheck_combination_async(
            outbound_spec=outbound_spec,
            inbound_spec=inbound_spec,
            currency=currency,
            max_results_per_route=max_results_per_route,
            headless=headless,
            cache_path=cache_path,
        )
    )
