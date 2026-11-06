import json
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import scraper
import pandas as pd
from scraper import (
    FlightOption,
    RouteQueryResult,
    _cache_key,
    clear_daily_cache,
    get_cache_epoch,
    is_cache_entry_usable,
    load_daily_cache,
    match_selected_flight,
    parse_flights_from_html,
    update_single_cache_entry,
)
from combiner import (
    build_combinations_dataframe,
    sanitize_records_for_json,
)
import server


FIXTURES_DIR = Path(__file__).parent / "fixtures"


class SmartFlightFinderRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orig_cache_file = scraper.CACHE_FILE_PATH
        scraper.CACHE_FILE_PATH = str(Path(self.temp_dir.name) / "test_flight_cache.json")
        clear_daily_cache()

        with server.JOB_LOCK:
            server.SCRAPE_STATUS.update(
                {
                    "running": False,
                    "stopping": False,
                    "state": "idle",
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
            )

    def tearDown(self):
        scraper.CACHE_FILE_PATH = self.orig_cache_file
        self.temp_dir.cleanup()

    def _start_test_http_server(self):
        httpd = HTTPServer(("127.0.0.1", 0), server.FlightFinderHandler)
        port = httpd.server_port
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return httpd, f"http://127.0.0.1:{port}"

    # 1. /api/progress ve /api/progress?t=123 aynı JSON endpoint'ine ulaşır
    def test_01_progress_endpoint_with_and_without_query_string(self):
        httpd, base_url = self._start_test_http_server()
        try:
            with urlopen(f"{base_url}/api/progress") as r1:
                self.assertEqual(r1.status, 200)
                data1 = json.loads(r1.read().decode("utf-8"))

            with urlopen(f"{base_url}/api/progress?t=123456789") as r2:
                self.assertEqual(r2.status, 200)
                data2 = json.loads(r2.read().decode("utf-8"))

            self.assertEqual(data1["running"], data2["running"])
            self.assertEqual(data1["state"], data2["state"])
            self.assertIn("cache_error", data1)

            with urlopen(f"{base_url}/api/cache?t=999") as r3:
                self.assertEqual(r3.status, 200)
                cache_data = json.loads(r3.read().decode("utf-8"))
                self.assertIn("cached_queries", cache_data)
        finally:
            httpd.shutdown()
            httpd.server_close()

    # 2. Yeni rota/tarih/para biriminde eski arama verileri gösterilmez
    def test_02_no_stale_search_data_fallback_across_route_date_or_currency(self):
        epoch = get_cache_epoch()
        out_flight = FlightOption(
            origin="ESB",
            destination="AMS",
            date="2026-11-15",
            airline="Pegasus",
            departure_time="08:00",
            arrival_time="11:00",
            duration="4 sa",
            duration_minutes=240,
            stops=0,
            stops_text="Aktarmasız",
            price=3500.0,
            currency="TRY",
            booking_url="https://www.google.com/travel/flights",
            arrival_day_offset=0,
            arrival_date="2026-11-15",
            cabin_baggage_included=True,
        )
        in_flight = FlightOption(
            origin="AMS",
            destination="ESB",
            date="2026-11-20",
            airline="Pegasus",
            departure_time="14:00",
            arrival_time="19:00",
            duration="4 sa",
            duration_minutes=240,
            stops=0,
            stops_text="Aktarmasız",
            price=3700.0,
            currency="TRY",
            booking_url="https://www.google.com/travel/flights",
            arrival_day_offset=0,
            arrival_date="2026-11-20",
            cabin_baggage_included=True,
        )
        update_single_cache_entry(
            _cache_key("ESB", "AMS", "2026-11-15", "TRY"),
            RouteQueryResult(
                origin="ESB",
                destination="AMS",
                date="2026-11-15",
                currency="TRY",
                status="success",
                flights=[out_flight],
                requested_limit=8,
                collected_count=1,
                total_dom_cards=1,
                scope_complete=True,
                scope_note="OK",
                fetched_at="2026-11-05T12:00:00",
            ).to_cache_entry(),
            expected_epoch=epoch,
        )
        update_single_cache_entry(
            _cache_key("AMS", "ESB", "2026-11-20", "TRY"),
            RouteQueryResult(
                origin="AMS",
                destination="ESB",
                date="2026-11-20",
                currency="TRY",
                status="success",
                flights=[in_flight],
                requested_limit=8,
                collected_count=1,
                total_dom_cards=1,
                scope_complete=True,
                scope_note="OK",
                fetched_at="2026-11-05T12:00:00",
            ).to_cache_entry(),
            expected_epoch=epoch,
        )

        httpd, base_url = self._start_test_http_server()
        try:
            # Farklı hedef havalimanı (CDG) sorulduğunda eski arama dönmemeli
            req_body = json.dumps(
                {
                    "home_airport": "ESB",
                    "target_airports": ["CDG"],
                    "window_start": "2026-11-15",
                    "window_end": "2026-11-20",
                    "min_stay_days": 4,
                    "max_stay_days": 6,
                    "currency": "TRY",
                    "max_results_per_route": 8,
                    "client_request_id": 42,
                }
            ).encode("utf-8")
            req = Request(
                f"{base_url}/api/combine?t=111",
                data=req_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                self.assertFalse(payload["has_data"])
                self.assertEqual(payload["combinations"], [])
                self.assertEqual(payload["outbound_count"], 0)
                self.assertEqual(payload["inbound_count"], 0)
                self.assertEqual(payload["client_request_id"], 42)
                self.assertIn("Bu arama için henüz veri yok", payload["message"])

            # Aynı rota ama farklı para birimi (EUR) sorulduğunda TRY verileri EUR gibi sunulmamalı
            req_eur = Request(
                f"{base_url}/api/combine",
                data=json.dumps(
                    {
                        "home_airport": "ESB",
                        "target_airports": ["AMS"],
                        "window_start": "2026-11-15",
                        "window_end": "2026-11-20",
                        "min_stay_days": 4,
                        "max_stay_days": 6,
                        "currency": "EUR",
                        "max_results_per_route": 8,
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req_eur) as resp_eur:
                payload_eur = json.loads(resp_eur.read().decode("utf-8"))
                self.assertFalse(payload_eur["has_data"])
                self.assertEqual(payload_eur["combinations"], [])
        finally:
            httpd.shutdown()
            httpd.server_close()

    # 3. Hatalı tarama gerçek no_results olarak saklanmaz + Kaydedilmiş örnek HTML testleri
    def test_03_scrape_errors_separated_from_no_results_and_fixture_parsing(self):
        # 3a: Kaydedilmiş sample_flights.html -> success, mükerrer kart elenir, fiyata göre sıralanır
        sample_html = (FIXTURES_DIR / "sample_flights.html").read_text(encoding="utf-8")
        res_ok = parse_flights_from_html(
            html_content=sample_html,
            origin="ESB",
            destination="AMS",
            date_str="2026-11-15",
            currency="TRY",
            booking_url="https://example.com",
            requested_limit=8,
        )
        self.assertEqual(res_ok.status, "success")
        self.assertEqual(len(res_ok.flights), 2)  # 3 DOM kartından 1'i mükerrer, 2 benzersiz kaldı
        self.assertEqual(res_ok.flights[0].price, 2850.0)
        self.assertEqual(res_ok.flights[0].arrival_day_offset, 1)
        self.assertEqual(res_ok.flights[0].arrival_date, "2026-11-16")
        self.assertIsNone(res_ok.flights[0].cabin_baggage_included)
        self.assertEqual(res_ok.flights[1].price, 3450.0)
        self.assertTrue(res_ok.flights[1].cabin_baggage_included)

        # 3b: Kaydedilmiş no_results.html -> gerçek no_results
        no_res_html = (FIXTURES_DIR / "no_results.html").read_text(encoding="utf-8")
        res_empty = parse_flights_from_html(
            html_content=no_res_html,
            origin="ESB",
            destination="EIN",
            date_str="2026-11-15",
            currency="TRY",
            booking_url="https://example.com",
            requested_limit=8,
        )
        self.assertEqual(res_empty.status, "no_results")
        self.assertTrue(is_cache_entry_usable(res_empty.to_cache_entry(), 8))

        # 3c: Kaydedilmiş blocked_captcha.html -> blocked (asla kullanılabilir önbellek sayılmaz)
        blocked_html = (FIXTURES_DIR / "blocked_captcha.html").read_text(encoding="utf-8")
        res_blocked = parse_flights_from_html(
            html_content=blocked_html,
            origin="ESB",
            destination="AMS",
            date_str="2026-11-15",
            currency="TRY",
            booking_url="https://example.com",
            requested_limit=8,
        )
        self.assertEqual(res_blocked.status, "blocked")
        self.assertFalse(is_cache_entry_usable(res_blocked.to_cache_entry(), 8))

        # 3d: Timeout veya parse_error durumları da önbellekten geçerli sonuç olarak sunulmaz
        res_timeout = RouteQueryResult(
            origin="ESB",
            destination="DUS",
            date="2026-11-15",
            currency="TRY",
            status="timeout",
            flights=[],
            requested_limit=8,
            collected_count=0,
            total_dom_cards=0,
            scope_complete=False,
            scope_note="Zaman aşımı",
            fetched_at="2026-11-05T12:00:00",
            error_message="Zaman aşımı",
        )
        self.assertFalse(is_cache_entry_usable(res_timeout.to_cache_entry(), 8))

    # 4. Bilinmeyen aktarma direkt filtresinden geçmez
    def test_04_unknown_stops_excluded_from_direct_filter(self):
        out_unknown_stops = FlightOption(
            origin="ESB",
            destination="AMS",
            date="2026-11-15",
            airline="TestAir",
            departure_time="10:00",
            arrival_time="13:00",
            duration="3 sa",
            duration_minutes=180,
            stops=None,
            stops_text=None,
            price=2000.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=0,
            arrival_date="2026-11-15",
        )
        in_direct = FlightOption(
            origin="AMS",
            destination="ESB",
            date="2026-11-20",
            airline="TestAir",
            departure_time="15:00",
            arrival_time="19:00",
            duration="3 sa",
            duration_minutes=180,
            stops=0,
            stops_text="Aktarmasız",
            price=2000.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=0,
            arrival_date="2026-11-20",
        )
        df_direct = build_combinations_dataframe(
            outbound_flights=[out_unknown_stops],
            inbound_flights=[in_direct],
            min_stay_days=4,
            max_stay_days=6,
            window_start="2026-11-15",
            window_end="2026-11-25",
            expected_currency="TRY",
            max_stops=0,
        )
        self.assertTrue(df_direct.empty)

        df_all = build_combinations_dataframe(
            outbound_flights=[out_unknown_stops],
            inbound_flights=[in_direct],
            min_stay_days=4,
            max_stay_days=6,
            window_start="2026-11-15",
            window_end="2026-11-25",
            expected_currency="TRY",
            max_stops=None,
        )
        self.assertEqual(len(df_all), 1)
        records = sanitize_records_for_json(df_all.to_dict(orient="records"))
        self.assertIsNone(records[0]["out_stops"])

        # Sadece gidiş aktarmasız (out_max_stops=0, in_max_stops=None) seçildiğinde:
        # Gidiş aktarmasız (0), dönüş 1 aktarmalı olan kombinasyon geçmeli!
        out_direct = FlightOption(
            origin="ESB",
            destination="AMS",
            date="2026-11-15",
            airline="TestAir",
            departure_time="10:00",
            arrival_time="13:00",
            duration="3 sa",
            duration_minutes=180,
            stops=0,
            stops_text="Aktarmasız",
            price=2000.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=0,
            arrival_date="2026-11-15",
        )
        in_1stop = FlightOption(
            origin="AMS",
            destination="ESB",
            date="2026-11-20",
            airline="TestAir",
            departure_time="15:00",
            arrival_time="21:00",
            duration="6 sa",
            duration_minutes=360,
            stops=1,
            stops_text="1 aktarma",
            price=1800.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=0,
            arrival_date="2026-11-20",
        )
        df_out_only = build_combinations_dataframe(
            outbound_flights=[out_direct, out_unknown_stops],
            inbound_flights=[in_1stop],
            min_stay_days=4,
            max_stay_days=6,
            window_start="2026-11-15",
            window_end="2026-11-25",
            expected_currency="TRY",
            out_max_stops=0,
            in_max_stops=None,
        )
        self.assertEqual(len(df_out_only), 1)
        self.assertEqual(int(df_out_only.iloc[0]["out_stops"]), 0)
        self.assertEqual(int(df_out_only.iloc[0]["in_stops"]), 1)

        # Toplam seyahat süresi filtresi (max_total_travel_hours=8.0 -> 480 dk):
        # 180 + 360 = 540 dk (9 saat) olduğu için 8 saat sınırında elenmeli, 10 saat sınırında geçmeli
        df_total_8h = build_combinations_dataframe(
            outbound_flights=[out_direct],
            inbound_flights=[in_1stop],
            min_stay_days=4,
            max_stay_days=6,
            window_start="2026-11-15",
            window_end="2026-11-25",
            expected_currency="TRY",
            max_total_travel_hours=8.0,
        )
        self.assertTrue(df_total_8h.empty)

        df_total_10h = build_combinations_dataframe(
            outbound_flights=[out_direct],
            inbound_flights=[in_1stop],
            min_stay_days=4,
            max_stay_days=6,
            window_start="2026-11-15",
            window_end="2026-11-25",
            expected_currency="TRY",
            max_total_travel_hours=10.0,
        )
        self.assertEqual(len(df_total_10h), 1)

    # 5. Bilinmeyen süre maksimum süre filtresinden geçmez ve en kısa uçuş gibi sıralanmaz
    def test_05_unknown_duration_and_departure_time_handling(self):
        out_unknown_dur = FlightOption(
            origin="ESB",
            destination="AMS",
            date="2026-11-15",
            airline="UnknownDurAir",
            departure_time=None,
            arrival_time="13:00",
            duration=None,
            duration_minutes=None,
            stops=0,
            stops_text="Aktarmasız",
            price=2500.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=0,
            arrival_date="2026-11-15",
        )
        out_known_dur = FlightOption(
            origin="ESB",
            destination="AMS",
            date="2026-11-15",
            airline="KnownDurAir",
            departure_time="09:00",
            arrival_time="13:00",
            duration="4 sa",
            duration_minutes=240,
            stops=0,
            stops_text="Aktarmasız",
            price=2500.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=0,
            arrival_date="2026-11-15",
        )
        in_known = FlightOption(
            origin="AMS",
            destination="ESB",
            date="2026-11-20",
            airline="KnownDurAir",
            departure_time="14:00",
            arrival_time="18:00",
            duration="4 sa",
            duration_minutes=240,
            stops=0,
            stops_text="Aktarmasız",
            price=2500.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=0,
            arrival_date="2026-11-20",
        )

        # Maksimum süre filtresi (max_flight_hours=6) aktifken süresi bilinmeyen uçuş geçmemeli
        df_filtered = build_combinations_dataframe(
            outbound_flights=[out_unknown_dur, out_known_dur],
            inbound_flights=[in_known],
            min_stay_days=4,
            max_stay_days=6,
            window_start="2026-11-15",
            window_end="2026-11-25",
            expected_currency="TRY",
            max_flight_hours=6.0,
        )
        self.assertEqual(len(df_filtered), 1)
        self.assertEqual(df_filtered.iloc[0]["out_airline"], "KnownDurAir")

        # Kalkış saati filtresi aktifken saati bilinmeyen (None) uçuş 12:00 varsayılıp geçmemeli
        df_time_filtered = build_combinations_dataframe(
            outbound_flights=[out_unknown_dur, out_known_dur],
            inbound_flights=[in_known],
            min_stay_days=4,
            max_stay_days=6,
            window_start="2026-11-15",
            window_end="2026-11-25",
            expected_currency="TRY",
            out_dep_min_time="08:00",
            out_dep_max_time="18:00",
        )
        self.assertEqual(len(df_time_filtered), 1)
        self.assertEqual(df_time_filtered.iloc[0]["out_airline"], "KnownDurAir")

        # Aynı fiyatta süresi bilinen uçuş, süresi bilinmeyenden (None) önce sıralanmalı
        df_sorted = build_combinations_dataframe(
            outbound_flights=[out_unknown_dur, out_known_dur],
            inbound_flights=[in_known],
            min_stay_days=4,
            max_stay_days=6,
            window_start="2026-11-15",
            window_end="2026-11-25",
            expected_currency="TRY",
        )
        self.assertEqual(len(df_sorted), 2)
        self.assertEqual(df_sorted.iloc[0]["out_airline"], "KnownDurAir")
        records = sanitize_records_for_json(df_sorted.to_dict(orient="records"))
        self.assertIsNone(records[1]["total_travel_minutes"])

    # 6. Ertesi gün varışta gece sayısı doğru hesaplanır (1 Aralık kalkış, 2 Aralık varış, 6 Aralık dönüş = 4 gece)
    def test_06_overnight_arrival_stay_nights_calculation(self):
        out_overnight = FlightOption(
            origin="ESB",
            destination="AMS",
            date="2026-12-01",
            airline="AJet",
            departure_time="23:30",
            arrival_time="02:15",
            duration="4 sa 45 dk",
            duration_minutes=285,
            stops=1,
            stops_text="1 aktarma",
            price=3000.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=1,
            arrival_date="2026-12-02",
        )
        in_flight = FlightOption(
            origin="AMS",
            destination="ESB",
            date="2026-12-06",
            airline="Pegasus",
            departure_time="14:00",
            arrival_time="19:00",
            duration="4 sa",
            duration_minutes=240,
            stops=0,
            stops_text="Aktarmasız",
            price=3200.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=0,
            arrival_date="2026-12-06",
        )

        # 2 Aralık varış -> 6 Aralık dönüş = 4 gece
        df_4nights = build_combinations_dataframe(
            outbound_flights=[out_overnight],
            inbound_flights=[in_flight],
            min_stay_days=4,
            max_stay_days=4,
            window_start="2026-12-01",
            window_end="2026-12-10",
            expected_currency="TRY",
        )
        self.assertEqual(len(df_4nights), 1)
        self.assertEqual(int(df_4nights.iloc[0]["stay_days"]), 4)

        # Varış tarihi doğrulanamayan (arrival_day_offset=None, arrival_date=None) uçuş sessizce aynı gün kabul edilmez
        out_unverified_arr = FlightOption(
            origin="ESB",
            destination="AMS",
            date="2026-12-02",
            airline="UnknownArrAir",
            departure_time="10:00",
            arrival_time=None,
            duration=None,
            duration_minutes=None,
            stops=0,
            stops_text="Aktarmasız",
            price=1000.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=None,
            arrival_date=None,
        )
        df_unverified = build_combinations_dataframe(
            outbound_flights=[out_unverified_arr],
            inbound_flights=[in_flight],
            min_stay_days=4,
            max_stay_days=4,
            window_start="2026-12-01",
            window_end="2026-12-10",
            expected_currency="TRY",
        )
        self.assertTrue(df_unverified.empty)

        # Eksik transfer maliyeti sessizce 0 kabul edilmez
        df_missing_transfer = build_combinations_dataframe(
            outbound_flights=[out_overnight],
            inbound_flights=[in_flight],
            min_stay_days=4,
            max_stay_days=4,
            window_start="2026-12-01",
            window_end="2026-12-10",
            expected_currency="TRY",
            include_transfer=True,
            transfer_costs={"DUS": 500.0},  # AMS eksik!
        )
        self.assertTrue(df_missing_transfer.empty)

    # 7. Dönüş varışı seyahat penceresi dışındaysa kombinasyon elenir
    def test_07_return_arrival_after_window_end_is_filtered_out(self):
        out_flight = FlightOption(
            origin="ESB",
            destination="AMS",
            date="2026-12-01",
            airline="Pegasus",
            departure_time="08:00",
            arrival_time="11:00",
            duration="4 sa",
            duration_minutes=240,
            stops=0,
            stops_text="Aktarmasız",
            price=3000.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=0,
            arrival_date="2026-12-01",
        )
        in_overnight = FlightOption(
            origin="AMS",
            destination="ESB",
            date="2026-12-05",
            airline="AJet",
            departure_time="22:30",
            arrival_time="04:15",
            duration="4 sa 45 dk",
            duration_minutes=285,
            stops=1,
            stops_text="1 aktarma",
            price=3100.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=1,
            arrival_date="2026-12-06",
        )
        df = build_combinations_dataframe(
            outbound_flights=[out_flight],
            inbound_flights=[in_overnight],
            min_stay_days=4,
            max_stay_days=6,
            window_start="2026-12-01",
            window_end="2026-12-05",
            expected_currency="TRY",
        )
        self.assertTrue(df.empty)

    # 8. Önbellek kapsamından fazla sonuç istenirse yeniden tarama gerekir
    def test_08_cache_scope_insufficient_when_higher_limit_requested(self):
        partial_res = RouteQueryResult(
            origin="ESB",
            destination="AMS",
            date="2026-11-15",
            currency="TRY",
            status="success",
            flights=[
                FlightOption(
                    origin="ESB",
                    destination="AMS",
                    date="2026-11-15",
                    airline="Pegasus",
                    departure_time="08:00",
                    arrival_time="11:00",
                    duration="4 sa",
                    duration_minutes=240,
                    stops=0,
                    stops_text="Aktarmasız",
                    price=3000.0,
                    currency="TRY",
                    booking_url="https://example.com",
                    arrival_day_offset=0,
                    arrival_date="2026-11-15",
                )
            ],
            requested_limit=8,
            collected_count=8,
            total_dom_cards=20,
            scope_complete=False,
            scope_note="Kısmi",
            fetched_at="2026-11-05T12:00:00",
        )
        entry = partial_res.to_cache_entry()
        self.assertTrue(is_cache_entry_usable(entry, 8))
        self.assertFalse(is_cache_entry_usable(entry, 20))

        legacy_entry = {"status": "success", "flights": []}
        self.assertFalse(is_cache_entry_usable(legacy_entry, 8))

    # 9. Dörtten fazla hedef ve geçersiz sayısal girdiler reddedilir (HTTP 400), 4 hedef kabul edilir
    def test_09_input_validation_rejects_more_than_4_targets_and_invalid_inputs(self):
        cleaned, err = server.validate_search_request(
            {
                "home_airport": "ESB",
                "target_airports": ["AMS", "DUS", "CGN", "BRU", "EIN"],
                "window_start": "2026-11-15",
                "window_end": "2026-11-25",
                "min_stay_days": 4,
                "max_stay_days": 6,
                "currency": "TRY",
            }
        )
        self.assertIsNone(cleaned)
        self.assertIn("En fazla 4", err)

        cleaned_4, err_4 = server.validate_search_request(
            {
                "home_airport": "ESB",
                "target_airports": ["AMS", "DUS", "CGN", "BRU"],
                "window_start": "2026-11-15",
                "window_end": "2026-11-25",
                "min_stay_days": 4,
                "max_stay_days": 6,
                "currency": "TRY",
            }
        )
        self.assertIsNone(err_4)
        self.assertEqual(len(cleaned_4["target_airports"]), 4)

        cleaned_ok, err_ok = server.validate_search_request(
            {
                "home_airport": "ESB",
                "target_airports": ["ESB", "AMS", "DUS"],
                "window_start": "2026-11-15",
                "window_end": "2026-11-25",
                "min_stay_days": 4,
                "max_stay_days": 6,
                "currency": "TRY",
            }
        )
        self.assertIsNone(err_ok)
        self.assertEqual(cleaned_ok["target_airports"], ["AMS", "DUS"])

        httpd, base_url = self._start_test_http_server()
        try:
            bad_req = Request(
                f"{base_url}/api/scrape",
                data=json.dumps(
                    {
                        "home_airport": "ESB",
                        "target_airports": ["AMS", "DUS", "CGN", "EIN", "BRU"],
                        "window_start": "2026-11-15",
                        "window_end": "2026-11-25",
                        "min_stay_days": 4,
                        "max_stay_days": 6,
                        "currency": "TRY",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as ctx:
                urlopen(bad_req)
            self.assertEqual(ctx.exception.code, 400)
            ctx.exception.close()
        finally:
            httpd.shutdown()
            httpd.server_close()

    # 10. Eşzamanlı tarama başlatma ve önbellek temizleme tutarlıdır + Seçili kombinasyon yeniden kontrol eşleştirmesi
    def test_10_concurrent_scrape_lock_cache_clear_epoch_and_recheck_matching(self):
        old_epoch = get_cache_epoch()
        clear_daily_cache()
        new_epoch = get_cache_epoch()
        self.assertGreater(new_epoch, old_epoch)

        stale_saved = update_single_cache_entry(
            _cache_key("ESB", "AMS", "2026-11-15", "TRY"),
            RouteQueryResult(
                origin="ESB",
                destination="AMS",
                date="2026-11-15",
                currency="TRY",
                status="success",
                flights=[],
                requested_limit=8,
                collected_count=0,
                total_dom_cards=0,
                scope_complete=True,
                scope_note="OK",
                fetched_at="2026-11-05T12:00:00",
            ).to_cache_entry(),
            expected_epoch=old_epoch,
        )
        self.assertFalse(stale_saved)
        reloaded_cache = load_daily_cache()
        self.assertEqual(len(reloaded_cache["entries"]), 0)

        with server.JOB_LOCK:
            server.SCRAPE_STATUS["running"] = True
            server.SCRAPE_STATUS["search_id"] = "job-active-test"

        httpd, base_url = self._start_test_http_server()
        try:
            req = Request(
                f"{base_url}/api/scrape",
                data=json.dumps(
                    {
                        "home_airport": "ESB",
                        "target_airports": ["AMS"],
                        "window_start": "2026-11-15",
                        "window_end": "2026-11-25",
                        "min_stay_days": 4,
                        "max_stay_days": 6,
                        "currency": "TRY",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as ctx:
                urlopen(req)
            self.assertEqual(ctx.exception.code, 409)
            ctx.exception.close()
        finally:
            with server.JOB_LOCK:
                server.SCRAPE_STATUS["running"] = False
            httpd.shutdown()
            httpd.server_close()

        fresh_flights = [
            FlightOption(
                origin="ESB",
                destination="AMS",
                date="2026-11-15",
                airline="KLM",
                departure_time="06:00",
                arrival_time="09:00",
                duration="4 sa",
                duration_minutes=240,
                stops=0,
                stops_text="Aktarmasız",
                price=9500.0,
                currency="TRY",
                booking_url="https://example.com",
                arrival_day_offset=0,
                arrival_date="2026-11-15",
            )
        ]
        matched = match_selected_flight(
            {
                "origin": "ESB",
                "destination": "AMS",
                "date": "2026-11-15",
                "airline": "Pegasus",
                "departure_time": "08:00",
                "arrival_time": "11:00",
                "stops": 0,
            },
            fresh_flights,
        )
        self.assertIsNone(matched)

    # 11. Olumsuz bagaj ifadesi yanlışlıkla ücretsiz bagaj sayılmaz ve çelişkili ifadeler None döner
    def test_11_baggage_negation_and_conflict(self):
        from scraper import extract_verified_baggage_flag

        self.assertIs(extract_verified_baggage_flag("Kabin bagajı dahil değil", None), False)
        self.assertIs(extract_verified_baggage_flag("Kabin bagajı dahil", None), True)
        self.assertIs(extract_verified_baggage_flag("Sadece koltuk altı", None), False)
        self.assertIsNone(extract_verified_baggage_flag("Pegasus ile direkt uçuş", None))
        self.assertIsNone(
            extract_verified_baggage_flag("Kabin bagajı dahil. Sadece koltuk altı çanta.", None)
        )

    # 12. Fiyat kontrolü farklı uçuş numarasını veya farklı aktarma/varış gününü kabul etmez, belirsiz kimliği ayırır
    def test_12_recheck_flight_number_and_identity_matching(self):
        cand_tk999 = FlightOption(
            origin="ESB",
            destination="AMS",
            date="2026-11-15",
            airline="Turkish Airlines",
            departure_time="08:00",
            arrival_time="11:00",
            duration="4 sa",
            duration_minutes=240,
            stops=0,
            stops_text="Aktarmasız",
            price=4200.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=0,
            arrival_date="2026-11-15",
            flight_number="TK999",
        )
        # Seçili uçuş TK123 iken aday TK999 -> asla eşleşmemeli!
        self.assertIsNone(
            match_selected_flight(
                {
                    "origin": "ESB",
                    "destination": "AMS",
                    "date": "2026-11-15",
                    "airline": "Turkish Airlines",
                    "departure_time": "08:00",
                    "arrival_time": "11:00",
                    "arrival_date": "2026-11-15",
                    "arrival_day_offset": 0,
                    "stops": 0,
                    "flight_number": "TK123",
                },
                [cand_tk999],
            )
        )

        # Aynı uçuş numarası (TK123) ama farklı saat veya farklı varış tarihi -> eşleşmemeli!
        cand_tk123_diff_time = FlightOption(
            origin="ESB",
            destination="AMS",
            date="2026-11-15",
            airline="Turkish Airlines",
            departure_time="09:30",
            arrival_time="12:30",
            duration="4 sa",
            duration_minutes=240,
            stops=0,
            stops_text="Aktarmasız",
            price=4200.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=0,
            arrival_date="2026-11-15",
            flight_number="TK123",
        )
        self.assertIsNone(
            match_selected_flight(
                {
                    "origin": "ESB",
                    "destination": "AMS",
                    "date": "2026-11-15",
                    "airline": "Turkish Airlines",
                    "departure_time": "08:00",
                    "arrival_time": "11:00",
                    "arrival_date": "2026-11-15",
                    "arrival_day_offset": 0,
                    "stops": 0,
                    "flight_number": "TK123",
                },
                [cand_tk123_diff_time],
            )
        )

        # Farklı aktarma havalimanı / segmenti olan uçuş aynı toplam saatlerde olsa da eşleşmemeli!
        cand_saw = FlightOption(
            origin="ESB",
            destination="AMS",
            date="2026-11-15",
            airline="AJet",
            departure_time="08:00",
            arrival_time="14:00",
            duration="7 sa",
            duration_minutes=420,
            stops=1,
            stops_text="1 aktarma",
            price=3500.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=0,
            arrival_date="2026-11-15",
            layover_airports=["SAW"],
            segments=["VF101", "VF102"],
        )
        self.assertIsNone(
            match_selected_flight(
                {
                    "origin": "ESB",
                    "destination": "AMS",
                    "date": "2026-11-15",
                    "airline": "AJet",
                    "departure_time": "08:00",
                    "arrival_time": "14:00",
                    "arrival_date": "2026-11-15",
                    "arrival_day_offset": 0,
                    "stops": 1,
                    "layover_airports": ["IST"],
                    "segments": ["VF201", "VF202"],
                },
                [cand_saw],
            )
        )

        # Aynı uçuş numarası (TK123) ve aynı saatler fakat farklı varış tarihi (+1 gün) -> eşleşmemeli!
        cand_tk123_diff_arr_day = FlightOption(
            origin="ESB",
            destination="AMS",
            date="2026-11-15",
            airline="Turkish Airlines",
            departure_time="23:00",
            arrival_time="02:00",
            duration="4 sa",
            duration_minutes=240,
            stops=0,
            stops_text="Aktarmasız",
            price=4200.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=1,
            arrival_date="2026-11-16",
            flight_number="TK123",
        )
        self.assertIsNone(
            match_selected_flight(
                {
                    "origin": "ESB",
                    "destination": "AMS",
                    "date": "2026-11-15",
                    "airline": "Turkish Airlines",
                    "departure_time": "23:00",
                    "arrival_time": "02:00",
                    "arrival_date": "2026-11-15",
                    "arrival_day_offset": 0,
                    "stops": 0,
                    "flight_number": "TK123",
                },
                [cand_tk123_diff_arr_day],
            )
        )

        # Kimliği doğrulanan aynı uçuşun fiyatı değişirse doğru eşleşir ve fiyat farkı doğru hesaplanır
        from scraper import evaluate_selected_flight_match

        cand_tk123_new_price = FlightOption(
            origin="ESB",
            destination="AMS",
            date="2026-11-15",
            airline="Turkish Airlines",
            departure_time="08:00",
            arrival_time="11:00",
            duration="4 sa",
            duration_minutes=240,
            stops=0,
            stops_text="Aktarmasız",
            price=4500.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=0,
            arrival_date="2026-11-15",
            flight_number="TK123",
        )
        status_ok, matched_ok, _ = evaluate_selected_flight_match(
            {
                "origin": "ESB",
                "destination": "AMS",
                "date": "2026-11-15",
                "airline": "Turkish Airlines",
                "departure_time": "08:00",
                "arrival_time": "11:00",
                "arrival_date": "2026-11-15",
                "arrival_day_offset": 0,
                "stops": 0,
                "price": 4000.0,
                "flight_number": "TK123",
            },
            [cand_tk123_new_price],
        )
        self.assertEqual(status_ok, "matched")
        self.assertIsNotNone(matched_ok)
        self.assertAlmostEqual(matched_ok.price - 4000.0, 500.0)

        # Kimliği yetersiz/belirsiz aday (ör. aktarmalı uçuşta ne uçuş no ne aktarma havalimanı/segmenti var)
        # kesin doğrulanmış sayılmaz -> identity_unverified döner!
        cand_ambig_conn = FlightOption(
            origin="ESB",
            destination="AMS",
            date="2026-11-15",
            airline="AJet",
            departure_time="08:00",
            arrival_time="14:00",
            duration="7 sa",
            duration_minutes=420,
            stops=1,
            stops_text="1 aktarma",
            price=3500.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=0,
            arrival_date="2026-11-15",
            flight_number=None,
            layover_airports=[],
            segments=[],
        )
        status_ambig, matched_ambig, _ = evaluate_selected_flight_match(
            {
                "origin": "ESB",
                "destination": "AMS",
                "date": "2026-11-15",
                "airline": "AJet",
                "departure_time": "08:00",
                "arrival_time": "14:00",
                "arrival_date": "2026-11-15",
                "arrival_day_offset": 0,
                "stops": 1,
            },
            [cand_ambig_conn],
        )
        self.assertEqual(status_ambig, "identity_unverified")
        self.assertIsNone(matched_ambig)

    # 13. Varış günü ayrıştırması "ertesi gün", "next day", "+2", açık aynı gün ve belirsizliği doğru işler
    def test_13_arrival_day_offset_parsing_and_uncertainty(self):
        from scraper import parse_time_and_day_offset, compute_arrival_date

        # Türkçe "ertesi gün" -> +1
        _, _, off_tr = parse_time_and_day_offset("23:00", "02:00", "23:00 02:00 ertesi gün", "")
        self.assertEqual(off_tr, 1)

        # İngilizce "next day" -> +1
        _, _, off_en = parse_time_and_day_offset("23:00", "02:00", "23:00 02:00 next day", "")
        self.assertEqual(off_en, 1)

        # +2 -> iki gün sonrası
        _, _, off_p2 = parse_time_and_day_offset("22:00", "05:00+2", "22:00 05:00+2", "")
        self.assertEqual(off_p2, 2)

        # Açıkça doğrulanan aynı gün -> 0
        _, _, off_same = parse_time_and_day_offset("08:00", "11:00", "Kalkış: 08:00, Varış: 11:00", "")
        self.assertEqual(off_same, 0)

        # Gün farkı doğrulanamıyorsa (ör. 23:00 -> 02:00 ama hiçbir gün işareti yok veya ayrıştırılamayan '+4' işareti) -> None
        _, _, off_ambig = parse_time_and_day_offset("23:00", "02:00", "23:00 02:00", "")
        self.assertIsNone(off_ambig)
        _, _, off_unparseable = parse_time_and_day_offset("08:00", "11:00+4", "08:00 11:00+4", "")
        self.assertIsNone(off_unparseable)

        # 1 Aralık kalkış, "ertesi gün" (+1) ile 2 Aralık varış, 6 Aralık dönüş -> 4 gece
        arr_date_dec2 = compute_arrival_date("2026-12-01", off_tr)
        self.assertEqual(arr_date_dec2, "2026-12-02")
        out_dec1 = FlightOption(
            origin="ESB",
            destination="AMS",
            date="2026-12-01",
            airline="Pegasus",
            departure_time="23:00",
            arrival_time="02:00",
            duration="4 sa",
            duration_minutes=240,
            stops=0,
            stops_text="Aktarmasız",
            price=2900.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=off_tr,
            arrival_date=arr_date_dec2,
        )
        in_dec6 = FlightOption(
            origin="AMS",
            destination="ESB",
            date="2026-12-06",
            airline="Pegasus",
            departure_time="15:00",
            arrival_time="19:00",
            duration="4 sa",
            duration_minutes=240,
            stops=0,
            stops_text="Aktarmasız",
            price=3100.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=0,
            arrival_date="2026-12-06",
        )
        df_stay = build_combinations_dataframe(
            outbound_flights=[out_dec1],
            inbound_flights=[in_dec6],
            min_stay_days=4,
            max_stay_days=4,
            window_start="2026-12-01",
            window_end="2026-12-08",
            expected_currency="TRY",
        )
        self.assertEqual(len(df_stay), 1)
        self.assertEqual(int(df_stay.iloc[0]["stay_days"]), 4)

    # 14. Filtre uygulanmadan önce sonuçlar kesilmez (9. sıradaki direkt/uygun uçuş korunur)
    def test_14_no_premature_slicing_before_filters(self):
        epoch = get_cache_epoch()
        # İlk 8 uçuş 1 aktarmalı, 05:00 kalkışlı ve 9 saat süreli; 9. uçuş direkt (stops=0), 10:00 kalkışlı ve 3.5 saat süreli
        out_flights_9 = [
            FlightOption(
                origin="ESB",
                destination="AMS",
                date="2026-11-15",
                airline=f"StopAir{i}",
                departure_time="05:00",
                arrival_time="14:00",
                duration="9 sa",
                duration_minutes=540,
                stops=1,
                stops_text="1 aktarma",
                price=2000.0 + i * 50,
                currency="TRY",
                booking_url="https://example.com",
                arrival_day_offset=0,
                arrival_date="2026-11-15",
            )
            for i in range(8)
        ] + [
            FlightOption(
                origin="ESB",
                destination="AMS",
                date="2026-11-15",
                airline="DirectAir9",
                departure_time="10:00",
                arrival_time="13:30",
                duration="3 sa 30 dk",
                duration_minutes=210,
                stops=0,
                stops_text="Aktarmasız",
                price=3200.0,
                currency="TRY",
                booking_url="https://example.com",
                arrival_day_offset=0,
                arrival_date="2026-11-15",
            )
        ]
        in_flight = FlightOption(
            origin="AMS",
            destination="ESB",
            date="2026-11-20",
            airline="DirectReturn",
            departure_time="14:00",
            arrival_time="18:00",
            duration="4 sa",
            duration_minutes=240,
            stops=0,
            stops_text="Aktarmasız",
            price=3000.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=0,
            arrival_date="2026-11-20",
        )
        update_single_cache_entry(
            _cache_key("ESB", "AMS", "2026-11-15", "TRY"),
            RouteQueryResult(
                origin="ESB",
                destination="AMS",
                date="2026-11-15",
                currency="TRY",
                status="success",
                flights=out_flights_9,
                requested_limit=20,
                collected_count=9,
                total_dom_cards=9,
                scope_complete=True,
                scope_note="OK",
                fetched_at="2026-11-05T12:00:00",
            ).to_cache_entry(),
            expected_epoch=epoch,
        )
        update_single_cache_entry(
            _cache_key("AMS", "ESB", "2026-11-20", "TRY"),
            RouteQueryResult(
                origin="AMS",
                destination="ESB",
                date="2026-11-20",
                currency="TRY",
                status="success",
                flights=[in_flight],
                requested_limit=20,
                collected_count=1,
                total_dom_cards=1,
                scope_complete=True,
                scope_note="OK",
                fetched_at="2026-11-05T12:00:00",
            ).to_cache_entry(),
            expected_epoch=epoch,
        )

        httpd, base_url = self._start_test_http_server()
        try:
            # 14a: max_results_per_route=8 olsa bile önbellekteki 9. uçuş direkt filtrede (max_stops=0) kaybolmamalı!
            req_direct = Request(
                f"{base_url}/api/combine",
                data=json.dumps(
                    {
                        "home_airport": "ESB",
                        "target_airports": ["AMS"],
                        "window_start": "2026-11-15",
                        "window_end": "2026-11-20",
                        "min_stay_days": 4,
                        "max_stay_days": 6,
                        "currency": "TRY",
                        "max_results_per_route": 8,
                        "max_stops": 0,
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req_direct) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(data["combinations_count"], 1)
                self.assertEqual(data["combinations"][0]["out_airline"], "DirectAir9")

            # 14b: Kalkış saati filtresi (09:00-12:00) ve maksimum uçuş süresi filtresi (5 saat) için de 9. uçuş bulunur
            req_time_dur = Request(
                f"{base_url}/api/combine",
                data=json.dumps(
                    {
                        "home_airport": "ESB",
                        "target_airports": ["AMS"],
                        "window_start": "2026-11-15",
                        "window_end": "2026-11-20",
                        "min_stay_days": 4,
                        "max_stay_days": 6,
                        "currency": "TRY",
                        "max_results_per_route": 8,
                        "out_dep_min_time": "09:00",
                        "out_dep_max_time": "12:00",
                        "max_flight_hours": 5.0,
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req_time_dur) as resp:
                data_td = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(data_td["combinations_count"], 1)
                self.assertEqual(data_td["combinations"][0]["out_airline"], "DirectAir9")

            # 14c: Görüntüleme limiti (display_limit=3) filtrelerden SONRA uygulanır
            req_disp = Request(
                f"{base_url}/api/combine",
                data=json.dumps(
                    {
                        "home_airport": "ESB",
                        "target_airports": ["AMS"],
                        "window_start": "2026-11-15",
                        "window_end": "2026-11-20",
                        "min_stay_days": 4,
                        "max_stay_days": 6,
                        "currency": "TRY",
                        "max_results_per_route": 8,
                        "display_limit": 3,
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req_disp) as resp:
                data_disp = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(data_disp["combinations_count"], 9)
                self.assertEqual(len(data_disp["combinations"]), 3)
        finally:
            httpd.shutdown()
            httpd.server_close()

    # 15. "Daha fazla uçuş" butonuna basıldıktan sonra asenkron yüklenen kartlar beklenir ve toplanır
    def test_15_more_flights_async_expansion_and_scope(self):
        import asyncio
        from playwright.async_api import async_playwright
        from scraper import _extract_query_result_from_page, _launch_browser

        fixture_url = (FIXTURES_DIR / "delayed_more_flights.html").resolve().as_uri()

        async def _run_fixture_checks():
            async with async_playwright() as p:
                browser = await _launch_browser(p, headless=True)
                context = await browser.new_context()
                page = await context.new_page()
                await page.goto(fixture_url)
                res_expanded = await _extract_query_result_from_page(
                    page=page,
                    origin="ESB",
                    destination="AMS",
                    date_str="2026-11-15",
                    currency="TRY",
                    booking_url=fixture_url,
                    requested_limit=10,
                )
                await page.goto(fixture_url + "?hang=1")
                res_hang = await _extract_query_result_from_page(
                    page=page,
                    origin="ESB",
                    destination="AMS",
                    date_str="2026-11-15",
                    currency="TRY",
                    booking_url=fixture_url,
                    requested_limit=10,
                )
                await context.close()
                await browser.close()
                return res_expanded, res_hang

        res_expanded, res_hang = asyncio.run(_run_fixture_checks())
        # Gecikmeli eklenen 3. uçuş (2950 TRY Pegasus) toplanmış ve en ucuz olarak 1. sıraya yerleşmiş olmalı
        self.assertEqual(len(res_expanded.flights), 3)
        self.assertEqual(res_expanded.flights[0].price, 2950.0)
        self.assertTrue(res_expanded.scope_complete)

        # Hang durumunda buton hâlâ sayfada olduğu ve yeni kartlar yüklenemediği için scope_complete=False olmalı
        self.assertEqual(len(res_hang.flights), 2)
        self.assertFalse(res_hang.scope_complete)

    # 16. 48 saat önbellek süresi, bugün hariç temizleme ve gidiş/dönüş ayrı havayolu filtreleri
    def test_16_cache_48h_ttl_keep_today_and_separate_airline_filters(self):
        from datetime import datetime, timedelta
        from scraper import save_daily_cache, clear_daily_cache, load_daily_cache

        now_dt = datetime.now()
        today_iso = now_dt.isoformat(timespec="seconds")
        yesterday_iso = (now_dt - timedelta(hours=24)).isoformat(timespec="seconds")
        expired_iso = (now_dt - timedelta(hours=60)).isoformat(timespec="seconds")

        today_day = now_dt.strftime("%Y-%m-%d")
        yesterday_day = (now_dt - timedelta(hours=24)).strftime("%Y-%m-%d")
        expired_day = (now_dt - timedelta(hours=60)).strftime("%Y-%m-%d")

        raw_cache = {
            "schema_version": 2,
            "date": today_day,
            "entries": {
                "ESB|AMS|2026-11-15|TRY": {
                    "schema_version": 2,
                    "origin": "ESB",
                    "destination": "AMS",
                    "date": "2026-11-15",
                    "currency": "TRY",
                    "status": "success",
                    "flights": [],
                    "requested_limit": 8,
                    "collected_count": 0,
                    "total_dom_cards": 0,
                    "scope_complete": True,
                    "fetched_at": today_iso,
                    "cached_date": today_day,
                },
                "ESB|DUS|2026-11-15|TRY": {
                    "schema_version": 2,
                    "origin": "ESB",
                    "destination": "DUS",
                    "date": "2026-11-15",
                    "currency": "TRY",
                    "status": "success",
                    "flights": [],
                    "requested_limit": 8,
                    "collected_count": 0,
                    "total_dom_cards": 0,
                    "scope_complete": True,
                    "fetched_at": yesterday_iso,
                    "cached_date": yesterday_day,
                },
                "ESB|CGN|2026-11-15|TRY": {
                    "schema_version": 2,
                    "origin": "ESB",
                    "destination": "CGN",
                    "date": "2026-11-15",
                    "currency": "TRY",
                    "status": "success",
                    "flights": [],
                    "requested_limit": 8,
                    "collected_count": 0,
                    "total_dom_cards": 0,
                    "scope_complete": True,
                    "fetched_at": expired_iso,
                    "cached_date": expired_day,
                },
            },
        }
        save_daily_cache(raw_cache)

        # 48 saatlik TTL: Bugünkü ve 24 saat önceki (dünkü) kayıt korunmalı, 60 saat önceki silinmeli
        loaded = load_daily_cache()
        self.assertIn("ESB|AMS|2026-11-15|TRY", loaded["entries"])
        self.assertIn("ESB|DUS|2026-11-15|TRY", loaded["entries"])
        self.assertNotIn("ESB|CGN|2026-11-15|TRY", loaded["entries"])

        # Bugün hariç sil (keep_today=True): Dünkü kayıt silinmeli, bugünkü kayıt kalmalı
        clear_res = clear_daily_cache(keep_today=True)
        self.assertEqual(clear_res["removed_count"], 1)
        self.assertEqual(clear_res["kept_count"], 1)
        after_clear = load_daily_cache()
        self.assertIn("ESB|AMS|2026-11-15|TRY", after_clear["entries"])
        self.assertNotIn("ESB|DUS|2026-11-15|TRY", after_clear["entries"])

        # Gidiş ve Dönüş ayrı havayolu filtreleri
        out_flights = [
            FlightOption(
                origin="ESB",
                destination="AMS",
                date="2026-11-15",
                airline="Pegasus",
                departure_time="08:00",
                arrival_time="11:00",
                duration="4 sa",
                duration_minutes=240,
                stops=0,
                stops_text="Aktarmasız",
                price=3000.0,
                currency="TRY",
                booking_url="https://example.com",
                arrival_day_offset=0,
                arrival_date="2026-11-15",
            ),
            FlightOption(
                origin="ESB",
                destination="AMS",
                date="2026-11-15",
                airline="SunExpress",
                departure_time="10:00",
                arrival_time="13:00",
                duration="4 sa",
                duration_minutes=240,
                stops=0,
                stops_text="Aktarmasız",
                price=3500.0,
                currency="TRY",
                booking_url="https://example.com",
                arrival_day_offset=0,
                arrival_date="2026-11-15",
            ),
        ]
        in_flights = [
            FlightOption(
                origin="AMS",
                destination="ESB",
                date="2026-11-20",
                airline="Pegasus",
                departure_time="14:00",
                arrival_time="19:00",
                duration="4 sa",
                duration_minutes=240,
                stops=0,
                stops_text="Aktarmasız",
                price=3200.0,
                currency="TRY",
                booking_url="https://example.com",
                arrival_day_offset=0,
                arrival_date="2026-11-20",
            ),
            FlightOption(
                origin="AMS",
                destination="ESB",
                date="2026-11-20",
                airline="Turkish Airlines",
                departure_time="16:00",
                arrival_time="21:00",
                duration="4 sa",
                duration_minutes=240,
                stops=0,
                stops_text="Aktarmasız",
                price=4000.0,
                currency="TRY",
                booking_url="https://example.com",
                arrival_day_offset=0,
                arrival_date="2026-11-20",
            ),
        ]

        # Gidişte Pegasus hariç, Dönüşte serbest -> Gidiş sadece SunExpress, Dönüş Pegasus veya THY olabilir
        df_sep = build_combinations_dataframe(
            outbound_flights=out_flights,
            inbound_flights=in_flights,
            min_stay_days=5,
            max_stay_days=5,
            out_excluded_airlines=["Pegasus"],
            in_excluded_airlines=["Turkish Airlines"],
            window_end="2026-11-25",
        )
        self.assertEqual(len(df_sep), 1)
        self.assertEqual(df_sep.iloc[0]["out_airline"], "SunExpress")
        self.assertEqual(df_sep.iloc[0]["in_airline"], "Pegasus")

        # Gidiş ve Dönüş ayrı havalimanı filtreleri (out_excluded_airports / in_excluded_airports)
        out_flights_ap = out_flights + [
            FlightOption(
                origin="ESB",
                destination="DUS",
                date="2026-11-15",
                airline="SunExpress",
                departure_time="09:00",
                arrival_time="12:00",
                duration="4 sa",
                duration_minutes=240,
                stops=0,
                stops_text="Aktarmasız",
                price=2800.0,
                currency="TRY",
                booking_url="https://example.com",
                arrival_day_offset=0,
                arrival_date="2026-11-15",
            )
        ]
        in_flights_ap = in_flights + [
            FlightOption(
                origin="DUS",
                destination="ESB",
                date="2026-11-20",
                airline="SunExpress",
                departure_time="15:00",
                arrival_time="20:00",
                duration="4 sa",
                duration_minutes=240,
                stops=0,
                stops_text="Aktarmasız",
                price=2900.0,
                currency="TRY",
                booking_url="https://example.com",
                arrival_day_offset=0,
                arrival_date="2026-11-20",
            )
        ]
        df_ap = build_combinations_dataframe(
            outbound_flights=out_flights_ap,
            inbound_flights=in_flights_ap,
            min_stay_days=5,
            max_stay_days=5,
            out_excluded_airports=["AMS"],
            in_excluded_airports=["DUS"],
            window_end="2026-11-25",
        )
        self.assertTrue((df_ap["out_destination"] == "DUS").all())
        self.assertTrue((df_ap["in_origin"] == "AMS").all())

    # 17. Tatile haftasonunu dahil et (include_weekend) filtresi doğru çalışır
    def test_17_include_weekend_filter(self):
        # 1. Uçuş: 2026-11-20 Cuma varış, 2026-11-23 Pazartesi dönüş (Cumartesi ve Pazar dahil)
        f_weekend_out = FlightOption(
            origin="ESB",
            destination="AMS",
            date="2026-11-20",
            airline="Pegasus",
            departure_time="08:00",
            arrival_time="11:00",
            duration="4 sa",
            duration_minutes=240,
            stops=0,
            stops_text="Aktarmasız",
            price=3000.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=0,
            arrival_date="2026-11-20",
        )
        f_weekend_in = FlightOption(
            origin="AMS",
            destination="ESB",
            date="2026-11-23",
            airline="Pegasus",
            departure_time="14:00",
            arrival_time="19:00",
            duration="4 sa",
            duration_minutes=240,
            stops=0,
            stops_text="Aktarmasız",
            price=3200.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=0,
            arrival_date="2026-11-23",
        )

        # 2. Uçuş: 2026-11-16 Pazartesi varış, 2026-11-19 Perşembe dönüş (Yalnızca hafta içi: Pzt, Sal, Çar, Per)
        f_weekday_out = FlightOption(
            origin="ESB",
            destination="AMS",
            date="2026-11-16",
            airline="SunExpress",
            departure_time="09:00",
            arrival_time="12:00",
            duration="4 sa",
            duration_minutes=240,
            stops=0,
            stops_text="Aktarmasız",
            price=2500.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=0,
            arrival_date="2026-11-16",
        )
        f_weekday_in = FlightOption(
            origin="AMS",
            destination="ESB",
            date="2026-11-19",
            airline="SunExpress",
            departure_time="15:00",
            arrival_time="20:00",
            duration="4 sa",
            duration_minutes=240,
            stops=0,
            stops_text="Aktarmasız",
            price=2700.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=0,
            arrival_date="2026-11-19",
        )

        # 3. Uçuş: 2026-11-22 Pazar varış, 2026-11-25 Çarşamba dönüş (Sadece Pazar dahil, Cumartesi yok)
        f_sunday_out = FlightOption(
            origin="ESB",
            destination="AMS",
            date="2026-11-22",
            airline="AJet",
            departure_time="10:00",
            arrival_time="13:00",
            duration="4 sa",
            duration_minutes=240,
            stops=0,
            stops_text="Aktarmasız",
            price=2600.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=0,
            arrival_date="2026-11-22",
        )
        f_sunday_in = FlightOption(
            origin="AMS",
            destination="ESB",
            date="2026-11-25",
            airline="AJet",
            departure_time="16:00",
            arrival_time="21:00",
            duration="4 sa",
            duration_minutes=240,
            stops=0,
            stops_text="Aktarmasız",
            price=2800.0,
            currency="TRY",
            booking_url="https://example.com",
            arrival_day_offset=0,
            arrival_date="2026-11-25",
        )

        # Filtre kapalıyken: Üç kombinasyon da döner
        df_all = build_combinations_dataframe(
            outbound_flights=[f_weekend_out, f_weekday_out, f_sunday_out],
            inbound_flights=[f_weekend_in, f_weekday_in, f_sunday_in],
            min_stay_days=3,
            max_stay_days=3,
            include_weekend=False,
            window_end="2026-11-30",
        )
        self.assertEqual(len(df_all), 3)

        # Alanların doğru etiketlendiğini doğrula
        row_weekend = df_all[df_all["out_date"] == "2026-11-20"].iloc[0]
        self.assertTrue(row_weekend["includes_weekend"])
        self.assertEqual(row_weekend["weekend_label"], "Cmt & Paz")
        self.assertEqual(row_weekend["weekend_days_count"], 2)

        row_weekday = df_all[df_all["out_date"] == "2026-11-16"].iloc[0]
        self.assertFalse(row_weekday["includes_weekend"])
        self.assertEqual(row_weekday["weekend_label"], "Hafta İçi")
        self.assertEqual(row_weekday["weekend_days_count"], 0)

        row_sunday = df_all[df_all["out_date"] == "2026-11-22"].iloc[0]
        self.assertFalse(row_sunday["includes_weekend"])  # Sadece Pazar dahil olması tam haftasonu sayılmaz!
        self.assertEqual(row_sunday["weekend_label"], "Yalnızca Pazar")
        self.assertEqual(row_sunday["weekend_days_count"], 1)

        # include_weekend=True aktifken: SADECE her iki günü de (Cumartesi ve Pazar) içeren kombinasyon döner!
        df_weekend_only = build_combinations_dataframe(
            outbound_flights=[f_weekend_out, f_weekday_out, f_sunday_out],
            inbound_flights=[f_weekend_in, f_weekday_in, f_sunday_in],
            min_stay_days=3,
            max_stay_days=3,
            include_weekend=True,
            window_end="2026-11-30",
        )
        self.assertEqual(len(df_weekend_only), 1)
        self.assertEqual(df_weekend_only.iloc[0]["out_date"], "2026-11-20")
        self.assertEqual(df_weekend_only.iloc[0]["in_date"], "2026-11-23")
        self.assertEqual(df_weekend_only.iloc[0]["weekend_label"], "Cmt & Paz")

    def test_18_independent_leg_baggage_filters_and_estimation(self):
        """
        Gidiş ve dönüş için bağımsız bagaj filtreleri, referans ücretler ve maliyet sıralaması:
        1. Gidişte AJet Basic: 8 kg kabin (€15) + 20 kg uçak altı (€25) = €40.
        2. Dönüşte SunExpress SunValue varsayımı: 8 kg kabin dahil (€0) ve 20 kg alt bagaj dahil (€0).
        3. Kombinasyon toplam bagaj maliyeti: €40 + €0 = €40. SunEco+ (30 kg) seçilirse +€15 eklenir.
        4. Tekliften doğrulanan hak (verified_offer) referansın önüne geçer.
        5. Pegasus kampanya rakamları otomatik tahmin olarak uygulanmaz (unknown kalır).
        6. THY EcoFly kabin dahil, uçak altı bilinmiyor olarak kalır (sabit 15kg/0kg atanmaz).
        7. Kur çarpanı yoksa EUR referansı TRY toplamına eklenmez (unknown kalır).
        8. Bagaj maliyeti belirsiz olanlar, bilinen seçeneklerin önüne ücretsizmiş gibi geçemez.
        9. 'Bu yönde bagaj dahil olsun' filtresi doğrulanmamış bagajları eler.
        """
        from baggage_rules import evaluate_leg_baggage

        # 1) AJet Almanya Basic: 8 kg kabin (€15) + 20 kg uçak altı (€25)
        ajet_out = {
            "origin": "ESB",
            "destination": "DUS",
            "airline": "AJet",
            "stops": 0,
            "cabin_baggage_included": None,
            "checked_baggage_included": None,
            "checked_baggage_kg": None,
            "currency": "EUR",
        }
        res_ajet = evaluate_leg_baggage(
            flight_data=ajet_out,
            cabin_req="1x8kg",
            checked_req=20,
            currency="EUR",
        )
        self.assertEqual(res_ajet.cabin_cost, 15.0)
        self.assertEqual(res_ajet.checked_cost, 25.0)
        self.assertEqual(res_ajet.leg_baggage_cost, 40.0)
        self.assertTrue(res_ajet.leg_cost_known)
        self.assertIn("AJet Basic varsayımı: kabin ücretli, bilette kontrol edin", res_ajet.cabin_desc)
        self.assertIn("4 kg", res_ajet.personal_bag_info)

        # 2) SunExpress SunValue varsayımı: 8 kg kabin dahil (€0), 20 kg'a kadar kayıtlı bagaj dahil (€0).
        # 20 kg üzeri: 30 kg için SunEco+ (+€15), 35 kg için SunComfort (+€45).
        sun_in = {
            "origin": "DUS",
            "destination": "ESB",
            "airline": "SunExpress",
            "stops": 0,
            "cabin_baggage_included": None,
            "checked_baggage_included": None,
            "checked_baggage_kg": None,
            "currency": "EUR",
        }
        res_sun = evaluate_leg_baggage(
            flight_data=sun_in,
            cabin_req="1x8kg",
            checked_req=20,
            currency="EUR",
        )
        self.assertEqual(res_sun.cabin_cost, 0.0)
        self.assertEqual(res_sun.checked_cost, 0.0)
        self.assertEqual(res_sun.leg_baggage_cost, 0.0)
        self.assertTrue(res_sun.leg_cost_known)
        self.assertIn("SunValue varsayımı", res_sun.checked_desc)

        # 30 kg için teklifte üst paket farkı yoksa sabit €15 üretilmez (belirsiz kalır); manuel ücret varsa uygulanır
        res_sun_30_unknown = evaluate_leg_baggage(
            flight_data=sun_in,
            cabin_req="none",
            checked_req=30,
            currency="EUR",
        )
        self.assertIsNone(res_sun_30_unknown.checked_cost)
        self.assertFalse(res_sun_30_unknown.leg_cost_known)

        res_sun_30_manual = evaluate_leg_baggage(
            flight_data=sun_in,
            cabin_req="none",
            checked_req=30,
            currency="EUR",
            manual_checked_fee=15.0,
        )
        self.assertEqual(res_sun_30_manual.checked_cost, 15.0)
        self.assertEqual(res_sun_30_manual.leg_baggage_cost, 15.0)

        # 3) Kombinasyon oluşturma ve toplam bagaj hesabı
        f_ajet_out = FlightOption(
            origin="ESB",
            destination="DUS",
            date="2026-11-15",
            airline="AJet",
            departure_time="10:00",
            arrival_time="13:00",
            duration="3 sa",
            duration_minutes=180,
            stops=0,
            stops_text="Aktarmasız",
            price=100.0,
            currency="EUR",
            booking_url="https://example.com/out",
            arrival_day_offset=0,
            arrival_date="2026-11-15",
        )
        f_sun_in = FlightOption(
            origin="DUS",
            destination="ESB",
            date="2026-11-20",
            airline="SunExpress",
            departure_time="14:00",
            arrival_time="19:00",
            duration="4 sa",
            duration_minutes=240,
            stops=0,
            stops_text="Aktarmasız",
            price=80.0,
            currency="EUR",
            booking_url="https://example.com/in",
            arrival_day_offset=0,
            arrival_date="2026-11-20",
        )

        df_combo = build_combinations_dataframe(
            outbound_flights=[f_ajet_out],
            inbound_flights=[f_sun_in],
            min_stay_days=5,
            max_stay_days=5,
            expected_currency="EUR",
            out_cabin_req="1x8kg",
            out_checked_req=20,
            in_cabin_req="1x8kg",
            in_checked_req=20,
        )
        self.assertEqual(len(df_combo), 1)
        row = df_combo.iloc[0]
        self.assertEqual(row["out_baggage_cost"], 40.0)
        self.assertEqual(row["in_baggage_cost"], 0.0)
        self.assertEqual(row["baggage_total_price"], 40.0)
        self.assertTrue(row["baggage_cost_known"])
        self.assertEqual(row["grand_total_price"], 220.0)

        # 4) Doğrulanmış teklif önceliği: teklifte kabin onaylı ise €0
        f_verified_out = FlightOption(
            origin="ESB",
            destination="DUS",
            date="2026-11-15",
            airline="AJet",
            departure_time="10:00",
            arrival_time="13:00",
            duration="3 sa",
            duration_minutes=180,
            stops=0,
            stops_text="Aktarmasız",
            price=110.0,
            currency="EUR",
            booking_url="https://example.com/out2",
            arrival_day_offset=0,
            arrival_date="2026-11-15",
            cabin_baggage_included=True,
            checked_baggage_included=True,
            checked_baggage_kg=20,
        )
        df_verified = build_combinations_dataframe(
            outbound_flights=[f_verified_out],
            inbound_flights=[f_sun_in],
            min_stay_days=5,
            max_stay_days=5,
            expected_currency="EUR",
            out_cabin_req="1x8kg",
            out_checked_req=20,
            in_cabin_req="none",
            in_checked_req=10,
        )
        self.assertEqual(df_verified.iloc[0]["out_baggage_cost"], 0.0)
        self.assertEqual(df_verified.iloc[0]["baggage_total_price"], 0.0)

        # 5) Pegasus: Kampanya rakamları otomatik tahmin olarak uygulanmaz (unknown kalır)
        pegasus_out = {
            "origin": "SAW",
            "destination": "DUS",
            "airline": "Pegasus",
            "stops": 0,
            "currency": "EUR",
        }
        res_pgs = evaluate_leg_baggage(pegasus_out, cabin_req="1x8kg", checked_req=20, currency="EUR")
        self.assertIsNone(res_pgs.cabin_cost)
        self.assertIsNone(res_pgs.checked_cost)
        self.assertFalse(res_pgs.leg_cost_known)
        self.assertEqual(res_pgs.cabin_status, "unknown")

        # 6) THY: EcoFly kabin dahil, uçak altı belirsiz (unknown)
        thy_out = {
            "origin": "IST",
            "destination": "AMS",
            "airline": "Turkish Airlines",
            "stops": 0,
            "currency": "EUR",
        }
        res_thy = evaluate_leg_baggage(thy_out, cabin_req="1x8kg", checked_req=20, currency="EUR")
        self.assertEqual(res_thy.cabin_cost, 0.0)
        self.assertEqual(res_thy.cabin_status, "included")
        self.assertIsNone(res_thy.checked_cost)
        self.assertFalse(res_thy.leg_cost_known)

        # 7) Kur çarpanı yoksa EUR referansı TRY toplamına eklenmez (unknown kalır)
        res_no_fx = evaluate_leg_baggage(ajet_out, cabin_req="1x8kg", checked_req=20, currency="TRY", manual_eur_rate=None)
        self.assertIsNone(res_no_fx.cabin_cost)
        self.assertFalse(res_no_fx.leg_cost_known)

        # Kur çarpanı girilirse (1 EUR = 38 TRY) dönüştürülür
        res_with_fx = evaluate_leg_baggage(ajet_out, cabin_req="1x8kg", checked_req=20, currency="TRY", manual_eur_rate=38.0)
        self.assertEqual(res_with_fx.cabin_cost, 570.0)    # 15 * 38
        self.assertEqual(res_with_fx.checked_cost, 950.0)  # 25 * 38
        self.assertEqual(res_with_fx.leg_baggage_cost, 1520.0)
        self.assertTrue(res_with_fx.leg_cost_known)

        # 8) Sıralama: Bagaj maliyeti belirsiz olanlar bilinenlerin önüne geçemez!
        f_known_higher_base = FlightOption(
            origin="ESB", destination="DUS", date="2026-11-15", airline="Turkish Airlines",
            departure_time="09:00", arrival_time="12:00", duration="3 sa", duration_minutes=180,
            stops=0, stops_text="Aktarmasız", price=120.0, currency="EUR",
            booking_url="https://example.com/thy", arrival_day_offset=0, arrival_date="2026-11-15",
            cabin_baggage_included=True, checked_baggage_included=True, checked_baggage_kg=20
        )
        f_known_lower_base = FlightOption(
            origin="ESB", destination="DUS", date="2026-11-15", airline="AJet",
            departure_time="10:00", arrival_time="13:00", duration="3 sa", duration_minutes=180,
            stops=0, stops_text="Aktarmasız", price=90.0, currency="EUR",
            booking_url="https://example.com/ajet", arrival_day_offset=0, arrival_date="2026-11-15",
        )
        f_unknown_bag = FlightOption(
            origin="ESB", destination="DUS", date="2026-11-15", airline="Pegasus",
            departure_time="08:00", arrival_time="11:00", duration="3 sa", duration_minutes=180,
            stops=0, stops_text="Aktarmasız", price=50.0, currency="EUR",
            booking_url="https://example.com/pgs", arrival_day_offset=0, arrival_date="2026-11-15",
        )
        f_return_free = FlightOption(
            origin="DUS", destination="ESB", date="2026-11-20", airline="SunExpress",
            departure_time="14:00", arrival_time="19:00", duration="4 sa", duration_minutes=240,
            stops=0, stops_text="Aktarmasız", price=50.0, currency="EUR",
            booking_url="https://example.com/in", arrival_day_offset=0, arrival_date="2026-11-20",
            cabin_baggage_included=True, checked_baggage_included=True, checked_baggage_kg=20
        )

        df_sorted = build_combinations_dataframe(
            outbound_flights=[f_unknown_bag, f_known_lower_base, f_known_higher_base],
            inbound_flights=[f_return_free],
            min_stay_days=5,
            max_stay_days=5,
            expected_currency="EUR",
            out_cabin_req="1x8kg",
            out_checked_req=20,
            in_cabin_req="none",
            in_checked_req=0,
        )
        # THY: base €120 + return €50 + bag €0 = €170 (known)
        # AJet: base €90 + return €50 + bag €40 = €180 (known)
        # Pegasus: base €50 + return €50 + bag unknown = €100 (unknown)
        # Sıralama: THY (€170, known) önce gelmeli, ardından AJet (€180, known), en son Pegasus (unknown)!
        self.assertEqual(len(df_sorted), 3)
        self.assertEqual(df_sorted.iloc[0]["out_airline"], "Turkish Airlines")
        self.assertEqual(df_sorted.iloc[0]["grand_total_price"], 170.0)
        self.assertTrue(df_sorted.iloc[0]["baggage_cost_known"])

        self.assertEqual(df_sorted.iloc[1]["out_airline"], "AJet")
        self.assertEqual(df_sorted.iloc[1]["grand_total_price"], 180.0)
        self.assertTrue(df_sorted.iloc[1]["baggage_cost_known"])

        self.assertEqual(df_sorted.iloc[2]["out_airline"], "Pegasus")
        self.assertFalse(df_sorted.iloc[2]["baggage_cost_known"])

        # 9) 'Bu yönde bagaj dahil olsun' filtresi doğrulanmamış uçuşları eler
        df_filtered_inc = build_combinations_dataframe(
            outbound_flights=[f_unknown_bag, f_known_lower_base, f_known_higher_base],
            inbound_flights=[f_return_free],
            min_stay_days=5,
            max_stay_days=5,
            expected_currency="EUR",
            out_cabin_req="1x8kg",
            out_checked_req=20,
            out_baggage_included_only=True,
            in_cabin_req="none",
            in_checked_req=0,
        )
        # Yalnızca THY (teklifte onaylı 8kg kabin ve 20kg bagajı olan) geçer
        self.assertEqual(len(df_filtered_inc), 1)
        self.assertEqual(df_filtered_inc.iloc[0]["out_airline"], "Turkish Airlines")

    # 19. Bagaj hakkı öncelikleri, SunValue/SunLight kuralları, AJet tarifesi ve eksik toplam güvenliği
    def test_19_baggage_strict_rules_and_subtotals(self):
        from baggage_rules import evaluate_leg_baggage
        from combiner import build_combinations_dataframe

        # 1. SunValue ve 8 kg kabin + 20 kg uçak altı -> ek ücret 0
        sun_val = {
            "origin": "ESB", "destination": "AMS", "airline": "SunExpress",
            "package_name": "SunValue", "stops": 0, "currency": "TRY",
        }
        res_val = evaluate_leg_baggage(sun_val, cabin_req="1x8kg", checked_req=20, currency="TRY")
        self.assertEqual(res_val.cabin_cost, 0.0)
        self.assertEqual(res_val.checked_cost, 0.0)
        self.assertEqual(res_val.leg_baggage_cost, 0.0)
        self.assertTrue(res_val.leg_cost_known)
        self.assertEqual(res_val.cabin_source, "package_rule")
        self.assertEqual(res_val.checked_source, "package_rule")
        self.assertTrue(res_val.passes_filter)

        # 2. SunExpress paketi bilinmiyor -> SunValue tahmini etiketlenir, doğrulanmış hak filtresinden geçmez
        sun_unknown_pkg = {
            "origin": "ESB", "destination": "AMS", "airline": "SunExpress",
            "package_name": None, "stops": 0, "currency": "TRY",
        }
        res_un_pkg = evaluate_leg_baggage(sun_unknown_pkg, cabin_req="1x8kg", checked_req=20, currency="TRY", baggage_included_only=True)
        self.assertEqual(res_un_pkg.cabin_cost, 0.0)
        self.assertEqual(res_un_pkg.checked_cost, 0.0)
        self.assertEqual(res_un_pkg.cabin_source, "sunvalue_assumption")
        self.assertEqual(res_un_pkg.checked_source, "sunvalue_assumption")
        self.assertIn("SunValue varsayımı", res_un_pkg.leg_summary_text)
        self.assertFalse(res_un_pkg.passes_filter)

        # 3. SunExpress'te açık 'kabin dahil değil' ve '0 kg' -> SunValue bunları ezmez
        sun_neg = {
            "origin": "ESB", "destination": "AMS", "airline": "SunExpress",
            "stops": 0, "currency": "TRY",
            "cabin_baggage_included": False,
            "checked_baggage_included": False,
            "checked_baggage_kg": 0,
        }
        res_neg = evaluate_leg_baggage(sun_neg, cabin_req="1x8kg", checked_req=20, currency="TRY")
        self.assertFalse(res_neg.cabin_cost == 0.0)
        self.assertFalse(res_neg.checked_cost == 0.0)
        self.assertFalse(res_neg.passes_filter)

        # 4. Uygun SunLight rotasında 8 kg kabin + 20 kg uçak altı -> referans başlangıç tahmini €54,98
        sunlight_flight = {
            "origin": "ESB", "destination": "DUS", "airline": "SunExpress",
            "package_name": "SunLight", "stops": 0, "currency": "EUR",
        }
        res_sl = evaluate_leg_baggage(sunlight_flight, cabin_req="1x8kg", checked_req=20, currency="EUR")
        self.assertEqual(res_sl.cabin_cost, 19.99)
        self.assertEqual(res_sl.checked_cost, 34.99)
        self.assertEqual(res_sl.leg_baggage_cost, 54.98)
        self.assertTrue(res_sl.leg_cost_known)
        self.assertIn("SunLight başlangıç fiyatı", res_sl.cabin_desc)
        self.assertIn("SunLight başlangıç fiyatı", res_sl.checked_desc)

        # 5. Uçak altı dahil işareti var ama kilo yok -> 20 kg doğrulanmış sayılmaz
        bag_no_kg = {
            "origin": "ESB", "destination": "DUS", "airline": "AJet",
            "stops": 0, "currency": "EUR",
            "checked_baggage_included": True, "checked_baggage_kg": None,
        }
        res_no_kg = evaluate_leg_baggage(bag_no_kg, cabin_req="none", checked_req=20, currency="EUR", baggage_included_only=True)
        self.assertFalse(res_no_kg.checked_passes_filter)
        self.assertIsNone(res_no_kg.checked_cost)
        self.assertIn("Uçak altı dahil · kilo doğrulanmadı", res_no_kg.checked_desc)

        # 6. AJet 20 kg dahil / 25 kg ihtiyaç -> ilk bagaj tarifesinden €12 uygulanmaz
        ajet_20kg = {
            "origin": "ESB", "destination": "DUS", "airline": "AJet",
            "stops": 0, "currency": "EUR",
            "checked_baggage_included": True, "checked_baggage_kg": 20,
        }
        res_ajet_25 = evaluate_leg_baggage(ajet_20kg, cabin_req="none", checked_req=25, currency="EUR")
        self.assertIsNone(res_ajet_25.checked_cost)
        self.assertFalse(res_ajet_25.leg_cost_known)

        # 7. AJet 0 kg dahil / 30 kg ihtiyaç -> €25'lik 20 kg paketi yeterli sayılmaz
        ajet_0kg = {
            "origin": "ESB", "destination": "DUS", "airline": "AJet",
            "stops": 0, "currency": "EUR",
            "checked_baggage_included": False, "checked_baggage_kg": 0,
        }
        res_ajet_30 = evaluate_leg_baggage(ajet_0kg, cabin_req="none", checked_req=30, currency="EUR")
        self.assertIsNone(res_ajet_30.checked_cost)
        self.assertFalse(res_ajet_30.leg_cost_known)

        # 8. Bilet €200, bilinen bagaj €40, diğer bagaj belirsiz -> ara toplam €240, tam toplam null
        f_out = FlightOption(
            origin="ESB", destination="DUS", date="2026-11-15", airline="AJet",
            departure_time="10:00", arrival_time="13:00", duration="3 sa", duration_minutes=180,
            stops=0, stops_text="Aktarmasız", price=100.0, currency="EUR",
            booking_url="https://example.com/out", arrival_day_offset=0, arrival_date="2026-11-15",
        )
        f_in_unknown = FlightOption(
            origin="DUS", destination="ESB", date="2026-11-20", airline="Pegasus",
            departure_time="14:00", arrival_time="19:00", duration="4 sa", duration_minutes=240,
            stops=0, stops_text="Aktarmasız", price=100.0, currency="EUR",
            booking_url="https://example.com/in", arrival_day_offset=0, arrival_date="2026-11-20",
        )
        df_part = build_combinations_dataframe(
            outbound_flights=[f_out],
            inbound_flights=[f_in_unknown],
            min_stay_days=5, max_stay_days=5, expected_currency="EUR",
            out_cabin_req="1x8kg", out_checked_req=20,  # AJet: 15 + 25 = 40 (known)
            in_cabin_req="1x8kg", in_checked_req=20,    # Pegasus: unknown
        )
        self.assertEqual(len(df_part), 1)
        r_part = df_part.iloc[0]
        self.assertEqual(r_part["flight_total_price"], 200.0)
        self.assertEqual(r_part["known_subtotal_price"], 240.0)
        self.assertTrue(pd.isna(r_part["grand_total_price"]))
        self.assertFalse(r_part["baggage_cost_known"])
        self.assertEqual(r_part["missing_baggage_note"], "dönüş bagajı hariç")

        # JSON çıktısında grand_total_price'ın null olduğunu doğrula
        from combiner import sanitize_records_for_json
        records = sanitize_records_for_json(df_part.to_dict(orient="records"))
        self.assertIsNone(records[0]["grand_total_price"])

        # 9. Türkiye dışı rota veya aktarma bilgisi belirsiz -> kapsamı doğrulanmamış referans ücret otomatik uygulanmaz
        ajet_non_tr = {
            "origin": "DUS", "destination": "AMS", "airline": "AJet",
            "stops": 0, "currency": "EUR",
        }
        res_non_tr = evaluate_leg_baggage(ajet_non_tr, cabin_req="1x8kg", checked_req=20, currency="EUR")
        self.assertIsNone(res_non_tr.cabin_cost)
        self.assertIsNone(res_non_tr.checked_cost)

        ajet_unknown_stops = {
            "origin": "ESB", "destination": "DUS", "airline": "AJet",
            "stops": None, "currency": "EUR",
        }
        res_un_stops = evaluate_leg_baggage(ajet_unknown_stops, cabin_req="1x8kg", checked_req=20, currency="EUR")
        self.assertIsNone(res_un_stops.cabin_cost)
        self.assertIsNone(res_un_stops.checked_cost)


if __name__ == "__main__":
    unittest.main()
