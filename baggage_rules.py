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
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple, Set


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
    cabin_source: str
    cabin_desc: str
    cabin_ref_eur: Optional[float]
    cabin_passes_filter: bool

    checked_required_kg: int
    checked_cost: Optional[float]         # None if unknown, 0.0 if included/not needed, or fee
    checked_status: str                   # included | verified_price | reference_estimate | manual_estimate | unknown | not_needed
    checked_source: str
    checked_desc: str
    checked_ref_eur: Optional[float]
    checked_passes_filter: bool

    leg_baggage_cost: Optional[float]     # None if any required item is unknown, else cabin_cost + checked_cost
    leg_cost_known: bool
    leg_summary_text: str
    personal_bag_info: str
    passes_filter: bool

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
            "leg_cost_known": self.leg_cost_known,
            "leg_summary_text": self.leg_summary_text,
            "personal_bag_info": self.personal_bag_info,
            "passes_filter": self.passes_filter,
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
    checked_req: int = 0,                         # 0, 5, 10, 15, 20
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
    1. Doğrulanmış teklif verisi
    2. Kullanıcı manuel tahmini (varsa)
    3. Rota ve pakete uygun resmi referans ücret
    4. Belirsiz (unknown / None)
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
    has_stops = False
    if stops is not None:
        try:
            has_stops = int(stops) > 0
        except (ValueError, TypeError):
            has_stops = False

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

    personal_info = get_personal_bag_info(airline_norm)
    is_uk_route = (origin in UK_AIRPORTS) or (destination in UK_AIRPORTS)
    is_standard_ajet_europe = (
        (origin in STANDARD_AJET_EUROPE_AIRPORTS) or (destination in STANDARD_AJET_EUROPE_AIRPORTS)
    )

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

    if cabin_required:
        # A) Teklif üzerinden doğrulandı mı?
        if verified_cabin is True:
            cabin_cost = 0.0
            cabin_status = "included"
            cabin_source = "verified_offer"
            cabin_desc = "8 kg kabin: dahil · teklif üzerinden doğrulandı"
            cabin_passes_filter = True
        else:
            # Teklifte kabin dahil değil veya bilinmiyor
            if baggage_included_only:
                cabin_passes_filter = False

            # Fiyat belirleme:
            # 1. Manuel kullanıcı ücreti girilmiş mi?
            if manual_cabin_fee is not None:
                cabin_cost = round(float(manual_cabin_fee), 2)
                cabin_status = "manual_estimate"
                cabin_source = "manual"
                cabin_desc = f"8 kg kabin: manuel tahmin (+{cabin_cost:g} {currency})"
            # 2. Aktarmalı rota kontrolü (aktarma sayısıyla çarpılmaz, bilinmiyorsa belirsiz)
            elif has_stops:
                cabin_cost = None
                cabin_status = "unknown"
                cabin_source = "connecting_unverified"
                cabin_desc = "8 kg kabin: aktarmalı rota için doğrulanmadı"
            # 3. Havayolu referans kuralları
            elif airline_norm == "thy":
                # THY EcoFly 8 kg kabin içerir
                cabin_cost = 0.0
                cabin_status = "included"
                cabin_source = "package_rule"
                cabin_desc = "8 kg kabin: dahil · THY EcoFly varsayımı"
            elif airline_norm == "sunexpress":
                # SunExpress SunValue default paketinde 8 kg (55x40x23 cm) kabin valizi fiyata dahildir
                cabin_cost = 0.0
                cabin_status = "included"
                cabin_source = "sunvalue_default"
                cabin_desc = "8 kg kabin: dahil · SunExpress SunValue varsayımı"
            elif airline_norm == "ajet":
                if is_standard_ajet_europe:
                    cabin_ref_eur = 15.0
                    converted, note = convert_ref_eur_to_target(cabin_ref_eur, currency, manual_eur_rate)
                    cabin_cost = converted
                    cabin_status = "reference_estimate" if converted is not None else "unknown"
                    cabin_source = "basic_entry"
                    if converted is not None:
                        cabin_desc = f"8 kg kabin: tahmini +€15 ({converted:g} {currency}) · AJet Basic varsayımı: kabin ücretli, bilette kontrol edin"
                    else:
                        cabin_desc = f"8 kg kabin: tahmini +€15 · AJet Basic varsayımı: kabin ücretli, bilette kontrol edin ({note})"
                else:
                    cabin_cost = None
                    cabin_status = "unknown"
                    cabin_source = "out_of_scope"
                    cabin_desc = "8 kg kabin: ücret belirsiz (standart Almanya/Hollanda dışı hat)"
            elif airline_norm == "pegasus":
                # Eylül 2026 kampanyası tarihli kampanya referansıdır; otomatik tahmin olarak kullanılmaz
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

    if checked_required_kg > 0:
        # A) Teklif üzerinden dahil hak biliniyor mu?
        known_included_kg: Optional[int] = None
        if verified_checked_kg is not None and int(verified_checked_kg) >= 0:
            known_included_kg = int(verified_checked_kg)
        elif verified_checked is True:
            known_included_kg = 20  # Teklifte onaylı bagaj varsa en az standart 20 kg

        if known_included_kg is not None and known_included_kg >= checked_required_kg:
            checked_cost = 0.0
            checked_status = "included"
            checked_source = "verified_offer"
            checked_desc = f"{known_included_kg} kg uçak altı: dahil · teklif üzerinden doğrulandı"
            checked_passes_filter = True
        else:
            if baggage_included_only:
                checked_passes_filter = False

            # Eksik kilo hesabı
            base_included = known_included_kg or 0
            missing_kg = max(0, checked_required_kg - base_included)

            # 1. Manuel kullanıcı ücreti girilmiş mi?
            if manual_checked_fee is not None:
                checked_cost = round(float(manual_checked_fee), 2)
                checked_status = "manual_estimate"
                checked_source = "manual"
                checked_desc = f"{checked_required_kg} kg uçak altı: manuel tahmin (+{checked_cost:g} {currency})"
            # 2. Aktarmalı rota kontrolü
            elif has_stops:
                checked_cost = None
                checked_status = "unknown"
                checked_source = "connecting_unverified"
                checked_desc = f"{checked_required_kg} kg uçak altı: aktarmalı rota için doğrulanmadı"
            # 3. Havayolu referans kuralları
            elif airline_norm == "sunexpress":
                # SunExpress standart Avrupa/uluslararası hatlarında default paket SunValue'dur (20 kg kayıtlı bagaj fiyata dahildir).
                # 20 kg üzeri ihtiyaçlarda SunEco+ (30 kg) ve SunComfort (35 kg) paket yükseltmeleri uygulanır.
                if checked_required_kg <= 20:
                    checked_cost = 0.0
                    checked_status = "included"
                    checked_source = "sunvalue_default"
                    checked_desc = f"{checked_required_kg} kg uçak altı: dahil (20 kg'a kadar) · SunExpress SunValue varsayımı"
                elif checked_required_kg <= 30:
                    # SunEco+ paketi (30 kg) referans paket yükseltme farkı: ~€15.00
                    checked_ref_eur = 15.0
                    converted, note = convert_ref_eur_to_target(checked_ref_eur, currency, manual_eur_rate)
                    checked_cost = converted
                    checked_status = "reference_estimate" if converted is not None else "unknown"
                    checked_source = "suneco_upgrade"
                    if converted is not None:
                        checked_desc = f"{checked_required_kg} kg uçak altı: tahmini +€{checked_ref_eur:.0f} ({converted:g} {currency}) · SunExpress SunEco+ (30 kg) paket yükseltme varsayımı"
                    else:
                        checked_desc = f"{checked_required_kg} kg uçak altı: tahmini +€{checked_ref_eur:.0f} · SunExpress SunEco+ (30 kg) paket yükseltme varsayımı ({note})"
                elif checked_required_kg <= 35:
                    # SunComfort paketi (35 kg) referans paket yükseltme farkı: ~€45.00
                    checked_ref_eur = 45.0
                    converted, note = convert_ref_eur_to_target(checked_ref_eur, currency, manual_eur_rate)
                    checked_cost = converted
                    checked_status = "reference_estimate" if converted is not None else "unknown"
                    checked_source = "suncomfort_upgrade"
                    if converted is not None:
                        checked_desc = f"{checked_required_kg} kg uçak altı: tahmini +€{checked_ref_eur:.0f} ({converted:g} {currency}) · SunExpress SunComfort (35 kg) paket yükseltme varsayımı"
                    else:
                        checked_desc = f"{checked_required_kg} kg uçak altı: tahmini +€{checked_ref_eur:.0f} · SunExpress SunComfort (35 kg) paket yükseltme varsayımı ({note})"
                else:
                    checked_cost = None
                    checked_status = "unknown"
                    checked_source = "excess_unverified"
                    checked_desc = f"{checked_required_kg} kg uçak altı: 35 kg üzeri için fazla bagaj ücreti teklifte doğrulanmadı"
            elif airline_norm == "ajet":
                if is_standard_ajet_europe:
                    # AJet Almanya/Hollanda online: 0–10 kg €12, 11–20 kg €25
                    if missing_kg <= 10:
                        ref_val = 12.0
                        tier_label = "0–10 kg paketi"
                    else:
                        ref_val = 25.0
                        tier_label = "11–20 kg paketi"

                    checked_ref_eur = ref_val
                    converted, note = convert_ref_eur_to_target(checked_ref_eur, currency, manual_eur_rate)
                    checked_cost = converted
                    checked_status = "reference_estimate" if converted is not None else "unknown"
                    checked_source = "basic_entry"
                    if converted is not None:
                        checked_desc = f"{checked_required_kg} kg uçak altı: tahmini +€{checked_ref_eur:.0f} ({converted:g} {currency}) · AJet Basic varsayımı ({tier_label})"
                    else:
                        checked_desc = f"{checked_required_kg} kg uçak altı: tahmini +€{checked_ref_eur:.0f} · AJet Basic varsayımı ({tier_label}, {note})"
                else:
                    checked_cost = None
                    checked_status = "unknown"
                    checked_source = "out_of_scope"
                    checked_desc = f"{checked_required_kg} kg uçak altı: ücret belirsiz (standart Almanya/Hollanda dışı hat)"
            elif airline_norm == "thy":
                # EcoFly uçak altı hakkı rotaya bağlıdır; her bilete sabit 15 kg veya 0 kg atanmaz
                checked_cost = None
                checked_status = "unknown"
                checked_source = "thy_ecofly_variable"
                checked_desc = f"{checked_required_kg} kg uçak altı: ücret belirsiz · THY EcoFly rotaya bağlı, teklifte fiyat yok"
            elif airline_norm == "pegasus":
                # Pegasus Light dinamik fiyatlandırma; Eylül 2026 kampanya rakamları otomatik tahmin olarak uygulanmaz
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
    # 3. BACAK TOPLAM BAGAJ MALİYETİ
    # -------------------------------------------------------------
    passes_filter = cabin_passes_filter and checked_passes_filter

    if cabin_cost is not None and checked_cost is not None:
        leg_baggage_cost = round(cabin_cost + checked_cost, 2)
        leg_cost_known = True
    else:
        leg_baggage_cost = None
        leg_cost_known = False

    summary_parts = []
    if cabin_required:
        summary_parts.append(cabin_desc)
    if checked_required_kg > 0:
        summary_parts.append(checked_desc)

    if not summary_parts:
        if airline_norm == "sunexpress":
            summary_parts.append("SunExpress SunValue: 8 kg kabin ve 20 kg kayıtlı bagaj fiyata dahildir")
        elif airline_norm == "thy":
            summary_parts.append("THY EcoFly: 8 kg kabin valizi fiyata dahildir (uçak altı rotaya bağlıdır)")
        elif airline_norm == "ajet":
            summary_parts.append("AJet Basic: 4 kg koltuk altı çantası ücretsiz (kabin/uçak altı ek ücrete tabidir)")
        elif airline_norm == "pegasus":
            summary_parts.append("Pegasus Light: 3 kg koltuk altı çantası ücretsiz (kabin/uçak altı ek ücrete tabidir)")
        else:
            summary_parts.append("Standart bilet paketi (ek bagaj seçilmedi)")
    else:
        # SunExpress için tek bir ürün seçilmişse diğer dahil hakkı da kullanıcıya net şekilde hatırlat
        if airline_norm == "sunexpress":
            if cabin_required and checked_required_kg == 0:
                summary_parts.append("SunValue: 20 kg kayıtlı bagaj da fiyata dahildir")
            elif not cabin_required and checked_required_kg <= 20:
                summary_parts.append("SunValue: 8 kg kabin valizi de fiyata dahildir")

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
        leg_cost_known=leg_cost_known,
        leg_summary_text=leg_summary,
        personal_bag_info=personal_info,
        passes_filter=passes_filter,
    )
