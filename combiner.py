import math
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd
from scraper import FlightOption


# Hedef şehirlere göre varsayılan TAHMİNİ tek yön kara ulaşımı (tren/otobüs) değerleri (EUR cinsinden).
# Kullanıcı arayüzde kendi kurunu girerek veya doğrudan tutarları değiştirerek düzenleyebilir.
DEFAULT_TRANSFER_PRESETS_EUR: Dict[str, Dict[str, float]] = {
    "EIN": {"cost": 5.0, "duration_min": 20},
    "AMS": {"cost": 23.0, "duration_min": 80},
    "DUS": {"cost": 19.0, "duration_min": 105},
    "CGN": {"cost": 25.0, "duration_min": 150},
    "BRU": {"cost": 22.0, "duration_min": 110},
    "CRL": {"cost": 26.0, "duration_min": 145},
    "NRN": {"cost": 18.0, "duration_min": 75},
    "RTM": {"cost": 21.0, "duration_min": 85},
}


def compute_effective_date_ranges(
    window_start: str,
    window_end: str,
    min_stay_days: int,
) -> Tuple[str, str, str, str]:
    """
    Seyahat penceresi [window_start, window_end] ve minimum konaklama gecesine (min_stay_days) göre:
      - Gidiş kalkış tarihi aralığı: window_start .. (window_end - min_stay_days)
      - Dönüş kalkış tarihi aralığı: (window_start + min_stay_days) .. window_end
    döner. Varış gün farkı (+1/+2) >= 0 olduğu için bu aralık hiçbir geçerli uçuş gününü erken elemez.
    """
    start_dt = datetime.strptime(window_start, "%Y-%m-%d")
    end_dt = datetime.strptime(window_end, "%Y-%m-%d")
    min_stay = max(1, int(min_stay_days))

    out_start_dt = start_dt
    out_end_dt = end_dt - timedelta(days=min_stay)
    in_start_dt = start_dt + timedelta(days=min_stay)
    in_end_dt = end_dt

    return (
        out_start_dt.strftime("%Y-%m-%d"),
        out_end_dt.strftime("%Y-%m-%d"),
        in_start_dt.strftime("%Y-%m-%d"),
        in_end_dt.strftime("%Y-%m-%d"),
    )


def parse_hhmm_to_minutes(hhmm: Optional[str]) -> Optional[int]:
    """
    '07:45' formatındaki saati gün içi dakikaya (0..1439) çevirir.
    Saat bilgisi yoksa veya geçersizse varsayılan bir saat (örn. 12:00) uydurmaz; None döner.
    """
    if not hhmm or not str(hhmm).strip():
        return None
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", str(hhmm))
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def format_baggage_status(cabin_flag: Optional[bool]) -> str:
    """
    Havayolu adına göre tahmin yürütmez; yalnızca gerçek tekliften doğrulanmışsa
    Dahil/Hariç yazar, aksi halde 'Bilinmiyor (Doğrulanmadı)' döner.
    """
    if cabin_flag is True:
        return "✅ Kabin Bagajı Dahil (Doğrulandı)"
    if cabin_flag is False:
        return "❌ Kabin Bagajı Hariç (Doğrulandı)"
    return "❓ Bagaj: Bilinmiyor (Doğrulanmadı)"


def _resolve_arrival_date(row: pd.Series) -> Optional[str]:
    """
    Uçuşun varış yerel tarihini (YYYY-MM-DD) döner.
    Eğer arrival_date veya arrival_day_offset doğrulanmamışsa sessizce aynı gün kabul etmez, None döner.
    """
    arr_date = row.get("arrival_date")
    if isinstance(arr_date, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", arr_date.strip()):
        return arr_date.strip()

    dep_date = row.get("date")
    offset = row.get("arrival_day_offset")
    if (
        isinstance(dep_date, str)
        and re.match(r"^\d{4}-\d{2}-\d{2}$", dep_date.strip())
        and offset is not None
        and not pd.isna(offset)
    ):
        try:
            dt = datetime.strptime(dep_date.strip(), "%Y-%m-%d") + timedelta(days=int(offset))
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return None
    return None


def sanitize_records_for_json(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Pandas to_dict('records') çıktısındaki NaN / inf değerlerini JSON uyumlu None (null) değerine dönüştürür.
    """
    cleaned: List[Dict[str, Any]] = []
    for rec in records:
        new_rec: Dict[str, Any] = {}
        for k, v in rec.items():
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                new_rec[k] = None
            elif pd.isna(v) if not isinstance(v, (list, dict, str, bytes)) else False:
                new_rec[k] = None
            else:
                new_rec[k] = v
        cleaned.append(new_rec)
    return cleaned


def build_combinations_dataframe(
    outbound_flights: List[FlightOption],
    inbound_flights: List[FlightOption],
    min_stay_days: int = 4,
    max_stay_days: int = 6,
    window_start: Optional[str] = None,
    window_end: Optional[str] = None,
    expected_currency: Optional[str] = None,
    max_stops: Optional[int] = None,
    max_flight_hours: Optional[float] = None,
    out_dep_min_time: str = "00:00",
    out_dep_max_time: str = "23:59",
    in_dep_min_time: str = "00:00",
    in_dep_max_time: str = "23:59",
    baggage_mode: str = "none",  # "none" | "free_cabin_only" | "add_cabin_bag" | "add_checked_bag"
    cabin_bag_fee: float = 0.0,
    checked_bag_fee: float = 0.0,
    excluded_airlines: Optional[List[str]] = None,
    include_transfer: bool = False,
    transfer_costs: Optional[Dict[str, float]] = None,
    transfer_durations: Optional[Dict[str, int]] = None,
) -> pd.DataFrame:
    """
    Ayrık tek yön gidiş (outbound) ve dönüş (inbound) uçuşlarını kartezyen çarpımla birleştirir.

    Kurallar:
    - Farklı para birimlerindeki uçuşlar asla toplanmaz.
    - Aktarma, süre veya kalkış saati doğrulanmamış (None) uçuşlar ilgili filtre aktifken elenir.
    - Hedefte kalış gecesi (stay_days), gidişin hedefe varış yerel tarihi (out_arrival_date) ile
      dönüşün kalkış yerel tarihi (in_date) arasındaki fark olarak hesaplanır.
    - Dönüşün eve varış yerel tarihi (in_arrival_date), window_end tarihini aşamaz.
    - Doğrulanamayan varış tarihleri sessizce aynı gün kabul edilmez; kombinasyondan elenir.
    """
    if not outbound_flights or not inbound_flights:
        return pd.DataFrame()

    df_out = pd.DataFrame([f.to_dict() for f in outbound_flights])
    df_in = pd.DataFrame([f.to_dict() for f in inbound_flights])

    # Para birimi doğrulaması (farklı para birimlerini asla karıştırma)
    if expected_currency:
        curr_upper = expected_currency.upper().strip()
        df_out = df_out[df_out["currency"].astype(str).str.upper() == curr_upper]
        df_in = df_in[df_in["currency"].astype(str).str.upper() == curr_upper]

    if df_out.empty or df_in.empty:
        return pd.DataFrame()

    # Varış yerel tarihlerini doğrula ve hesapla
    df_out["resolved_arrival_date"] = df_out.apply(_resolve_arrival_date, axis=1)
    df_in["resolved_arrival_date"] = df_in.apply(_resolve_arrival_date, axis=1)

    # Doğrulanamayan varış tarihlerini sessizce aynı gün kabul etme -> ele
    df_out = df_out[df_out["resolved_arrival_date"].notna()].copy()
    df_in = df_in[df_in["resolved_arrival_date"].notna()].copy()

    if df_out.empty or df_in.empty:
        return pd.DataFrame()

    # Bagaj durumu etiketleri (yalnızca doğrulanmış veriye göre)
    if "cabin_baggage_included" not in df_out.columns:
        df_out["cabin_baggage_included"] = None
    if "cabin_baggage_included" not in df_in.columns:
        df_in["cabin_baggage_included"] = None

    df_out["baggage_label"] = df_out["cabin_baggage_included"].apply(format_baggage_status)
    df_in["baggage_label"] = df_in["cabin_baggage_included"].apply(format_baggage_status)

    # 1) Aktarma filtresi (stops değeri None/NaN olanlar filtreyi geçemez!)
    if max_stops is not None:
        df_out = df_out[df_out["stops"].notna() & (df_out["stops"] <= int(max_stops))]
        df_in = df_in[df_in["stops"].notna() & (df_in["stops"] <= int(max_stops))]

    # 2) Maksimum uçuş süresi filtresi (duration_minutes değeri None/NaN veya <=0 olanlar geçemez!)
    if max_flight_hours is not None and float(max_flight_hours) > 0:
        max_mins = int(float(max_flight_hours) * 60)
        df_out = df_out[
            df_out["duration_minutes"].notna()
            & (df_out["duration_minutes"] > 0)
            & (df_out["duration_minutes"] <= max_mins)
        ]
        df_in = df_in[
            df_in["duration_minutes"].notna()
            & (df_in["duration_minutes"] > 0)
            & (df_in["duration_minutes"] <= max_mins)
        ]

    # 3) Kalkış saati aralığı filtreleri (saati bilinmeyen uçuşlar 12:00 varsayılmaz, elenir!)
    out_min_m = parse_hhmm_to_minutes(out_dep_min_time)
    out_max_m = parse_hhmm_to_minutes(out_dep_max_time)
    in_min_m = parse_hhmm_to_minutes(in_dep_min_time)
    in_max_m = parse_hhmm_to_minutes(in_dep_max_time)

    out_min_val = out_min_m if out_min_m is not None else 0
    out_max_val = out_max_m if out_max_m is not None else 1439
    in_min_val = in_min_m if in_min_m is not None else 0
    in_max_val = in_max_m if in_max_m is not None else 1439

    if out_min_val > 0 or out_max_val < 1439:
        out_dep_mins = df_out["departure_time"].apply(parse_hhmm_to_minutes)
        df_out = df_out[
            out_dep_mins.notna() & (out_dep_mins >= out_min_val) & (out_dep_mins <= out_max_val)
        ]

    if in_min_val > 0 or in_max_val < 1439:
        in_dep_mins = df_in["departure_time"].apply(parse_hhmm_to_minutes)
        df_in = df_in[
            in_dep_mins.notna() & (in_dep_mins >= in_min_val) & (in_dep_mins <= in_max_val)
        ]

    # 4) Hariç tutulan havayolları filtresi
    if excluded_airlines:
        excl_set = {a.strip().lower() for a in excluded_airlines if a and a.strip()}
        if excl_set:
            df_out = df_out[~df_out["airline"].astype(str).str.lower().isin(excl_set)]
            df_in = df_in[~df_in["airline"].astype(str).str.lower().isin(excl_set)]

    # 5) "Ücretsiz kabin bagajı" filtresinden YALNIZCA gerçek tekliften doğrulanmış (True) olanlar geçer
    if baggage_mode == "free_cabin_only":
        df_out = df_out[df_out["cabin_baggage_included"].apply(lambda x: x is True)]
        df_in = df_in[df_in["cabin_baggage_included"].apply(lambda x: x is True)]

    if df_out.empty or df_in.empty:
        return pd.DataFrame()

    # Sütunları gidiş (out_) ve dönüş (in_) olarak adlandır
    df_out = df_out.add_prefix("out_")
    df_in = df_in.add_prefix("in_")

    # Kartezyen çarpım (cross join) - yalnızca aynı para birimindeki bacakları eşleştir
    df_out["_key"] = df_out["out_currency"]
    df_in["_key"] = df_in["in_currency"]
    combos = pd.merge(df_out, df_in, on="_key").drop(columns=["_key"])

    if combos.empty:
        return pd.DataFrame()

    # Seyahat penceresi sınır kontrolü:
    # - Gidiş kalkış tarihi >= window_start
    # - Dönüşün eve varış yerel tarihi (in_resolved_arrival_date) <= window_end
    if window_start:
        combos = combos[combos["out_date"] >= window_start]
    if window_end:
        combos = combos[combos["in_resolved_arrival_date"] <= window_end]

    if combos.empty:
        return pd.DataFrame()

    # Hedefte kalış gecesi (stay_days):
    # Gidişin hedefe varış yerel tarihi (out_resolved_arrival_date) ile
    # dönüşün kalkış yerel tarihi (in_date) arasındaki gün farkı!
    out_arr_dt = pd.to_datetime(combos["out_resolved_arrival_date"])
    in_dep_dt = pd.to_datetime(combos["in_date"])
    combos["stay_days"] = (in_dep_dt - out_arr_dt).dt.days
    combos["out_arrival_date"] = combos["out_resolved_arrival_date"]
    combos["in_arrival_date"] = combos["in_resolved_arrival_date"]

    # Kalış süresi filtresi
    combos = combos[
        (combos["stay_days"] >= int(min_stay_days)) & (combos["stay_days"] <= int(max_stay_days))
    ].copy()

    if combos.empty:
        return pd.DataFrame()

    # Asimetrik rota kontrolü
    combos["is_asymmetric"] = combos["out_destination"] != combos["in_origin"]
    combos["route_summary"] = (
        combos["out_origin"]
        + " → "
        + combos["out_destination"]
        + "  |  "
        + combos["in_origin"]
        + " → "
        + combos["in_destination"]
    )

    # 6) Manuel tahmini bagaj ek ücreti hesabı
    if baggage_mode == "add_cabin_bag":
        combos["out_baggage_cost"] = combos["out_cabin_baggage_included"].apply(
            lambda x: 0.0 if x is True else float(cabin_bag_fee)
        )
        combos["in_baggage_cost"] = combos["in_cabin_baggage_included"].apply(
            lambda x: 0.0 if x is True else float(cabin_bag_fee)
        )
        combos["baggage_is_estimated"] = True
    elif baggage_mode == "add_checked_bag":
        combos["out_baggage_cost"] = float(checked_bag_fee)
        combos["in_baggage_cost"] = float(checked_bag_fee)
        combos["baggage_is_estimated"] = True
    else:
        combos["out_baggage_cost"] = 0.0
        combos["in_baggage_cost"] = 0.0
        combos["baggage_is_estimated"] = False

    combos["baggage_total_price"] = combos["out_baggage_cost"] + combos["in_baggage_cost"]

    # 7) Kara ulaşımı (transfer) maliyeti ve süresi
    # Eksik transfer maliyetini sessizce 0 kabul ederek daha ucuz seçenek üretme!
    if include_transfer:
        if not transfer_costs:
            return pd.DataFrame()
        clean_costs: Dict[str, float] = {}
        for k, v in transfer_costs.items():
            if v is not None and str(v).strip() != "":
                try:
                    val = float(v)
                    if not math.isnan(val) and val >= 0:
                        clean_costs[k.upper().strip()] = val
                except (ValueError, TypeError):
                    pass

        combos["out_transfer_cost"] = combos["out_destination"].map(clean_costs)
        combos["in_transfer_cost"] = combos["in_origin"].map(clean_costs)

        # Transfer maliyeti tanımlı olmayan havalimanlarını 0 kabul etme, kombinasyondan çıkar
        combos = combos[
            combos["out_transfer_cost"].notna() & combos["in_transfer_cost"].notna()
        ].copy()
        if combos.empty:
            return pd.DataFrame()

        clean_durs: Dict[str, Optional[int]] = {}
        if transfer_durations:
            for k, v in transfer_durations.items():
                if v is not None and str(v).strip() != "":
                    try:
                        clean_durs[k.upper().strip()] = max(0, int(float(v)))
                    except (ValueError, TypeError):
                        pass

        combos["out_transfer_min"] = combos["out_destination"].map(clean_durs)
        combos["in_transfer_min"] = combos["in_origin"].map(clean_durs)
        combos["transfer_is_estimated"] = True
    else:
        combos["out_transfer_cost"] = 0.0
        combos["in_transfer_cost"] = 0.0
        combos["out_transfer_min"] = 0
        combos["in_transfer_min"] = 0
        combos["transfer_is_estimated"] = False

    # Fiyat toplamları
    combos["flight_total_price"] = combos["out_price"] + combos["in_price"]
    combos["transfer_total_price"] = (
        combos["out_transfer_cost"] + combos["in_transfer_cost"]
    )
    combos["grand_total_price"] = (
        combos["flight_total_price"]
        + combos["baggage_total_price"]
        + combos["transfer_total_price"]
    )

    # Süre toplamları: süresi bilinmeyen uçuşları 0 dakika gösterme veya en kısa uçuş olarak sıralama!
    out_dur_valid = combos["out_duration_minutes"].notna() & (combos["out_duration_minutes"] > 0)
    in_dur_valid = combos["in_duration_minutes"].notna() & (combos["in_duration_minutes"] > 0)
    both_flight_dur_known = out_dur_valid & in_dur_valid

    combos["total_flight_minutes"] = pd.Series(index=combos.index, dtype="float64")
    combos.loc[both_flight_dur_known, "total_flight_minutes"] = (
        combos.loc[both_flight_dur_known, "out_duration_minutes"].astype(float)
        + combos.loc[both_flight_dur_known, "in_duration_minutes"].astype(float)
    )

    if include_transfer:
        tr_dur_known = combos["out_transfer_min"].notna() & combos["in_transfer_min"].notna()
        combos["total_transfer_minutes"] = pd.Series(index=combos.index, dtype="float64")
        combos.loc[tr_dur_known, "total_transfer_minutes"] = (
            combos.loc[tr_dur_known, "out_transfer_min"].astype(float)
            + combos.loc[tr_dur_known, "in_transfer_min"].astype(float)
        )
        both_total_known = both_flight_dur_known & tr_dur_known
        combos["total_travel_minutes"] = pd.Series(index=combos.index, dtype="float64")
        combos.loc[both_total_known, "total_travel_minutes"] = (
            combos.loc[both_total_known, "total_flight_minutes"]
            + combos.loc[both_total_known, "total_transfer_minutes"]
        )
    else:
        combos["total_transfer_minutes"] = 0.0
        combos["total_travel_minutes"] = combos["total_flight_minutes"]

    # Toplam maliyete ve ardından toplam seyahat süresine göre sırala (süresi bilinmeyenler en sona!)
    combos = combos.sort_values(
        by=["grand_total_price", "total_travel_minutes"],
        ascending=[True, True],
        na_position="last",
    ).reset_index(drop=True)

    return combos
