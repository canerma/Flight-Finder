# GEMINI.md — Smart Flight Finder (Kişisel Akıllı Uçuş Bulucu)

## 1. Proje Amacı ve Özeti
**Smart Flight Finder**, kişisel kullanım için geliştirilmiş, yakın alternatif havalimanlarını (en fazla **3 hedef havalimanı**) ve esnek tarih aralıklarını tarayarak en ucuz/mantıklı **simetrik veya asimetrik gidiş-dönüş** uçuş kombinasyonlarını (ör. Gidiş: `ESB -> CGN`, Dönüş: `AMS -> ESB`) bulan yerel bir web uygulamasıdır.

---

## 2. Temel Kısıtlar ve Mimari Prensipler
1. **0 TL API Maliyeti:** Ücretli uçuş API'leri (Amadeus, Kiwi, Skyscanner vb.) kullanılmaz. Tüm uçuş verileri arka planda **Playwright** ile doğrudan **Google Flights** üzerinden çekilir.
2. **Ayrık Tek Yön (Decoupled One-Way) Kombinasyon ve Gece Hesabı:**
   - Gidiş ve dönüş rotaları **tek yön** olarak bağımsız taranır (`Kalkış -> Hedef Havalimanları` ve `Hedef Havalimanları -> Kalkış`, karşı tarafta en fazla 3 benzersiz hedef havalimanı).
   - Kullanıcı tek bir **Seyahat Tarih Aralığı** (`En Erken Gidiş` – `Eve En Geç Varış`) ve **Hedefte Kalış Süresi (Gece)** (`min_stay` – `max_stay`) seçer.
   - **Gece Sayısı Hesabı:** Hedefte kalış gecesi (`stay_days`), gidiş uçuşunun **hedefe varış yerel tarihi** (`out_arrival_date`) ile dönüş uçuşunun **kalkış yerel tarihi** (`in_date`) arasındaki gün farkı olarak hesaplanır (`+1` / `+2` gün farkları dikkate alınır; ör. 1 Aralık kalkış, 2 Aralık hedefe varış, 6 Aralık dönüş = **4 gece**).
   - **Dönüş Varış Kontrolü:** Dönüş uçuşunun kalkış havalimanına varış yerel tarihi (`in_arrival_date`), seyahat penceresinin son gününü (`window_end`) aşamaz.
   - Doğrulanamayan varış tarihleri sessizce aynı gün kabul edilmez; kombinasyondan elenir.
3. **Sorgu Durumları ve Günlük Kalıcı Önbellek (`flight_cache.json`, Şema v2):**
   - Her `(origin, destination, date, currency)` sorgusu açık bir durumla kaydedilir: `success`, `no_results`, `timeout`, `blocked`, `parse_error`.
   - `no_results` durumu yalnızca sayfada gerçek "uçuş bulunamadı" mesajı doğrulandığında atanır. Hatalı sorgular (`timeout`, `blocked`, `parse_error`) asla geçerli boş sonuç gibi önbellekten sunulmaz.
   - Önbellek kayıtları `schema_version`, `requested_limit`, `collected_count`, `total_dom_cards`, `scope_complete` ve `fetched_at` metaverilerini içerir. Daha yüksek `max_results_per_route` istendiğinde düşük kapsamlı eski önbellek yeterli kabul edilmez ve yeniden taranır.
   - Önbellek yazımı geçici dosya + `os.replace` ile atomik yapılır ve `_CACHE_EPOCH` sayacı sayesinde tarama sürerken yapılan önbellek temizliği eski verilerin geri yazılmasını engeller.
4. **Eksik Bilginin Doğru Bilgi Gibi Kullanılmaması (Nullable Modelleme):**
   - Okunamayan `stops`, `duration_minutes`, `departure_time`, `arrival_time`, `arrival_day_offset` ve `cabin_baggage_included` alanları `None` (`null`) olarak modellenir ve arayüzde `"Doğrulanmadı"` olarak gösterilir.
   - Aktarma filtresi (`max_stops`), maksimum süre filtresi (`max_flight_hours`), kalkış saati filtresi ve `"Ücretsiz kabin bagajı"` filtresi aktifken ilgili alanı doğrulanmamış (`None`) uçuşlar filtreden geçirilmez.
   - Süresi bilinmeyen uçuşlar `0 dk` gösterilmez ve en kısa uçuş gibi en üste sıralanmaz (`na_position="last"`).
5. **Bagaj ve Kara Ulaşımı (Transfer) Tahmini:**
   - Havayolu adına bakılarak otomatik `"8 kg kabin ücretsiz"` veya `"sadece koltuk altı"` varsayımı yapılmaz; yalnızca teklif metninden doğrulanırsa işaretlenir, aksi halde `"Bilinmiyor (Doğrulanmadı)"` gösterilir.
   - Sabit kur varsayımı yoktur; kullanıcı arayüzden manuel döviz kuru (`1 EUR -> TRY/USD`) ve tahmini transfer/bagaj ücretlerini düzenleyebilir. Eksik transfer maliyeti sessizce `0` kabul edilmez.
6. **Seçili Kombinasyonu Yeniden Kontrol Etme ve Bağlantılar:**
   - Sonuç kartlarındaki **"🔄 Fiyatı Yeniden Kontrol Et"** butonu (`/api/recheck`), yalnızca seçili gidiş ve dönüş rota/gününü önbelleği atlayarak yeniden tarar ve aynı uçuşun güncel fiyatını/bulunabilirliğini kontrol eder (başka uçuşun fiyatını seçili uçuşa yazmaz).
   - Uygulama doğrudan bilet satışı yapmaz; bağlantılar **"Google Flights'ta Aramayı Aç ↗"** olarak etiketlenir ve sonuçların iki bağımsız tek yön teklifin toplamı olduğu açıkça belirtilir.
7. **Git / GitHub Senkronizasyon Kuralı:**
   - GitHub uzak deposuna (`https://github.com/canerma/Flight-Finder.git`) otomatik `git commit` / `git push` yapılmaz.
   - Uzak depoya senkronizasyon **yalnızca kullanıcı açıkça talep ettiğinde** ("senkron et", "github'a gönder" vb.) gerçekleştirilir.

---

## 3. Dosya Yapısı ve Sorumluluklar

| Dosya | Açıklama |
| :--- | :--- |
| `scraper.py` | Playwright tabanlı Google Flights tek yön scraper modülü. `FlightOption` ve `RouteQueryResult` veri modelleri, HTML/DOM ayrıştırıcı (`parse_flights_from_html`), sorgu durumu sınıflandırması (`success`, `no_results`, `timeout`, `blocked`, `parse_error`), atomik ve epoch korumalı önbellek (`flight_cache.json`) ve seçili kombinasyon fiyat kontrolü (`recheck_combination`) işlevlerini içerir. |
| `combiner.py` | Pandas kullanarak gidiş ve dönüş tek yön uçuşlarının kartezyen çarpımını yapar (`build_combinations_dataframe`). Varış yerel tarihine göre kalış gecesi hesabı, pencere sonu varış kontrolü, nullable filtreleme, tahmini bagaj/transfer maliyetleri ve JSON `NaN -> null` temizliğini (`sanitize_records_for_json`) gerçekleştirir. |
| `server.py` | Yerel HTTP ve JSON API sunucusu (`http://localhost:8000`). `urllib.parse.urlsplit(self.path).path` ile rota eşleştirir; girdi doğrulaması (`validate_search_request`), tekil iş kilidi (`JOB_LOCK`), durdurma (`/api/scrape/stop`), ilerleme (`/api/progress`), kombinasyon (`/api/combine`), fiyat kontrolü (`/api/recheck`) ve önbellek (`/api/cache`) uç noktalarını sunar. |
| `index.html` | Sıfır harici bağımlılıklı, karanlık tema destekli yerel Web Arayüzü. En fazla 3 hedef havalimanı seçimi, manuel kur alanı, canlı sorgu istatistikleri (`başarılı / uçuş yok / hatalı / önbellekten`), durdurma/devam etme, uçuş sonrası filtreler ve "Fiyatı Yeniden Kontrol Et" özelliklerini barındırır. |
| `app.py` | Güncel veri modeliyle uyumlu alternatif Streamlit arayüzü. |
| `tests/test_regressions.py` | Kaydedilmiş örnek HTML (`tests/fixtures/*.html`) ve sahte verilerle tüm kritik senaryoları doğrulayan `unittest` regresyon test paketi. |
| `baslat.bat` | Windows'ta yerel sunucuyu (`server.py`) başlatıp tarayıcıda `http://localhost:8000` adresini açan başlatıcı betik. |

---

## 4. Çalıştırma ve Test Talimatları

### Yerel Web Sunucusu (Önerilen)
1. `baslat.bat` dosyasına çift tıklayın **veya** terminalde şu komutu çalıştırın:
   ```powershell
   & "C:\Users\canerm\AppData\Local\Python\pythoncore-3.14-64\python.exe" server.py
   ```
2. Tarayıcıda **http://localhost:8000** adresini açın.

### Regresyon Testlerini Çalıştırma
```powershell
& "C:\Users\canerm\AppData\Local\Python\pythoncore-3.14-64\python.exe" -m unittest discover -s tests -v
```
