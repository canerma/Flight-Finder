# GEMINI.md — Smart Flight Finder (Kişisel Akıllı Uçuş Bulucu)

## 1. Proje Amacı ve Özeti
**Smart Flight Finder**, kişisel kullanım için geliştirilmiş, yakın alternatif havalimanlarını (en fazla **4 hedef havalimanı**) ve esnek tarih aralıklarını tarayarak en ucuz/mantıklı **simetrik veya asimetrik gidiş-dönüş** uçuş kombinasyonlarını (ör. Gidiş: `ESB -> CGN`, Dönüş: `AMS -> ESB`) bulan yerel bir web uygulamasıdır.

---

## 2. Temel Kısıtlar ve Mimari Prensipler
1. **0 TL API Maliyeti:** Ücretli uçuş API'leri (Amadeus, Kiwi, Skyscanner vb.) kullanılmaz. Tüm uçuş verileri arka planda **Playwright** ile doğrudan **Google Flights** üzerinden çekilir.
2. **Ayrık Tek Yön (Decoupled One-Way) Kombinasyon ve Gece Hesabı:**
   - Gidiş ve dönüş rotaları **tek yön** olarak bağımsız taranır (`Kalkış -> Hedef Havalimanları` ve `Hedef Havalimanları -> Kalkış`, karşı tarafta en fazla 4 benzersiz hedef havalimanı).
   - Kullanıcı tek bir **Seyahat Tarih Aralığı** (`En Erken Gidiş` – `Eve En Geç Varış`) ve **Hedefte Kalış Süresi (Gece)** (`min_stay` – `max_stay`) seçer.
   - **Gece Sayısı Hesabı:** Hedefte kalış gecesi (`stay_days`), gidiş uçuşunun **hedefe varış yerel tarihi** (`out_arrival_date`) ile dönüş uçuşunun **kalkış yerel tarihi** (`in_date`) arasındaki gün farkı olarak hesaplanır (`+1` / `+2` / `"ertesi gün"` / `"next day"` gün farkları varış alanına bağlanarak ayrıştırılır; ör. 1 Aralık kalkış, 2 Aralık hedefe varış, 6 Aralık dönüş = **4 gece**).
   - **Dönüş Varış Kontrolü:** Dönüş uçuşunun kalkış havalimanına varış yerel tarihi (`in_arrival_date`), seyahat penceresinin son gününü (`window_end`) aşamaz.
   - Doğrulanamayan varış tarihleri veya yalnızca saat küçüklüğünden (`23:00 -> 02:00` açık gün işareti olmadan) kaynaklanan belirsizlikler sessizce aynı gün veya `+1` kabul edilmez; `None` bırakılır ve kombinasyondan elenir.
3. **Sorgu Durumları, Kapsam Takibi ve 48 Saatlik Kalıcı Önbellek (`flight_cache.json`, Şema v2):**
   - Önbellek kayıtları **48 saat (`CACHE_TTL_HOURS = 48`)** boyunca geçerlidir. Arayüzdeki **"🧹 Önbelleği Temizle (Bugün Hariç Sil)"** butonu (`keep_today=True`), içinde bulunduğumuz gün (`_today_str()`) yapılan sorguları koruyup önceki günlere ait önbellek kayıtlarını temizler.
   - Her `(origin, destination, date, currency)` sorgusu açık bir durumla kaydedilir: `success`, `no_results`, `timeout`, `blocked`, `parse_error`.
   - `no_results` durumu yalnızca sayfada gerçek "uçuş bulunamadı" mesajı doğrulandığında atanır. Hatalı sorgular (`timeout`, `blocked`, `parse_error`) asla geçerli boş sonuç gibi önbellekten sunulmaz.
   - `"Daha fazla uçuş"` genişletmesi tıklama ve kart okuma adımlarını ayırır; yeni kartların DOM'a eklenmesini sınırlı zaman aşımıyla bekler.
   - Önbellek kayıtları `schema_version`, `requested_limit`, `collected_count`, `total_dom_cards`, `limit_reached`, `all_loaded_parsed`, `scope_complete` ve `fetched_at` metaverilerini içerir. `scope_complete` yalnızca genişletme zaman aşımına uğramadığında, sayfada yüklenmemiş ek uçuş butonu kalmadığında ve yüklenen tüm kartlar ayrıştırıldığında `True` olur.
   - Toplanan ham uçuşlar filtreleme aşamasından önce kesilmez (`[:max_results]` uygulanmaz); tüm toplanan uçuşlar filtreleme aşamasına (`build_combinations_dataframe`) aktarılır ve görüntüleme limiti (`display_limit`) filtreleme + sıralama sonrasında uygulanır.
   - Önbellek yazımı geçici dosya + `os.replace` ile atomik yapılır ve `_CACHE_EPOCH` sayacı sayesinde tarama sürerken yapılan önbellek temizliği eski verilerin geri yazılmasını engeller.
4. **Eksik Bilginin Doğru Bilgi Gibi Kullanılmaması (Nullable Modelleme) ve Bağımsız Bacak Filtreleri:**
   - Okunamayan `stops`, `duration_minutes`, `departure_time`, `arrival_time`, `arrival_day_offset` ve `cabin_baggage_included` alanları `None` (`null`) olarak modellenir ve arayüzde `"Doğrulanmadı"` olarak gösterilir.
   - Aktarma filtresi (`max_stops`, `out_max_stops`, `in_max_stops`), gidiş ve dönüş için ayrı havayolu filtreleri (`out_excluded_airlines`, `in_excluded_airlines`), maksimum bacak/toplam seyahat süresi filtresi (`max_flight_hours`, `max_total_travel_hours`), kalkış saati filtresi ve `"Ücretsiz kabin bagajı"` filtresi aktifken ilgili alanı doğrulanmamış (`None`) uçuşlar filtreden geçirilmez.
   - Süresi bilinmeyen uçuşlar `0 dk` gösterilmez ve en kısa uçuş gibi en üste sıralanmaz (`na_position="last"`).
5. **Bağımsız Bacak Bagaj Filtreleri, Tahmin Motoru (`baggage_rules.py`) ve Kara Ulaşımı:**
   - Gidiş ve dönüş bacakları için bağımsız bagaj seçenekleri sunulur: Üst dolap kabin valizi (`"none"` / `"1x8kg"`), uçak altı bagajı (`0, 5, 10, 15, 20, 30, 35 kg`), isteğe bağlı `"Bu yönde bagaj dahil olsun"` filtresi (`out_baggage_included_only`, `in_baggage_included_only`) ve isteğe bağlı manuel ek ücret alanları.
   - Doğrulanmış teklif verisi her zaman varsayımın ve tahminin önündedir (`verified_offer > package_rule > assumption > unknown`). Teklifte kabin veya uçak altının dahil olmadığı ("kabin dahil değil" veya "0 kg") açıkça belirtilmişse hiçbir varsayım bunu geçersiz kılamaz. Tahmini haklar `"Bu yönde bagaj dahil olsun"` (`baggage_included_only`) filtresinden geçemez.
   - Kartta "Bagaj dahil" yazıp kilo belirtilmemişse 20 kg atanmaz; `"Uçak altı dahil · kilo doğrulanmadı"` olarak modellenir ve kilo garantisi olmadığı için bagaj dahil filtresini tek başına sağlayamaz.
   - Yetişkin ekonomi Türkiye–Avrupa hatları referans verileri (`baggage_rules.py`):
     - **SunExpress:** Uluslararası/standart Avrupa hatlarında paket bilinmediğinde **SunValue varsayımı** (8 kg kabin + 20 kg uçak altı tahminen dahil) kullanılır; bu sınırlar içinde ek ücret €0'dur ancak tahmin olduğu için bilette doğrulanmış sayılmaz. Paket SunLight ise başlangıç tarifesi uygulanır: 8 kg kabin €19,99; uçak altı 5 kg €19,99, 10 kg €24,99, 15 kg €29,99, 20 kg €34,99. SunEco+ (>20 kg) ve SunComfort (>35 kg) için sabit ücret üretilmez, teklifte açık fiyat yoksa maliyet belirsiz (`None`) kalır.
     - **THY:** EcoFly 8 kg kabin + 4 kg kişisel çanta dahil. EcoFly uçak altı rotaya bağlıdır; her bilete sabit 15 kg veya 0 kg atanmaz, teklifte fiyat yoksa belirsiz bırakılır.
     - **AJet:** Basic 4 kg koltuk altı ücretsiz; kabin çelişkili olduğundan doğrulanmadıkça `"AJet Basic varsayımı: kabin ücretli, bilette kontrol edin"` etiketi verilir. Almanya/Hollanda online referansı: 8 kg kabin €15; uçak altı 0–10 kg €12, 11–20 kg €25. Bu referanslar yalnızca bagajsız bilete ilk bagaj ekleme tarifesidir; ek kilo veya 20 kg üzeri için uygulanamaz. Yalnızca Türkiye ile Almanya/Hollanda arasındaki direkt uçuşlarda geçerlidir; aktarma sayısı bilinmeyen veya Türkiye dışı hatlarda otomatik uygulanmaz.
     - **Pegasus:** Light 3 kg koltuk altı ücretsiz. Eylül 2026 kampanya rakamları tarihli kampanya referansıdır; otomatik tahmin olarak uygulanmaz ve fiyat yoksa belirsiz (`unknown`) bırakılır.
   - Aktarmalı uçuşlarda referans ücretler aktarma sayısıyla çarpılmaz; belirsiz bırakılır.
   - Sabit kur varsayımı yoktur; kullanıcı arayüzden manuel döviz kuru (`1 EUR -> TRY/USD`) girebilir. Kur girilmediğinde EUR referansları TRY/USD toplamına eklenmez (belirsiz kalır).
   - Eksik fiyat durumlarında bilinen tutarlar korunur: `known_subtotal_price` (uçuş toplamı + bilinen bagaj + transfer) hesaplanır. Tüm seçili bagaj maliyetleri biliniyorsa `grand_total_price` bu toplama eşitlenir; aksi halde `grand_total_price = None` (`null`) kalır ve kartta bilinen ara toplam ile eksik bacak notu (`missing_baggage_note`) gösterilir.
   - Sıralama: Bagaj maliyeti tam bilinen seçenekler (`baggage_cost_known=True`) `grand_total_price`'a göre en önde yer alır; eksik olanlar ise `known_subtotal_price`'a göre bunların arkasında listelenir. Tüm sonuçlar eksikse en iyi fiyat göstergesi "Maliyet Belirsiz" olarak sunulur.
   - Eksik transfer maliyeti sessizce `0` kabul edilmez.
6. **Seçili Kombinasyonu Yeniden Kontrol Etme ve Bağlantılar:**
   - Sonuç kartlarındaki **"🔄 Fiyatı Yeniden Kontrol Et"** butonu (`/api/recheck`), yalnızca seçili gidiş ve dönüş rota/gününü önbelleği atlayarak yeniden tarar. Uçuş numarası (`flight_number`), kalkış/varış saatleri, varış yerel tarihi (`arrival_date` / `arrival_day_offset`), aktarma sayısı (`stops`), sıralı segmentler (`segments`) ve aktarma havalimanları (`layover_airports`) birlikte doğrulanır. Farklı uçuş numaraları veya farklı varış günleri asla eşleştirilmez; kimlik bilgisi yetersiz veya belirsizse `"identity_unverified"` (`"Uçuş kimliği doğrulanamadı"`) durumu döndürülür.
   - Uygulama doğrudan bilet satışı yapmaz; bağlantılar **"Google Flights'ta Aramayı Aç ↗"** olarak etiketlenir ve sonuçların iki bağımsız tek yön teklifin toplamı olduğu açıkça belirtilir.
7. **Git / GitHub Senkronizasyon Kuralı:**
   - GitHub uzak deposuna (`https://github.com/canerma/Flight-Finder.git`) otomatik `git commit` / `git push` yapılmaz.
   - Uzak depoya senkronizasyon **yalnızca kullanıcı açıkça talep ettiğinde** ("senkron et", "github'a gönder" vb.) gerçekleştirilir.

---

## 3. Dosya Yapısı ve Sorumluluklar

| Dosya | Açıklama |
| :--- | :--- |
| `scraper.py` | Playwright tabanlı Google Flights tek yön scraper modülü. `FlightOption` ve `RouteQueryResult` veri modelleri, HTML/DOM ayrıştırıcı (`parse_flights_from_html`), sorgu durumu sınıflandırması (`success`, `no_results`, `timeout`, `blocked`, `parse_error`), atomik ve epoch korumalı önbellek (`flight_cache.json`) ve seçili kombinasyon fiyat kontrolü (`recheck_combination`) işlevlerini içerir. |
| `baggage_rules.py` | Yetişkin ekonomi Türkiye–Avrupa hatları için havayolu kuralları (SunExpress, THY, AJet, Pegasus), bacak bazında bağımsız bagaj değerlendirmesi (`evaluate_leg_baggage`), referans EUR/hedef döviz dönüşümü ve filtre uyumluluğunu yöneten modül. |
| `combiner.py` | Pandas kullanarak gidiş ve dönüş tek yön uçuşlarının kartezyen çarpımını yapar (`build_combinations_dataframe`). Varış yerel tarihine göre kalış gecesi hesabı, pencere sonu varış kontrolü, bağımsız bagaj maliyetleri, nullable filtreleme, tahmini bagaj/transfer maliyetleri ve JSON `NaN -> null` temizliğini (`sanitize_records_for_json`) gerçekleştirir. |
| `server.py` | Yerel HTTP ve JSON API sunucusu (`http://localhost:8000`). `urllib.parse.urlsplit(self.path).path` ile rota eşleştirir; girdi doğrulaması (`validate_search_request`), tekil iş kilidi (`JOB_LOCK`), durdurma (`/api/scrape/stop`), ilerleme (`/api/progress`), kombinasyon (`/api/combine`), fiyat kontrolü (`/api/recheck`) ve önbellek (`/api/cache`) uç noktalarını sunar. |
| `index.html` | Sıfır harici bağımlılıklı, karanlık tema destekli yerel Web Arayüzü. En fazla 4 hedef havalimanı seçimi, manuel kur alanı, canlı sorgu istatistikleri (`başarılı / uçuş yok / hatalı / önbellekten`), durdurma/devam etme, uçuş sonrası filtreler ve "Fiyatı Yeniden Kontrol Et" özelliklerini barındırır. |
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
