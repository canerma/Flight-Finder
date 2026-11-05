from datetime import date, timedelta
import pandas as pd
import streamlit as st

from scraper import scrape_decoupled_roundtrip
from combiner import (
    build_combinations_dataframe,
    compute_effective_date_ranges,
    DEFAULT_TRANSFER_PRESETS_EUR,
)


st.set_page_config(
    page_title="Smart Flight Finder | Akıllı Uçuş Bulucu",
    page_icon="✈️",
    layout="wide",
)

st.title("✈️ Smart Flight Finder (Kişisel Akıllı Uçuş Bulucu)")
st.caption(
    "Ayrık Tek Yön (Decoupled One-Way) tarama ve kartezyen çarpım ile "
    "yakın alternatif havalimanları (en fazla 3 hedef) arasındaki en ucuz simetrik/asimetrik gidiş-dönüş kombinasyonlarını bulur. "
    "Sonuçlar iki bağımsız tek yön teklifin toplamıdır."
)

# Session state başlatma
if "outbound_flights" not in st.session_state:
    st.session_state.outbound_flights = []
if "inbound_flights" not in st.session_state:
    st.session_state.inbound_flights = []
if "last_search_sig" not in st.session_state:
    st.session_state.last_search_sig = None
if "last_stats" not in st.session_state:
    st.session_state.last_stats = None

# ---------------- SIDEBAR: ARAMA PARAMETRELERİ ----------------
with st.sidebar:
    st.header("🔍 Rota & Tarih Ayarları")

    home_airport = st.text_input(
        "Kalkış Havalimanı (IATA Kodu)",
        value="ESB",
        help="Örn: ESB (Ankara), SAW veya IST (İstanbul), ADB (İzmir)",
    ).upper().strip()

    target_airports_raw = st.text_input(
        "Hedef Yakın Havalimanları (Virgülle, en fazla 3)",
        value="AMS, DUS, CGN",
        help="En fazla 3 benzersiz hedef havalimanı seçebilirsiniz (kalkış havalimanı hariç).",
    )
    raw_targets = [
        code.strip().upper()
        for code in target_airports_raw.split(",")
        if code.strip()
    ]
    target_airports = []
    for code in raw_targets:
        if code != home_airport and code not in target_airports:
            target_airports.append(code)

    if len(target_airports) > 3:
        st.warning("En fazla 3 hedef havalimanı seçilebilir. İlk 3 havalimanı kullanılacak.")
        target_airports = target_airports[:3]

    st.subheader("📅 Seyahat Tarih Aralığı")
    default_win_start = date.today() + timedelta(days=10)
    default_win_end = default_win_start + timedelta(days=14)

    col_d1, col_d2 = st.columns(2)
    with col_d1:
        window_start = st.date_input("En Erken Gidiş", value=default_win_start)
    with col_d2:
        window_end = st.date_input("Eve En Geç Varış", value=default_win_end)

    stay_range = st.slider(
        "Hedefte Kalış Süresi (Gece)",
        min_value=1,
        max_value=30,
        value=(4, 6),
        help="Gidişin hedefe varış yerel tarihi ile dönüşün kalkış yerel tarihi arasındaki gece farkı",
    )
    min_stay_days, max_stay_days = stay_range

    win_start_str = window_start.strftime("%Y-%m-%d")
    win_end_str = window_end.strftime("%Y-%m-%d")

    try:
        out_start_str, out_end_str, in_start_str, in_end_str = compute_effective_date_ranges(
            win_start_str, win_end_str, min_stay_days
        )
        out_start = date.fromisoformat(out_start_str)
        out_end = date.fromisoformat(out_end_str)
        in_start = date.fromisoformat(in_start_str)
        in_end = date.fromisoformat(in_end_str)
    except ValueError:
        out_start = window_start
        out_end = window_end - timedelta(days=min_stay_days)
        in_start = window_start + timedelta(days=min_stay_days)
        in_end = window_end

    currency = st.selectbox("Para Birimi", options=["TRY", "EUR", "USD"], index=0)

    manual_exchange_rate = 1.0
    if currency != "EUR":
        manual_exchange_rate = st.number_input(
            f"Manuel 1 EUR → {currency} Kuru (Transfer Tahmini İçin)",
            min_value=0.01,
            value=38.5 if currency == "TRY" else 1.08,
            step=0.5 if currency == "TRY" else 0.05,
        )

    st.divider()
    st.header("⚙️ Filtre & Kombinasyon Ayarları")

    stops_option = st.selectbox(
        "Maksimum Aktarma",
        options=["Tümü", "Sadece Aktarmasız (Direkt)", "Maksimum 1 Aktarma"],
        index=0,
    )
    if stops_option == "Sadece Aktarmasız (Direkt)":
        max_stops = 0
    elif stops_option == "Maksimum 1 Aktarma":
        max_stops = 1
    else:
        max_stops = None

    only_asymmetric = st.checkbox(
        "Sadece asimetrik rotaları göster (Farklı havalimanından dönüş)",
        value=False,
    )

    st.divider()
    st.header("🚆 Kara Ulaşımı (Transfer) Tahmini")
    include_transfer = st.checkbox(
        "Nihai varış noktasına tren/otobüs tahmini maliyetini ekle",
        value=False,
    )

    transfer_costs = {}
    transfer_durations = {}

    if include_transfer:
        st.caption(
            f"Tahmini tek yön transfer ücretleri (**{currency}**, kullanılan kur: 1 EUR = {manual_exchange_rate} {currency}):"
        )
        for airport in target_airports:
            preset = DEFAULT_TRANSFER_PRESETS_EUR.get(
                airport, {"cost": 20.0, "duration_min": 90}
            )
            c1, c2 = st.columns(2)
            with c1:
                transfer_costs[airport] = st.number_input(
                    f"{airport} Tahmini Ücret ({currency})",
                    min_value=0.0,
                    value=round(preset["cost"] * manual_exchange_rate, 0),
                    step=10.0 if currency == "TRY" else 1.0,
                    key=f"cost_{airport}_{currency}_{manual_exchange_rate}",
                )
            with c2:
                transfer_durations[airport] = int(
                    st.number_input(
                        f"{airport} Tahmini Süre (dk)",
                        min_value=0,
                        value=int(preset["duration_min"]),
                        step=5,
                        key=f"dur_{airport}",
                    )
                )

    st.divider()
    with st.expander("🛠️ Gelişmiş Scraper Ayarları"):
        max_results_per_route = st.slider(
            "Rota/Gün başına çekilecek maks. uçuş", 1, 25, 10
        )
        max_concurrent_pages = st.slider("Paralel sekme sayısı", 1, 6, 3)
        headless_mode = st.checkbox("Headless Mod (Tarayıcıyı gizle)", value=True)
        only_retry_failed = st.checkbox("Sadece eksik/hatalı sorguları tara", value=True)

    out_days = max(0, (out_end - out_start).days + 1)
    in_days = max(0, (in_end - in_start).days + 1)
    total_queries = len(target_airports) * (out_days + in_days)

    st.info(
        f"**Tarama Özeti:**\n"
        f"- Gidiş: {len(target_airports)} havalimanı × {out_days} gün = **{len(target_airports) * out_days}** arama\n"
        f"- Dönüş: {len(target_airports)} havalimanı × {in_days} gün = **{len(target_airports) * in_days}** arama\n"
        f"- Toplam: **{total_queries}** tek yön sorgu"
    )

    scrape_btn = st.button(
        "🚀 Uçuşları Tara ve Kombinasyonları Bul",
        type="primary",
        use_container_width=True,
    )

current_search_sig = (
    home_airport,
    tuple(target_airports),
    win_start_str,
    win_end_str,
    currency,
    max_results_per_route,
)

# ---------------- TARAMA İŞLEMİ ----------------
if scrape_btn:
    if not home_airport or not target_airports:
        st.error("Lütfen kalkış ve en az 1 (en fazla 3) varış havalimanı kodu girin.")
    elif out_end < out_start or in_end < in_start:
        st.error("Seçilen tarih aralığı minimum kalış süresi için yetersiz.")
    else:
        progress_bar = st.progress(0, text="Playwright başlatılıyor...")

        def update_progress(done: int, total: int, label: str, stats: dict = None):
            pct = int((done / total) * 100) if total > 0 else 0
            progress_bar.progress(pct, text=f"[{done}/{total}] {label}")

        with st.spinner("Google Flights üzerinden tek yön uçuşlar taranıyor..."):
            out_flights, in_flights, stats = scrape_decoupled_roundtrip(
                home_airport=home_airport,
                target_airports=target_airports,
                outbound_start_date=out_start.strftime("%Y-%m-%d"),
                outbound_end_date=out_end.strftime("%Y-%m-%d"),
                inbound_start_date=in_start.strftime("%Y-%m-%d"),
                inbound_end_date=in_end.strftime("%Y-%m-%d"),
                currency=currency,
                max_results_per_route=max_results_per_route,
                max_concurrent_pages=max_concurrent_pages,
                headless=headless_mode,
                only_retry_failed=only_retry_failed,
                progress_callback=update_progress,
            )

        st.session_state.outbound_flights = out_flights
        st.session_state.inbound_flights = in_flights
        st.session_state.last_search_sig = current_search_sig
        st.session_state.last_stats = stats
        progress_bar.empty()

        if stats.get("failed_queries", 0) > 0:
            st.warning(
                f"Kısmi sonuç: {stats.get('success_queries', 0)} başarılı, "
                f"{stats.get('no_results_queries', 0)} uçuş yok, "
                f"{stats.get('failed_queries', 0)} hatalı sorgu."
            )
        else:
            st.success(
                f"Tarama tamamlandı! {len(out_flights)} gidiş ve {len(in_flights)} dönüş uçuşu bulundu."
            )

# ---------------- SONUÇLARI BİRLEŞTİRME VE GÖSTERİM ----------------
if st.session_state.last_search_sig != current_search_sig:
    st.info(
        "Bu arama için henüz veri yok. Rota, tarih veya para birimi değiştirildi; "
        "lütfen **'Uçuşları Tara ve Kombinasyonları Bul'** butonuna tıklayın."
    )
else:
    out_flights = st.session_state.outbound_flights
    in_flights = st.session_state.inbound_flights
    active_curr = currency

    combos_df = build_combinations_dataframe(
        outbound_flights=out_flights,
        inbound_flights=in_flights,
        min_stay_days=min_stay_days,
        max_stay_days=max_stay_days,
        window_start=win_start_str,
        window_end=win_end_str,
        expected_currency=active_curr,
        max_stops=max_stops,
        include_transfer=include_transfer,
        transfer_costs=transfer_costs,
        transfer_durations=transfer_durations,
    )

    if only_asymmetric and not combos_df.empty:
        combos_df = combos_df[combos_df["is_asymmetric"]].reset_index(drop=True)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Taranan Gidiş Uçuşu", len(out_flights))
    m2.metric("Taranan Dönüş Uçuşu", len(in_flights))
    m3.metric("Uygun Kombinasyon", len(combos_df))
    if not combos_df.empty:
        best_price = combos_df.iloc[0]["grand_total_price"]
        m4.metric("En Düşük Toplam Maliyet", f"{best_price:,.0f} {active_curr}")
    else:
        m4.metric("En Düşük Toplam Maliyet", "-")

    tab_combos, tab_cards, tab_raw = st.tabs(
        [
            "📊 Tüm Kombinasyonlar Tablosu",
            "🏆 En İyi 10 Seçenek (Detaylı Kartlar)",
            "🛫 Ham Tek Yön Uçuşlar",
        ]
    )

    with tab_combos:
        if combos_df.empty:
            st.warning(
                "Seçilen kriterlere uygun kombinasyon bulunamadı."
            )
        else:
            st.dataframe(combos_df, use_container_width=True, hide_index=True)

    with tab_cards:
        if combos_df.empty:
            st.info("Gösterilecek kombinasyon yok.")
        else:
            for idx, row in combos_df.head(10).iterrows():
                asym_badge = "🔀 Asimetrik Rota" if row["is_asymmetric"] else "🔁 Simetrik Rota"
                with st.container(border=True):
                    c_head1, c_head2 = st.columns([3, 1])
                    with c_head1:
                        st.subheader(
                            f"#{idx + 1} | {row['route_summary']} ({row['stay_days']} Gece Kalış) — {asym_badge}"
                        )
                    with c_head2:
                        st.markdown(
                            f"### 💰 {row['grand_total_price']:,.0f} {active_curr}"
                        )

                    col_out, col_in = st.columns(2)
                    with col_out:
                        st.markdown(
                            f"**🛫 Gidiş: {row['out_origin']} → {row['out_destination']}** (Kalkış: {row['out_date']}, Varış: {row['out_arrival_date'] or 'Doğrulanmadı'})\n"
                            f"- **Havayolu:** {row['out_airline']}\n"
                            f"- **Saat:** {row['out_departure_time'] or 'Doğrulanmadı'} – {row['out_arrival_time'] or 'Doğrulanmadı'} ({row['out_duration'] or 'Doğrulanmadı'})\n"
                            f"- **Aktarma:** {row['out_stops_text'] or 'Doğrulanmadı'}\n"
                            f"- **Fiyat:** {row['out_price']:,.0f} {active_curr}"
                        )
                        st.link_button(
                            f"Google Flights'ta Aramayı Aç ↗ ({row['out_origin']}→{row['out_destination']})",
                            row["out_booking_url"],
                        )

                    with col_in:
                        st.markdown(
                            f"**🛬 Dönüş: {row['in_origin']} → {row['in_destination']}** (Kalkış: {row['in_date']}, Varış: {row['in_arrival_date'] or 'Doğrulanmadı'})\n"
                            f"- **Havayolu:** {row['in_airline']}\n"
                            f"- **Saat:** {row['in_departure_time'] or 'Doğrulanmadı'} – {row['in_arrival_time'] or 'Doğrulanmadı'} ({row['in_duration'] or 'Doğrulanmadı'})\n"
                            f"- **Aktarma:** {row['in_stops_text'] or 'Doğrulanmadı'}\n"
                            f"- **Fiyat:** {row['in_price']:,.0f} {active_curr}"
                        )
                        st.link_button(
                            f"Google Flights'ta Aramayı Aç ↗ ({row['in_origin']}→{row['in_destination']})",
                            row["in_booking_url"],
                        )

    with tab_raw:
        r_col1, r_col2 = st.columns(2)
        with r_col1:
            st.subheader(f"Gidiş Uçuşları ({len(out_flights)})")
            if out_flights:
                st.dataframe(
                    pd.DataFrame([f.to_dict() for f in out_flights]),
                    use_container_width=True,
                    hide_index=True,
                )
        with r_col2:
            st.subheader(f"Dönüş Uçuşları ({len(in_flights)})")
            if in_flights:
                st.dataframe(
                    pd.DataFrame([f.to_dict() for f in in_flights]),
                    use_container_width=True,
                    hide_index=True,
                )
