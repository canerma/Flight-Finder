# Flight Finder ✈️

**Esnek tarihler ve farklı havalimanları arasında kişisel gidiş-dönüş uçuş kombinasyonları bulucu.**

Ankara’dan Eindhoven’a gitmek istediğinizi düşünün: Amsterdam, Düsseldorf ve Köln’e gidişleri ve bu havalimanlarından Ankara’ya dönüşleri tek tek karşılaştırmak yerine, Flight Finder seçtiğiniz tarih aralığında bu seçenekleri birleştirir. Gidişte Köln, dönüşte Amsterdam kullanan bir seyahat de sonuçlarda yer alabilir.

Uygulama bilgisayarınızda çalışır. Uçuş verilerini Playwright aracılığıyla Google Flights sayfalarından toplar; ücretli uçuş API’si veya API anahtarı gerektirmez. Bilet satın alma işlemini ilgili satıcının sitesinde kendiniz yaparsınız.

## Özellikler

- Tek çıkış/geri dönüş noktası ve en fazla **4 hedef havalimanı**.
- Esnek seyahat penceresi ve minimum/maksimum kalış gecesi.
- Aynı veya farklı havalimanlarından gidiş-dönüş kombinasyonları.
- Gidiş ve dönüş için ayrı aktarma ve havayolu filtreleri; kalkış saati, tek yön ve toplam yolculuk süresi sınırları.
- İsteğe bağlı tahmini kara ulaşımı transfer ücretleri; manuel döviz kuru (`1 EUR -> TRY/USD`).
- Gidiş ve dönüş için bağımsız bagaj seçenekleri (üst dolap kabin valizi, 5/10/15/20 kg uçak altı bagaj kademeleri, Türkiye–Avrupa havayolu paket referansları, isteğe bağlı dahil olma filtresi ve manuel ek ücret alanları).
- Toplam maliyete, eşitlikte yolculuk süresine göre sıralama (bagaj maliyeti belirsiz olan seçenekler bilinenlerin önüne haksız geçemez).
- 48 saatlik disk önbelleği, tarama ilerlemesi ve sorgu durumları.
- Taramayı durdurma ve önbellekteki kullanılabilir sonuçlardan devam etme.
- Seçili kombinasyonun fiyatını yeniden arama, Google Flights arama bağlantıları ve CSV dışa aktarma.
- Yerel web arayüzü ve alternatif Streamlit arayüzü.

## Kurulum — Windows / PowerShell

Git ve Python kurulu olmalıdır. Proje yerel geliştirme ortamında Python 3.14 ile çalıştırılmıştır; bağımlılıklar `requirements.txt` dosyasındadır.

```powershell
git clone https://github.com/canerma/Flight-Finder.git
cd Flight-Finder

py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

`py` komutu bulunamıyorsa ilk Python komutunda `python` veya Python kurulumunuzun tam yolunu kullanın. Sanal ortamı etkinleştirmek zorunlu değildir; aşağıdaki komutlar doğrudan sanal ortamın Python yorumlayıcısını kullanır.

## Çalıştırma

Proje klasöründe:

```powershell
.\.venv\Scripts\python.exe server.py
```

Tarayıcıda [http://localhost:8000](http://localhost:8000) adresini açın. Sunucuyu durdurmak için terminalde `Ctrl+C` kullanın. Sunucu varsayılan olarak yalnızca `127.0.0.1:8000` üzerinde dinler.

> `baslat.bat` mevcut geliştiricinin bilgisayarına özgü sabit bir Python yolu içerir. Başka bir bilgisayarda kullanmadan önce bu yolu değiştirin veya yukarıdaki komutu kullanın.

Alternatif Streamlit arayüzü:

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

Ana kullanım için `server.py` ile açılan web arayüzü tercih edilir. İki arayüzün özellikleri birebir aynı olmayabilir.

## Kullanım

1. Çıkış havalimanını girin: örneğin **ESB**.
2. Hedef bölgeyi ve en fazla dört havalimanını seçin: **AMS, DUS, CGN**.
3. En erken gidiş ve eve en geç varış tarihini belirleyin.
4. Hedefte kalmak istediğiniz gece aralığını girin: örneğin **4–6 gece**.
5. Para birimini ve tarama ayarlarını seçip taramayı başlatın. İlk denemede kısa bir tarih aralığı kullanın.
6. Aktarma, kalkış saati, süre ve isteğe bağlı maliyet filtreleriyle sonuçları daraltın.
7. Uygun kombinasyonu yeniden kontrol edin; arama bağlantısını açıp uçuş ayrıntılarını ve nihai fiyatı satıcıda doğrulayın.

Kalış gecesi, gidişin hedefe varış yerel tarihi ile dönüşün kalkış yerel tarihi arasındaki farktır. Örneğin 1 Aralık kalkış, 2 Aralık varış ve 6 Aralık dönüş **4 gece** eder. Bu hesabın doğruluğu, sayfadan varış tarihinin doğru okunmasına bağlıdır.

Transfer seçildiğinde toplam maliyet hesabı:

```text
Gidiş bileti + dönüş bileti + tahmini bagaj ücretleri + iki yöndeki kara ulaşımı
```

Toplam yolculuk süresi filtresi gidiş ve dönüş uçuş sürelerinin toplamını; transfer etkinse transfer sürelerini de kapsar. Hedefte geçirilen günler bu süreye dahil değildir.

## Nasıl çalışır?

```text
Rota ve tarih seçimi
        ↓
Gidiş/dönüş tek yön sorguları → 48 saatlik önbellek
        ↓
Toplanan uçuşları yerelde eşleştirme
        ↓
Gece, tarih, aktarma, saat ve süre filtreleri
        ↓
Toplam maliyet hesabı ve sonuçlar
```

Aktarmalı seçenekler Google Flights sonuçlarından alınır. Uygulama bağımsız ara uçuşları tarayarak kendi aktarma ağını oluşturmaz. Gidiş ve dönüş ayrı tek yön teklifler olarak birleştirilir; bu toplam, havayolunun normal gidiş-dönüş veya çok şehirli tarifesinden farklı olabilir.

## Önbellek ve tarama durumları

`flight_cache.json`, sorguları havalimanları, tarih ve para birimi bazında saklar. Son 48 saat içindeki kullanılabilir ve yeterli kapsamlı kayıtlar yeniden kullanılır; bu sürenin dışındaki kayıtlar geçersiz sayılır. Dosya Git tarafından dışlanır.

| Durum | Anlamı |
| --- | --- |
| `success` | Uçuş verisi okunabildi. |
| `no_results` | Sayfada sonuç bulunmadığı tespit edildi. |
| `timeout` | Sayfa veya sonuçlar zamanında yüklenmedi. |
| `blocked` | Erişim engeli ya da CAPTCHA tespit edildi. |
| `parse_error` | Sayfa verisi beklenen biçimde okunamadı. |

Hatalı veya eksik sorgular varsa tarama kapsamını kontrol edin. Önbellek canlı fiyat garantisi vermez. Seçili kombinasyonun **Fiyatı Yeniden Kontrol Et** düğmesi, ilgili iki rota/gün için önbelleği atlayarak yeniden arama yapar.

**Önbelleği Temizle (Bugün Hariç Sil)** düğmesi bugünkü kayıtları korur, önceki günlere ait kayıtları siler. Bu işlem bugünkü fiyatları yenilemez.

Kapsam göstergeleri farklı anlamlara gelir:

| Alan | Anlamı |
| --- | --- |
| `limit_reached` | İstenen sayıda tekil uçuşa ulaşıldı mı? |
| `all_loaded_parsed` | Yüklenen kartların fiyatları okunabildi mi? |
| `scope_complete` | Genişletme zaman aşımı veya kalan ek sonuç düğmesi olmadan, yüklenen kartların fiyatları okunabildi mi? |

Bu göstergeler bagaj, segment veya varış tarihi gibi her ayrıntının doğrulandığını ya da piyasadaki tüm uçuşların kapsandığını garanti etmez. Toplanan uçuşlar filtrelenmeden önce sonuç limitiyle kesilmez; görüntüleme limiti filtreleme ve sıralamadan sonra uygulanır.

## Sınırlamalar

Bu proje kişisel kullanım için geliştirilen bir prototiptir; Google veya havayollarıyla resmi bir bağlantısı yoktur.

- Google Flights’ın sayfa yapısı, yükleme davranışı, çerez ekranları veya erişim kısıtları veri toplamayı etkileyebilir.
- Sonuçlar toplanabilen uçuşlarla sınırlıdır; tüm piyasadaki en ucuz bileti bulma garantisi yoktur.
- Google Flights’ta görülen teklif, havayolunun kendi sitesinde aynı fiyata bulunmayabilir. Bağlantılar belirli bir rezervasyonu değil arama sayfasını açar.
- Bagaj, varış günü ve uçuş kimliği metin ayrıştırmasına dayanır. Yeniden fiyat kontrolü de satın alma garantisi değildir.
- Transfer ücretleri, süreleri ve manuel kur tahminidir; güncel sefer saatlerini, gece ulaşımını ve bağlantı uygunluğunu ayrıca kontrol edin.
- Ayrı tek yön biletler bağımsız satın alınır. Birini alırken diğerinin fiyatı veya uygunluğu değişebilir.

### Veri doğrulama davranışı

- Bagaj metinlerindeki olumsuz ifadeler dikkate alınır. Bağımsız olumlu ve olumsuz bilgiler çelişiyorsa veya bilgi yoksa bagaj durumu bilinmiyor olarak bırakılır.
- Yeniden fiyat kontrolünde farklı uçuş numaraları reddedilir; saat, varış günü ve mevcut aktarma bilgileri karşılaştırılır. Yetersiz veya belirsiz kimlikte **Uçuş kimliği doğrulanamadı** sonucu gösterilir.
- `+1`, `+2`, “ertesi gün” ve “next day” gibi varış işaretleri desteklenir. Bazı durumlarda saat/süre bağlamından çıkarım yapıldığından varış gününü satıcının ekranında ayrıca doğrulayın.
- “Daha fazla uçuş” sonrasında gecikmeli yüklenen kartlar sınırlı süre beklenerek toplanır; genişletme zaman aşımında kapsam eksik olarak belirtilir.

Bu davranışlar önceki incelemede tespit edilen beş sorunun düzeltmelerini içerir. Desteklenmeyen sayfa biçimleri veya eksik veri için doğruluk garantisi verilmez.

## Testler

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Testler sahte uçuş verileri, yerel HTML örnekleri, geçici yerel HTTP sunucusu ve gecikmeli kart yüklemesi için Playwright kullanır. Tarayıcı testleri için kurulumdaki Chromium indirme adımı tamamlanmalıdır.

Paket; bagaj olumsuzluğu/çelişkisi, uçuş kimliği, gece aşımı, filtre öncesi sonuç kesilmesi, asenkron kart yüklemesi, dört hedef sınırı, 48 saatlik önbellek ve ayrı havayolu filtrelerini kapsar. Testlerin geçmesi canlı Google Flights sayfasıyla uyumluluğun doğrulandığı anlamına gelmez. HTTP testleri için ortamın yerel bağlantılara izin vermesi gerekir.

## Dosya yapısı

| Dosya | Sorumluluk |
| --- | --- |
| `server.py` | Yerel HTTP sunucusu, istek doğrulama ve tarama işleri |
| `scraper.py` | Playwright taraması, veri ayrıştırma, önbellek ve fiyat kontrolü |
| `combiner.py` | Uçuş eşleştirme, filtreler ve toplam maliyet/süre hesabı |
| `index.html` | Ana web arayüzü |
| `app.py` | Alternatif Streamlit arayüzü |
| `tests/` | Regresyon testleri ve HTML örnekleri |
| `requirements.txt` | Python bağımlılıkları |
| `GEMINI.md` | Proje bağlamı ve geliştirme kuralları |

## Sorun giderme

| Sorun | Kontrol |
| --- | --- |
| Tarayıcı başlatılamıyor | Sanal ortamın Python’u ile `-m playwright install chromium` komutunu çalıştırın. |
| Yerel sayfa açılmıyor | `server.py` terminalinin açık olduğunu ve 8000 portunun başka süreçte kullanılmadığını kontrol edin. |
| Sonuç çıkmıyor | Tarih/gece aralığını, filtreleri ve sorgu durumlarını inceleyin. “Uçuş yok” ile tarama hatasını ayırın. |
| Fiyat eski görünüyor | Seçili kombinasyonu yeniden kontrol edin. Temizleme düğmesinin bugünkü kayıtları koruduğunu unutmayın. |
| CAPTCHA veya erişim engeli | Taramayı durdurup daha sonra deneyin; engeli aşmaya yönelik otomasyon kullanmayın. |
