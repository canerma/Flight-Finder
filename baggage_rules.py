"""
baggage_rules.py - Havayolu Bagaj Kuralları, Referans Ücretler ve Bağımsız Bacak Tahmin Motoru

Yetişkin ekonomi ve Türkiye–Avrupa hatları için referans veriler:
- SunExpress (SunLight / SunValue)
- THY (EcoFly / ExtraFly)
- AJet (Basic / EcoJet)
- Pegasus (Light / Süper Eko)

Doğrulanmış paket ve teklif bilgisi her zaman tahminden önce gelir.
Kişisel koltuk altı çantası üst dolap kabin valizi ihtiyacını karşılamaz.
"""

import math
import re
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple, Set


TURKEY_AIRPORTS: Set[str] = {
    "ESB", "IST", "SAW", "AYT", "ADB", "BJV", "DLM", "ADA", "GZT", "TZX",
    "ASR", "KYA", "EZS", "ERZ", "VAN", "DIY", "SZF", "MLX", "GNY", "EDO",
    "DNZ", "KZR", "USQ", "ONQ", "AOE", "TEQ", "MQM", "BAL", "CKZ", "NOP",
    "NAV", "KCM", "IGD", "BGG", "AJI", "VAS", "YEI", "ISE", "OGU", "KFS"
}

UK_AIRPORTS: Set[str] = {
    "STN", "LTN", "LGW", "LHR", "SEN", "LCY", "BHX", "MAN", "EDI", "GLA",
    "BRS", "NCL", "BFS", "LPL", "EMA", "LBA", "ABZ", "BHD", "SOU", "CWL"
}

GERMANY_AIRPORTS: Set[str] = {
    "DUS", "CGN", "MUC", "BER", "FRA", "STR", "NUE", "FMO", "DTM", "NRN",
    "FKB", "LEJ", "DRS", "FMM", "HAM", "HAJ", "BRE", "PAD", "ERF", "SCN"
}

NETHERLANDS_AIRPORTS: Set[str] = {
    "AMS", "EIN", "RTM", "MST", "GRQ"
}

STANDARD_AJET_EUROPE_AIRPORTS: Set[str] = GERMANY_AIRPORTS | NETHERLANDS_AIRPORTS


@dataclass
class BaggageLegResult:
    cabin_required: bool
    cabin_cost: Optional[float]           # None if unknown, 0.0 if included/not needed, or fee
    cabin_status: str                     # included | verified_price | reference_estimate | manual_estimate | unknown | not_needed
    cabin_source: str                     # verified_offer | package_rule | sunvalue_assumption | basic_entry | sunlight_starting | manual | unknown | not_needed
    cabin_desc: str
    cabin_ref_eur: Optional[float]
    cabin_passes_filter: bool

    checked_required_kg: int
    checked_cost: Optional[float]         # None if unknown, 0.0 if included/not needed, or fee
    checked_status: str                   # included | verified_price | reference_estimate | manual_estimate | unknown | not_needed
    checked_source: str                   # verified_offer | package_rule | sunvalue_assumption | basic_entry | sunlight_starting | manual | unknown | not_needed
    checked_desc: str
    checked_ref_eur: Optional[float]
    checked_passes_filter: bool

    leg_baggage_cost: Optional[float]     # None if any required item is unknown, else cabin_cost + checked_cost
    leg_known_cost: float                 # Sum of all known required bag items on this leg (>= 0.0)
    leg_cost_known: bool                  # True only if all required bag items have known cost
    leg_summary_text: str
    personal_bag_info: str
    passes_filter: bool
    is_estimated: bool                    # True if based on assumption or reference estimate

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cabin_required": self.cabin_required,
            "cabin_cost": self.cabin_cost,
            "cabin_status": self.cabin_status,
            "cabin_source": self.cabin_source,
            "cabin_desc": self.cabin_desc,
            "cabin_ref_eur": self.cabin_ref_eur,
            "checked_required_kg": self.checked_required_kg,
            "checked_cost": self.checked_cost,
            "checked_status": self.checked_status,
            "checked_source": self.checked_source,
            "checked_desc": self.checked_desc,
            "checked_ref_eur": self.checked_ref_eur,
            "leg_baggage_cost": self.leg_baggage_cost,
            "leg_known_cost": self.leg_known_cost,
            "leg_cost_known": self.leg_cost_known,
            "leg_summary_text": self.leg_summary_text,
            "personal_bag_info": self.personal_bag_info,
            "passes_filter": self.passes_filter,
            "is_estimated": self.is_estimated,
        }


def normalize_airline_name(name: Optional[str]) -> str:
    """Havayolu adını standart koda eşler."""
    if not name:
        return "other"
    n = str(name).lower().strip()
    if "sunexpress" in n or "sun express" in n or n == "xq":
        return "sunexpress"
    if "turkish" in n or "türk hava" in n or "thy" in n or n == "tk":
        return "thy"
    if "ajet" in n or "anadolujet" in n or "anadolu jet" in n or n == "vf":
        return "ajet"
    if "pegasus" in n or "flypgs" in n or n == "pc":
        return "pegasus"
    return n


def get_personal_bag_info(airline_normalized: str) -> str:
    """Kişisel çanta (koltuk altı) hakkı bilgilendirme metni."""
    if airline_normalized == "sunexpress":
        return "1 adet 4 kg koltuk altı çanta (40×30×20 cm) ücretsiz · SunValue paketinde 8 kg kabin ve 20 kg kayıtlı bagaj dahildir"
    if airline_normalized == "thy":
        return "1 adet 4 kg kişisel çanta (40×30×15 cm) ücretsiz"
    if airline_normalized == "ajet":
        return "1 adet 4 kg koltuk altı çanta (40×30×15 cm) ücretsiz"
    if airline_normalized == "pegasus":
        return "1 adet 3 kg koltuk altı çanta (40×30×15 cm) ücretsiz"
    return "Koltuk altı çanta kuralları havayolu ve bilet tipine bağlıdır"


def convert_ref_eur_to_target(
    ref_eur: Optional[float],
    target_currency: str,
    manual_eur_rate: Optional[float],
) -> Tuple[Optional[float], Optional[str]]:
    """
    EUR referans ücretini hedef para birimine dönüştürür.
    Sabit kur varsayımı yapılmaz.
    Hedef para birimi EUR ise doğrudan kullanılır.
    Hedef para birimi TRY/USD ise ve geçerli bir manual_eur_rate varsa dönüştürülür.
    Kur eksik/geçersiz ise None döner ve toplama eklenmez.
    """
    if ref_eur is None:
        return None, None
    curr_upper = str(target_currency).upper().strip()
    if curr_upper == "EUR":
        return round(float(ref_eur), 2), None
    if manual_eur_rate is not None and float(manual_eur_rate) > 0:
        return round(float(ref_eur) * float(manual_eur_rate), 2), None
    return None, f"EUR referansı (+€{ref_eur:g}) kur çarpanı girilmediği için {curr_upper} toplamına eklenmedi"


def evaluate_leg_baggage(
    flight_data: Dict[str, Any],
    cabin_req: str = "none",                      # "none" | "1x8kg"
    checked_req: int = 0,                         # 0, 5, 10, 15, 20, 25, 30, 35
    baggage_included_only: bool = False,          # Yalnızca seçili ihtiyaçların tamamı teklife dahil olanları geçir
    manual_cabin_fee: Optional[float] = None,     # Kullanıcı manuel kabin ücreti (None = boş/belirsiz, 0.0 = ücretsiz)
    manual_checked_fee: Optional[float] = None,   # Kullanıcı manuel uçak altı ücreti
    currency: str = "TRY",
    manual_eur_rate: Optional[float] = None,
    sunexpress_cabin_default_eur: float = 19.99,  # Resmi sayfalar €14.99 - €19.99; varsayılan €19.99
) -> BaggageLegResult:
    """
    Tek bir bacak (gidiş veya dönüş) için bağımsız bagaj değerlendirmesi yapar.
    Öncelik sıraları:
    1. Teklifte açıkça okunan hak veya hak yokluğu (verified_offer).
    2. Teklifin paket adı biliniyorsa rotaya uygun paket kuralı (package_rule).
    3. Paket bilinmiyorsa açıkça etiketlenen varsayım (assumption).
    4. Belirsiz (unknown / None).
    """
    airline_raw = str(flight_data.get("airline") or "")
    airline_norm = normalize_airline_name(airline_raw)
    origin = str(flight_data.get("origin") or "").upper().strip()
    destination = str(flight_data.get("destination") or "").upper().strip()

    def _clean(val: Any) -> Any:
        if val is None:
            return None
        if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
            return None
        if str(val).strip().lower() in ("nan", "none", "<na>"):
            return None
        return val

    stops = _clean(flight_data.get("stops"))
    is_direct = False
    if stops is not None:
        try:
            is_direct = (int(stops) == 0)
        except (ValueError, TypeError):
            is_direct = False

    # Doğrulanmış teklif alanları (asla tahminle ezilmez)
    verified_cabin_raw = _clean(flight_data.get("cabin_baggage_included"))
    verified_cabin = verified_cabin_raw if isinstance(verified_cabin_raw, bool) else None

    verified_checked_raw = _clean(flight_data.get("checked_baggage_included"))
    verified_checked = verified_checked_raw if isinstance(verified_checked_raw, bool) else None

    verified_checked_kg_raw = _clean(flight_data.get("checked_baggage_kg"))
    verified_checked_kg: Optional[int] = None
    if verified_checked_kg_raw is not None:
        try:
            verified_checked_kg = int(verified_checked_kg_raw)
        except (ValueError, TypeError):
            verified_checked_kg = None

    pkg_raw = str(flight_data.get("package_name") or "").lower().strip()
    is_sunvalue = "sunvalue" in pkg_raw
    is_sunlight = "sunlight" in pkg_raw
    is_ecofly = "ecofly" in pkg_raw

    personal_info = get_personal_bag_info(airline_norm)
    is_tr_origin = origin in TURKEY_AIRPORTS
    is_tr_dest = destination in TURKEY_AIRPORTS
    is_turkey_route = (is_tr_origin != is_tr_dest)
    is_uk_route = (origin in UK_AIRPORTS) or (destination in UK_AIRPORTS)
    is_de_nl_route = (origin in STANDARD_AJET_EUROPE_AIRPORTS) or (destination in STANDARD_AJET_EUROPE_AIRPORTS)

    # Rota ve aktarma kapsam kontrolleri (aktarma sayısı bilinmiyorsa direkt uçuş varsayılmaz!)
    is_ajet_de_nl_scope = is_turkey_route and is_de_nl_route and is_direct
    is_sunlight_scope = is_turkey_route and not is_uk_route and is_direct

    # -------------------------------------------------------------
    # 1. ÜST DOLAP KABİN VALİZİ DEĞERLENDİRMESİ
    # -------------------------------------------------------------
    cabin_required = (cabin_req == "1x8kg")
    cabin_cost: Optional[float] = 0.0
    cabin_status = "not_needed"
    cabin_source = "not_needed"
    cabin_desc = "Kabin valizi gerekmiyor"
    cabin_ref_eur: Optional[float] = None
    cabin_passes_filter = True
    cabin_is_estimated = False

    if cabin_required:
        # 1. Teklifte açıkça okunan hak veya hak yokluğu
        if verified_cabin is True:
            cabin_cost = 0.0
            cabin_status = "included"
            cabin_source = "verified_offer"
            cabin_desc = "8 kg kabin: dahil · teklif üzerinden doğrulandı"
            cabin_passes_filter = True
            cabin_is_estimated = False
        elif verified_cabin is False:
            # Teklifte kabin dahil olmadığı açıkça belirtilmiş -> paket varsayımı bunu EZEMEZ!
            cabin_passes_filter = False
            if manual_cabin_fee is not None:
                cabin_cost = round(float(manual_cabin_fee), 2)
                cabin_status = "manual_estimate"
                cabin_source = "manual"
                cabin_desc = f"8 kg kabin: manuel tahmin (+{cabin_cost:g} {currency}) · teklifte kabin dahil değil"
                cabin_is_estimated = True
            elif airline_norm == "sunexpress" and (is_sunlight or is_sunlight_scope):
                if is_sunlight_scope:
                    cabin_ref_eur = sunexpress_cabin_default_eur
                    converted, note = convert_ref_eur_to_target(cabin_ref_eur, currency, manual_eur_rate)
                    cabin_cost = converted
                    cabin_status = "reference_estimate" if converted is not None else "unknown"
                    cabin_source = "sunlight_starting"
                    if converted is not None:
                        cabin_desc = f"8 kg kabin: tahmini +€19,99 ({converted:g} {currency}) · SunLight başlangıç fiyatı"
                    else:
                        cabin_desc = f"8 kg kabin: tahmini +€19,99 · SunLight başlangıç fiyatı ({note})"
                    cabin_is_estimated = True
                else:
                    cabin_cost = None
                    cabin_status = "unknown"
                    cabin_source = "out_of_scope"
                    cabin_desc = "8 kg kabin: dahil değil · kapsamı doğrulanmamış rota"
            elif airline_norm == "ajet" and is_ajet_de_nl_scope:
                cabin_ref_eur = 15.0
                converted, note = convert_ref_eur_to_target(cabin_ref_eur, currency, manual_eur_rate)
                cabin_cost = converted
                cabin_status = "reference_estimate" if converted is not None else "unknown"
                cabin_source = "basic_entry"
                if converted is not None:
                    cabin_desc = f"8 kg kabin: tahmini +€15 ({converted:g} {currency}) · AJet Basic: kabin ücretli, bilette kontrol edin"
                else:
                    cabin_desc = f"8 kg kabin: tahmini +€15 · AJet Basic: kabin ücretli ({note})"
                cabin_is_estimated = True
            else:
                cabin_cost = None
                cabin_status = "unknown"
                cabin_source = "unverified_fee"
                cabin_desc = "8 kg kabin: dahil değil (ek ücret teklifte doğrulanmadı)"
        else:
            # Teklifte kabin bilgisi doğrudan doğrulanmamış (verified_cabin is None)
            # Tahmini haklar "Yalnızca doğrulanmış bagaj dahil" filtresini GEÇEMEZ!
            cabin_passes_filter = False

            if manual_cabin_fee is not None:
                cabin_cost = round(float(manual_cabin_fee), 2)
                cabin_status = "manual_estimate"
                cabin_source = "manual"
                cabin_desc = f"8 kg kabin: manuel tahmin (+{cabin_cost:g} {currency})"
                cabin_is_estimated = True
            elif airline_norm == "sunexpress":
                if is_sunvalue:
                    # Doğrulanmış SunValue paketi kuralı
                    cabin_cost = 0.0
                    cabin_status = "included"
                    cabin_source = "package_rule"
                    cabin_desc = "8 kg kabin: dahil · SunValue paket kuralı"
                    cabin_passes_filter = True
                    cabin_is_estimated = False
                elif is_sunlight:
                    if is_sunlight_scope:
                        cabin_ref_eur = sunexpress_cabin_default_eur
                        converted, note = convert_ref_eur_to_target(cabin_ref_eur, currency, manual_eur_rate)
                        cabin_cost = converted
                        cabin_status = "reference_estimate" if converted is not None else "unknown"
                        cabin_source = "sunlight_starting"
                        if converted is not None:
                            cabin_desc = f"8 kg kabin: tahmini +€19,99 ({converted:g} {currency}) · SunLight başlangıç fiyatı"
                        else:
                            cabin_desc = f"8 kg kabin: tahmini +€19,99 · SunLight başlangıç fiyatı ({note})"
                        cabin_is_estimated = True
                    else:
                        cabin_cost = None
                        cabin_status = "unknown"
                        cabin_source = "out_of_scope"
                        cabin_desc = "8 kg kabin: ücret belirsiz"
                else:
                    # Paket bilinmiyor: SunValue senaryosunu varsayım olarak kullan
                    cabin_cost = 0.0
                    cabin_status = "included"
                    cabin_source = "sunvalue_assumption"
                    cabin_desc = "8 kg kabin: tahminen dahil · SunValue varsayımı"
                    cabin_is_estimated = True
                    cabin_passes_filter = False  # Doğrulanmış filtreyi geçmez!
            elif airline_norm == "thy":
                cabin_cost = 0.0
                cabin_status = "included"
                cabin_source = "package_rule" if is_ecofly else "thy_ecofly_assumption"
                cabin_desc = "8 kg kabin: dahil · THY EcoFly varsayımı"
                cabin_is_estimated = (not is_ecofly)
                cabin_passes_filter = is_ecofly
            elif airline_norm == "ajet":
                if is_ajet_de_nl_scope:
                    cabin_ref_eur = 15.0
                    converted, note = convert_ref_eur_to_target(cabin_ref_eur, currency, manual_eur_rate)
                    cabin_cost = converted
                    cabin_status = "reference_estimate" if converted is not None else "unknown"
                    cabin_source = "basic_entry"
                    if converted is not None:
                        cabin_desc = f"8 kg kabin: tahmini +€15 ({converted:g} {currency}) · AJet Basic varsayımı: kabin ücretli, bilette kontrol edin"
                    else:
                        cabin_desc = f"8 kg kabin: tahmini +€15 · AJet Basic varsayımı: kabin ücretli, bilette kontrol edin ({note})"
                    cabin_is_estimated = True
                else:
                    cabin_cost = None
                    cabin_status = "unknown"
                    cabin_source = "out_of_scope"
                    cabin_desc = "8 kg kabin: ücret belirsiz · kapsamı doğrulanmamış rota/aktarma"
            elif airline_norm == "pegasus":
                cabin_cost = None
                cabin_status = "unknown"
                cabin_source = "pegasus_light_dynamic"
                cabin_desc = "8 kg kabin: ücret belirsiz · Pegasus Light varsayımı (otomatik tahmin uygulanmaz)"
            else:
                cabin_cost = None
                cabin_status = "unknown"
                cabin_source = "carrier_unknown"
                cabin_desc = "8 kg kabin: ücret belirsiz · taşıyıcı referansı yok"

    # -------------------------------------------------------------
    # 2. UÇAK ALTI BAGAJI DEĞERLENDİRMESİ
    # -------------------------------------------------------------
    checked_required_kg = max(0, int(checked_req or 0))
    checked_cost: Optional[float] = 0.0
    checked_status = "not_needed"
    checked_source = "not_needed"
    checked_desc = "Uçak altı bagajı gerekmiyor"
    checked_ref_eur: Optional[float] = None
    checked_passes_filter = True
    checked_is_estimated = False

    if checked_required_kg > 0:
        # A) Teklifte açıkça "0 kg" veya "dahil değil" okunmuş mu?
        if verified_checked is False or verified_checked_kg == 0:
            checked_passes_filter = False
            # Teklifte 0 kg okunmuşsa paket kuralı ya da varsayım bunu DEĞİŞTİREMEZ!
            if manual_checked_fee is not None:
                checked_cost = round(float(manual_checked_fee), 2)
                checked_status = "manual_estimate"
                checked_source = "manual"
                checked_desc = f"{checked_required_kg} kg uçak altı: manuel tahmin (+{checked_cost:g} {currency}) · teklifte uçak altı 0 kg"
                checked_is_estimated = True
            elif airline_norm == "sunexpress" and (is_sunlight or is_sunlight_scope):
                if is_sunlight_scope and checked_required_kg <= 20:
                    if checked_required_kg <= 5: ref_val = 19.99
                    elif checked_required_kg <= 10: ref_val = 24.99
                    elif checked_required_kg <= 15: ref_val = 29.99
                    else: ref_val = 34.99
                    checked_ref_eur = ref_val
                    converted, note = convert_ref_eur_to_target(ref_val, currency, manual_eur_rate)
                    checked_cost = converted
                    checked_status = "reference_estimate" if converted is not None else "unknown"
                    checked_source = "sunlight_starting"
                    if converted is not None:
                        checked_desc = f"{checked_required_kg} kg uçak altı: tahmini +€{ref_val:g} ({converted:g} {currency}) · SunLight başlangıç fiyatı"
                    else:
                        checked_desc = f"{checked_required_kg} kg uçak altı: tahmini +€{ref_val:g} · SunLight başlangıç fiyatı ({note})"
                    checked_is_estimated = True
                else:
                    checked_cost = None
                    checked_status = "unknown"
                    checked_source = "excess_unverified"
                    checked_desc = f"{checked_required_kg} kg uçak altı: 20 kg üzeri için SunLight başlangıç tarifesi bulunmuyor"
            elif airline_norm == "ajet" and is_ajet_de_nl_scope:
                # Bagajsız bilete ilk uçak altı tarifesi (0–10 kg €12, 11–20 kg €25)
                if checked_required_kg <= 10:
                    ref_val = 12.0
                    tier_label = "0–10 kg paketi"
                elif checked_required_kg <= 20:
                    ref_val = 25.0
                    tier_label = "11–20 kg paketi"
                else:
                    ref_val = None
                    tier_label = None

                if ref_val is not None:
                    checked_ref_eur = ref_val
                    converted, note = convert_ref_eur_to_target(ref_val, currency, manual_eur_rate)
                    checked_cost = converted
                    checked_status = "reference_estimate" if converted is not None else "unknown"
                    checked_source = "basic_entry"
                    if converted is not None:
                        checked_desc = f"{checked_required_kg} kg uçak altı: tahmini +€{ref_val:.0f} ({converted:g} {currency}) · AJet Basic varsayımı ({tier_label})"
                    else:
                        checked_desc = f"{checked_required_kg} kg uçak altı: tahmini +€{ref_val:.0f} · AJet Basic varsayımı ({tier_label}, {note})"
                    checked_is_estimated = True
                else:
                    # 20 kg üzeri: 20 kg'lık paket yeterli sayılmaz!
                    checked_cost = None
                    checked_status = "unknown"
                    checked_source = "excess_unverified"
                    checked_desc = f"{checked_required_kg} kg uçak altı: 20 kg üzeri için online referans paket bulunmuyor"
            else:
                checked_cost = None
                checked_status = "unknown"
                checked_source = "carrier_unknown"
                checked_desc = f"{checked_required_kg} kg uçak altı: dahil değil (ücret doğrulanmadı)"

        # B) Teklif üzerinden doğrulanmış kilo bilgisi mevcut mu?
        elif verified_checked_kg is not None and verified_checked_kg > 0:
            if verified_checked_kg >= checked_required_kg:
                checked_cost = 0.0
                checked_status = "included"
                checked_source = "verified_offer"
                checked_desc = f"{verified_checked_kg} kg uçak altı: dahil · teklif üzerinden doğrulandı"
                checked_passes_filter = True
                checked_is_estimated = False
            else:
                # Eksik kilo mevcut (dahil_kg < istenen_kg)
                missing_kg = checked_required_kg - verified_checked_kg
                checked_passes_filter = False
                # İlk bagaj tarifeleri (0-10/11-20 kg) dahil hakkın üstüne ek kilo için KULLANILAMAZ!
                if manual_checked_fee is not None:
                    checked_cost = round(float(manual_checked_fee), 2)
                    checked_status = "manual_estimate"
                    checked_source = "manual"
                    checked_desc = f"{checked_required_kg} kg uçak altı ({missing_kg} kg eksik): manuel tahmin (+{checked_cost:g} {currency})"
                    checked_is_estimated = True
                else:
                    checked_cost = None
                    checked_status = "unknown"
                    checked_source = "excess_unverified"
                    checked_desc = f"{checked_required_kg} kg uçak altı ({verified_checked_kg} kg dahil, {missing_kg} kg eksik): fazla bagaj ücreti teklifte doğrulanmadı"

        # C) Teklifte "bagaj dahil" işareti var ama kilo yok -> ASLA 20 kg ATAMA!
        elif verified_checked is True and verified_checked_kg is None:
            # "Uçak altı dahil · kilo doğrulanmadı" göster. Doğrulanmış filtreyi geçmez.
            checked_passes_filter = False
            if manual_checked_fee is not None:
                checked_cost = round(float(manual_checked_fee), 2)
                checked_status = "manual_estimate"
                checked_source = "manual"
                checked_desc = f"{checked_required_kg} kg uçak altı: manuel tahmin (+{checked_cost:g} {currency}) · teklifte uçak altı dahil (kilo doğrulanmadı)"
                checked_is_estimated = True
            else:
                checked_cost = None
                checked_status = "unknown"
                checked_source = "unverified_kg"
                checked_desc = f"{checked_required_kg} kg uçak altı: Uçak altı dahil · kilo doğrulanmadı"

        # D) Teklifte uçak altı bilgisi doğrudan belirtilmemiş (verified_checked is None)
        else:
            checked_passes_filter = False

            if manual_checked_fee is not None:
                checked_cost = round(float(manual_checked_fee), 2)
                checked_status = "manual_estimate"
                checked_source = "manual"
                checked_desc = f"{checked_required_kg} kg uçak altı: manuel tahmin (+{checked_cost:g} {currency})"
                checked_is_estimated = True
            elif airline_norm == "sunexpress":
                if is_sunvalue:
                    if checked_required_kg <= 20:
                        checked_cost = 0.0
                        checked_status = "included"
                        checked_source = "package_rule"
                        checked_desc = f"{checked_required_kg} kg uçak altı: dahil (20 kg'a kadar) · SunValue paket kuralı"
                        checked_passes_filter = True
                        checked_is_estimated = False
                    else:
                        # 20 kg üzeri için sabit €15 veya €45 üretme!
                        checked_cost = None
                        checked_status = "unknown"
                        checked_source = "excess_unverified"
                        checked_desc = f"{checked_required_kg} kg uçak altı: 20 kg üzeri paket farkı teklifte doğrulanmadı"
                elif is_sunlight:
                    if is_sunlight_scope and checked_required_kg <= 20:
                        if checked_required_kg <= 5: ref_val = 19.99
                        elif checked_required_kg <= 10: ref_val = 24.99
                        elif checked_required_kg <= 15: ref_val = 29.99
                        else: ref_val = 34.99
                        checked_ref_eur = ref_val
                        converted, note = convert_ref_eur_to_target(ref_val, currency, manual_eur_rate)
                        checked_cost = converted
                        checked_status = "reference_estimate" if converted is not None else "unknown"
                        checked_source = "sunlight_starting"
                        if converted is not None:
                            checked_desc = f"{checked_required_kg} kg uçak altı: tahmini +€{ref_val:g} ({converted:g} {currency}) · SunLight başlangıç fiyatı"
                        else:
                            checked_desc = f"{checked_required_kg} kg uçak altı: tahmini +€{ref_val:g} · SunLight başlangıç fiyatı ({note})"
                        checked_is_estimated = True
                    else:
                        checked_cost = None
                        checked_status = "unknown"
                        checked_source = "excess_unverified"
                        checked_desc = f"{checked_required_kg} kg uçak altı: 20 kg üzeri için SunLight başlangıç tarifesi bulunmuyor"
                else:
                    # Paket bilinmiyor: SunValue senaryosunu varsayım olarak kullan
                    if checked_required_kg <= 20:
                        checked_cost = 0.0
                        checked_status = "included"
                        checked_source = "sunvalue_assumption"
                        checked_desc = f"{checked_required_kg} kg uçak altı: tahminen dahil (20 kg'a kadar) · SunValue varsayımı"
                        checked_is_estimated = True
                        checked_passes_filter = False  # Doğrulanmış filtreyi geçmez!
                    else:
                        # 20 kg üzeri için sabit €15 veya €45 üretme!
                        checked_cost = None
                        checked_status = "unknown"
                        checked_source = "excess_unverified"
                        checked_desc = f"{checked_required_kg} kg uçak altı: 20 kg üzeri için paket farkı doğrulanmadı"
            elif airline_norm == "ajet":
                if is_ajet_de_nl_scope:
                    # AJet Almanya/Hollanda online: 0–10 kg €12, 11–20 kg €25
                    if checked_required_kg <= 10:
                        ref_val = 12.0
                        tier_label = "0–10 kg paketi"
                    elif checked_required_kg <= 20:
                        ref_val = 25.0
                        tier_label = "11–20 kg paketi"
                    else:
                        ref_val = None
                        tier_label = None

                    if ref_val is not None:
                        checked_ref_eur = ref_val
                        converted, note = convert_ref_eur_to_target(ref_val, currency, manual_eur_rate)
                        checked_cost = converted
                        checked_status = "reference_estimate" if converted is not None else "unknown"
                        checked_source = "basic_entry"
                        if converted is not None:
                            checked_desc = f"{checked_required_kg} kg uçak altı: tahmini +€{ref_val:.0f} ({converted:g} {currency}) · AJet Basic varsayımı ({tier_label})"
                        else:
                            checked_desc = f"{checked_required_kg} kg uçak altı: tahmini +€{ref_val:.0f} · AJet Basic varsayımı ({tier_label}, {note})"
                        checked_is_estimated = True
                    else:
                        # 20 kg üzeri için 20 kg paketi yeterli sayılmaz!
                        checked_cost = None
                        checked_status = "unknown"
                        checked_source = "excess_unverified"
                        checked_desc = f"{checked_required_kg} kg uçak altı: 20 kg üzeri için online referans paket bulunmuyor"
                else:
                    checked_cost = None
                    checked_status = "unknown"
                    checked_source = "out_of_scope"
                    checked_desc = f"{checked_required_kg} kg uçak altı: ücret belirsiz · kapsamı doğrulanmamış rota/aktarma"
            elif airline_norm == "thy":
                # EcoFly uçak altı hakkı rotaya bağlıdır; teklifte fiyat yoksa belirsiz
                checked_cost = None
                checked_status = "unknown"
                checked_source = "thy_ecofly_variable"
                checked_desc = f"{checked_required_kg} kg uçak altı: ücret belirsiz · THY EcoFly rotaya bağlı, teklifte fiyat yok"
            elif airline_norm == "pegasus":
                # Pegasus Light dinamik fiyatlandırma; otomatik tahmin uygulanmaz
                checked_cost = None
                checked_status = "unknown"
                checked_source = "pegasus_light_dynamic"
                checked_desc = f"{checked_required_kg} kg uçak altı: ücret belirsiz · Pegasus Light varsayımı (otomatik tahmin uygulanmaz)"
            else:
                checked_cost = None
                checked_status = "unknown"
                checked_source = "carrier_unknown"
                checked_desc = f"{checked_required_kg} kg uçak altı: ücret belirsiz · taşıyıcı referansı yok"

    # -------------------------------------------------------------
    # 3. BACAK TOPLAM VE BİLİNEN MALİYETLER
    # -------------------------------------------------------------
    passes_filter = cabin_passes_filter and checked_passes_filter

    known_cabin = cabin_cost if cabin_cost is not None else 0.0
    known_checked = checked_cost if checked_cost is not None else 0.0
    leg_known_cost = round(known_cabin + known_checked, 2)

    if cabin_cost is not None and checked_cost is not None:
        leg_baggage_cost = leg_known_cost
        leg_cost_known = True
    else:
        leg_baggage_cost = None
        leg_cost_known = False

    is_estimated = (cabin_is_estimated or checked_is_estimated)

    # -------------------------------------------------------------
    # 4. AÇIKLAMA METNİ (Özet)
    # -------------------------------------------------------------
    summary_parts = []
    if airline_norm == "sunexpress" and (not pkg_raw or is_sunvalue):
        if not cabin_required and checked_required_kg == 0:
            if is_sunvalue:
                summary_parts.append("SunValue: 8 kg kabin ve 20 kg kayıtlı bagaj fiyata dahildir")
            else:
                summary_parts.append("SunValue varsayımı · 8 kg kabin + 20 kg uçak altı tahminen dahil")
        elif (cabin_status == "included") and (checked_status == "included" or not checked_required_kg):
            if is_sunvalue:
                summary_parts.append("SunValue paket kuralı · 8 kg kabin ve 20 kg uçak altı fiyata dahildir")
            else:
                summary_parts.append("SunValue varsayımı · 8 kg kabin + 20 kg uçak altı tahminen dahil")
        else:
            if cabin_required:
                summary_parts.append(cabin_desc)
            if checked_required_kg > 0:
                summary_parts.append(checked_desc)
    else:
        if cabin_required:
            summary_parts.append(cabin_desc)
        if checked_required_kg > 0:
            summary_parts.append(checked_desc)

        if not summary_parts:
            if airline_norm == "thy":
                summary_parts.append("THY EcoFly: 8 kg kabin valizi fiyata dahildir (uçak altı rotaya bağlıdır)")
            elif airline_norm == "ajet":
                summary_parts.append("AJet Basic: 4 kg koltuk altı çantası ücretsiz (kabin/uçak altı ek ücrete tabidir)")
            elif airline_norm == "pegasus":
                summary_parts.append("Pegasus Light: 3 kg koltuk altı çantası ücretsiz (kabin/uçak altı ek ücrete tabidir)")
            else:
                summary_parts.append("Standart bilet paketi (ek bagaj seçilmedi)")

    leg_summary = " · ".join(summary_parts)

    return BaggageLegResult(
        cabin_required=cabin_required,
        cabin_cost=cabin_cost,
        cabin_status=cabin_status,
        cabin_source=cabin_source,
        cabin_desc=cabin_desc,
        cabin_ref_eur=cabin_ref_eur,
        cabin_passes_filter=cabin_passes_filter,
        checked_required_kg=checked_required_kg,
        checked_cost=checked_cost,
        checked_status=checked_status,
        checked_source=checked_source,
        checked_desc=checked_desc,
        checked_ref_eur=checked_ref_eur,
        checked_passes_filter=checked_passes_filter,
        leg_baggage_cost=leg_baggage_cost,
        leg_known_cost=leg_known_cost,
        leg_cost_known=leg_cost_known,
        leg_summary_text=leg_summary,
        personal_bag_info=personal_info,
        passes_filter=passes_filter,
        is_estimated=is_estimated,
    )
