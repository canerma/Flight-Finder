# Flight Finder ✈️

**Esnek tarihler ve farklı havalimanları arasında kişisel gidiş-dönüş uçuş kombinasyonları bulucu.**

Ankara’dan Eindhoven’a gitmek istediğinizi düşünün: Amsterdam, Düsseldorf ve Köln’e gidişleri ve bu havalimanlarından Ankara’ya dönüşleri tek tek karşılaştırmak yerine, Flight Finder seçtiğiniz tarih aralığında bu seçenekleri birleştirir. Gidişte Köln, dönüşte Amsterdam kullanan bir seyahat de sonuçlarda yer alabilir.

Uygulama bilgisayarınızda çalışır. Uçuş verilerini Playwright aracılığıyla Google Flights sayfalarından toplar; ücretli uçuş API’si veya API anahtarı gerektirmez. Bilet satın alma işlemini ilgili satıcının sitesinde kendiniz yaparsınız.

## Özellikler

- Tek çıkış/geri dönüş noktası ve en fazla **3 hedef havalimanı**.
- Esnek seyahat penceresi ve minimum/maksimum kalış gecesi.
- Aynı veya farklı havalimanlarından gidiş-dönüş kombinasyonları.
- Gidiş ve dönüş için ayrı aktarma sınırları, kalkış saati ve yolculuk süresi filtreleri.
- İsteğe bağlı tahmini bagaj ve kara ulaşımı ücretleri; manuel döviz kuru.
- Toplam maliyete, eşitlikte yolculuk süresine göre sıralama.
- Günlük disk önbelleği, tarama ilerlemesi ve sorgu durumları.
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
2. Hedef bölgeyi ve en fazla üç havalimanını seçin: **AMS, DUS, CGN**.
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
Gidiş/dönüş tek yön sorguları → Günlük önbellek
        ↓
Toplanan uçuşları yerelde eşleştirme
        ↓
Gece, tarih, aktarma, saat ve süre filtreleri
        ↓
Toplam maliyet hesabı ve sonuçlar
```

Aktarmalı seçenekler Google Flights sonuçlarından alınır. Uygulama bağımsız ara uçuşları tarayarak kendi aktarma ağını oluşturmaz. Gidiş ve dönüş ayrı tek yön teklifler olarak birleştirilir; bu toplam, havayolunun normal gidiş-dönüş veya çok şehirli tarifesinden farklı olabilir.

## Önbellek ve tarama durumları

`flight_cache.json`, sorguları havalimanları, tarih ve para birimi bazında saklar. Aynı günün kullanılabilir ve yeterli kapsamlı kayıtları yeniden kullanılır. Dosya Git tarafından dışlanır.

| Durum | Anlamı |
| --- | --- |
| `success` | Uçuş verisi okunabildi. |
| `no_results` | Sayfada sonuç bulunmadığı tespit edildi. |
| `timeout` | Sayfa veya sonuçlar zamanında yüklenmedi. |
| `blocked` | Erişim engeli ya da CAPTCHA tespit edildi. |
| `parse_error` | Sayfa verisi beklenen biçimde okunamadı. |

Hatalı veya eksik sorgular varsa tarama kapsamını kontrol edin. Günlük önbellek canlı fiyat garantisi vermez; yeniden kontrol veya önbelleği temizleme işlemiyle fiyatları tekrar sorgulayabilirsiniz.

## Sınırlamalar

Bu proje kişisel kullanım için geliştirilen bir prototiptir; Google veya havayollarıyla resmi bir bağlantısı yoktur.

- Google Flights’ın sayfa yapısı, yükleme davranışı, çerez ekranları veya erişim kısıtları veri toplamayı etkileyebilir.
- Sonuçlar toplanabilen uçuşlarla sınırlıdır; tüm piyasadaki en ucuz bileti bulma garantisi yoktur.
- Google Flights’ta görülen teklif, havayolunun kendi sitesinde aynı fiyata bulunmayabilir. Bağlantılar belirli bir rezervasyonu değil arama sayfasını açar.
- Bagaj, varış günü ve uçuş kimliği metin ayrıştırmasına dayanır. Yeniden fiyat kontrolü de satın alma garantisi değildir.
- Transfer ücretleri, süreleri ve manuel kur tahminidir; güncel sefer saatlerini, gece ulaşımını ve bağlantı uygunluğunu ayrıca kontrol edin.
- Ayrı tek yön biletler bağımsız satın alınır. Birini alırken diğerinin fiyatı veya uygunluğu değişebilir.

### İncelemede belirlenen açık noktalar

Son kod incelemesinde aşağıdaki durumlar tespit edilmiştir; ilgili regresyon testleriyle doğrulanarak giderilmeleri gerekir:

- “Kabin bagajı dahil değil” gibi olumsuz ifadelerin yanlışlıkla dahil sayılması.
- Yeniden kontrolde farklı uçuş numaralarının benzer saatler üzerinden eşleşebilmesi.
- Bazı ertesi gün varış ifadelerinde gün farkının yanlış okunması.
- Filtrelemeden önce uygulanan sonuç limitinin uygun seçenekleri elemesi.
- “Daha fazla uçuş” tıklaması sonrasında yüklenen kartların kaçırılabilmesi.

## Testler

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Testler sahte uçuş verileri, yerel HTML örnekleri ve bazı senaryolarda geçici yerel HTTP sunucusu kullanır. Testlerin geçmesi canlı Google Flights sayfasıyla uyumluluğun doğrulandığı anlamına gelmez. HTTP testleri için ortamın yerel bağlantılara izin vermesi gerekir.

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
| Fiyat eski görünüyor | Seçili kombinasyonu yeniden kontrol edin veya önbelleği temizleyip yeniden tarayın. |
| CAPTCHA veya erişim engeli | Taramayı durdurup daha sonra deneyin; engeli aşmaya yönelik otomasyon kullanmayın. |
