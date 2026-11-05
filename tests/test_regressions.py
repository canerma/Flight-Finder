import json
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import scraper
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

    # 9. Üçten fazla hedef ve geçersiz sayısal girdiler reddedilir (HTTP 400)
    def test_09_input_validation_rejects_more_than_3_targets_and_invalid_inputs(self):
        cleaned, err = server.validate_search_request(
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
        self.assertIsNone(cleaned)
        self.assertIn("En fazla 3", err)

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
                        "target_airports": ["AMS", "DUS", "CGN", "EIN"],
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


if __name__ == "__main__":
    unittest.main()
