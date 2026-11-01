# Power BI Dashboard Rehberi

Bu rehber, `reports/powerbi/talay_ais_dashboard.xlsx` dosyasından 4 sayfalık dashboard'u adım adım kurmayı anlatır.
Tüm hesaplamalar Python tarafında yapıldı; Power BI'da sadece tabloları yükleyip görselleri yerleştiriyoruz.

Excel dosyası güncel değilse önce şunu çalıştır (encounters ve compare_talay'den sonra):

```bash
python -m src.big_data.powerbi_export
```

Menü adları Türkçe arayüze göre yazıldı. Power BI İngilizce açılırsa sık geçenlerin karşılıkları:

| Türkçe | English |
|---|---|
| Veri al | Get data |
| Model görünümü | Model view |
| Yeni ölçü | New measure |
| Sütuna göre sırala | Sort by column |
| Toplama yapma | Don't summarize |
| Görsel filtreleri | Filters on this visual |
| Dilimleyici / Dilimleyicileri eşitle | Slicer / Sync slicers |
| Kart, Halka grafik, Dağılım grafiği | Card, Donut chart, Scatter chart |
| Yayımla / Web'de yayımla | Publish / Publish to web |

---

## 0. Kurulum

1. **Power BI Desktop**'ı Microsoft Store'dan kur (ücretsiz). "Power BI Desktop" diye arat.
2. Açınca giriş yapmanı isteyebilir. Okul e-postanla (Piri Reis) giriş yap; yayınlama adımında bu hesap gerekecek.
3. **Dil/ondalık ayarı:** Dosya → Seçenekler ve ayarlar → Seçenekler → *Geçerli dosya* → Bölgesel ayarlar →
   "İçeri aktarma için yerel ayar" = **English (United States)**. Excel kullandığımız için çoğu sorun zaten yok, ama
   bu ayar sayıların 0.82 gibi doğru okunmasını garantiler.

## 1. Veriyi yükleme

1. Giriş → **Veri al → Excel çalışma kitabı** → `reports/powerbi/talay_ais_dashboard.xlsx`.
2. Gezgin penceresinde **10 sayfanın hepsini** işaretle → **Yükle**.
3. Sağdaki *Veri* panelinde 10 tablo görünmeli. Satır sayıları:

| Tablo | Satır | Ne işe yarar |
|---|---|---|
| summary | 1 | KPI kartları (ham satır, temiz satır, gemi sayısı…) |
| cleaning_steps | 8 | Her temizleme kuralının sildiği satır |
| format_comparison | 10 | CSV ve Parquet: boyut ve sorgu süresi |
| ship_types | 24 | Gemi tipine göre gemi sayısı |
| hourly_traffic | 1.080 | Saat başına Øresund'daki gemi sayısı |
| traffic_grid | 4.100 | ~1 km'lik hücrelerde trafik yoğunluğu (harita) |
| encounters | 4.157 | Bulunan tüm karşılaşmalar |
| encounter_ships | 8.314 | Her karşılaşmadaki iki gemi |
| scenario_points | 1.464 | Gerçek karşılaşmalar ve Talay senaryoları, aynı ölçekte |
| talay_vs_real | 90 | Talay değeri ile gerçek P10 / medyan / P90 |

### İlişki (tek bir tane yeterli)

*Model görünümü*ne geç (sol taraftaki üçüncü simge). `encounters[encounter_id]` alanını sürükleyip
`encounter_ships[encounter_id]` üzerine bırak. İlişki **Bire çok (1:\*)** olmalı. Power BI otomatik başka ilişkiler
oluşturduysa onları sil; diğer tablolar bağımsız kalacak.

### Ölçüler (DAX)

`encounters` tablosuna sağ tıkla → **Yeni ölçü** ve aşağıdakileri tek tek ekle:

```DAX
Encounters = CALCULATE(COUNTROWS(encounters), encounters[kept] = TRUE())

Close calls = CALCULATE(COUNTROWS(encounters), encounters[kept] = TRUE(), encounters[close_call] = TRUE())

Close call % = DIVIDE([Close calls], [Encounters])

Median closest distance (m) = CALCULATE(MEDIAN(encounters[min_dist_m]), encounters[kept] = TRUE())

Median speed ratio = CALCULATE(MEDIAN(encounters[speed_ratio]), encounters[kept] = TRUE())
```

`Close call %` ölçüsünü seçip üst menüden biçimini **Yüzde** yap.

### Sıralama ayarları

- `cleaning_steps[step]` sütununu seç → *Sütun araçları* → **Sütuna göre sırala** → `step_order`.
- `encounters[distance_band]` için sıralama sütunu yok. Grafikte *Diğer seçenekler (…)* → *Sırala* menüsünden
  ayarlayabilirsin, ya da olduğu gibi bırakabilirsin.

**Önemli:** `encounters` tablosunda dışlanan karşılaşmalar da var. Analiz görsellerinde ya yukarıdaki ölçüleri
kullan ya da görsele **kept = True** filtresi ekle. Yoksa liman otobüsleri ve römorkörler de sayılır.

---

## Sayfa 1: Veri hattı (Big Data kısmı)

Amaç: verinin ne kadar büyük olduğunu ve neden temizlik/Parquet gerektiğini göstermek.

1. **Kartlar** (Görselleştirmeler → Kart). `summary` tablosundan sırayla şunları sürükle: `raw_rows`, `clean_rows`,
   `duplicate_pct`, `ships`, `ships_unknown_type`. Tablo tek satır olduğu için varsayılan Toplam doğru sonucu verir.
   Başlıkları değiştir: "Ham AIS satırı", "Temiz konum", "Kopya satır %", "Gemi", "Tipi bilinmeyen gemi".
2. **Yatay çubuk grafik**: Y ekseni `cleaning_steps[step]`, X ekseni `rows_removed`.
   Başlık: "Temizleme adımlarında silinen satırlar".
3. **Kümelenmiş sütun grafik** (iki tane yan yana):
   - X ekseni `format_comparison[date]` (tarih hiyerarşisini kapat: alanın yanındaki oka tıkla → `date`),
     Y ekseni `size_mb`, Gösterge `format`. Başlık: "Dosya boyutu (MB)".
   - Aynısı `query_s` ile. Başlık: "Sorgu süresi (saniye)".

## Sayfa 2: Øresund trafiği

1. **Harita.** İki seçenek var:
   - **Azure Maps / Harita görseli:** Enlem `traffic_grid[cell_lat]`, Boylam `cell_lon`, Kabarcık boyutu
     `ship_minutes`. Harita görseli kapalıysa: Dosya → Seçenekler → Güvenlik → harita görsellerini etkinleştir.
   - **Harita çalışmazsa Dağılım grafiği:** X ekseni `cell_lon`, Y ekseni `cell_lat` (ikisinde de **Toplama yapma**
     seç), Boyut `ship_minutes`. Arka plan haritası olmaz ama boğazın şekli net görünür. Yayınlamada sorun çıkarmaz.
2. **Çizgi grafik**: X ekseni `hourly_traffic[hour_start]` (hiyerarşiyi kapat), Y ekseni `ships`.
   Başlık: "Saatlik gemi sayısı".
   Not: `ships` saat içinde ayrı gemileri sayar. Gemi grupları arasında toplanabilir ama saatler arasında
   toplanmamalı. Bu yüzden toplam kart olarak kullanma.
3. **Yığılmış çubuk**: Y ekseni `ship_types[ship_group]`, X ekseni `ships_in_oresund`.
4. **Dilimleyici**: `hourly_traffic[ship_group]`. Bu dilimleyici sadece kendi tablosunu (saatlik grafik) filtreler.
   Haritayı da filtrelemek istersen haritaya ayrıca `traffic_grid[ship_group]` dilimleyicisi koy.

## Sayfa 3: Karşılaşmalar

1. **Kartlar**: `Encounters`, `Close calls`, `Close call %`, `Median closest distance (m)`.
2. **Halka grafik**: Gösterge `encounters[encounter_type]`, Değerler `Encounters` ölçüsü.
3. **Harita veya dağılım grafiği**: Enlem `closest_lat`, Boylam `closest_lon`, Gösterge `encounter_type`.
   Görsel filtreleri bölümüne **kept = True** ekle.
4. **Kümelenmiş sütun**: X ekseni `encounters[hour]`, Y ekseni `Encounters`, Gösterge `encounter_type`.
   Başlık: "Saatlere göre karşılaşmalar".
5. **Yığılmış çubuk**: Y ekseni `distance_band`, X ekseni `Encounters`, Gösterge `encounter_type`.
   Başlık: "En yakın mesafe".
6. **Çubuk grafik (filtreleme şeffaflığı)**: Y ekseni `status`, X ekseni `encounter_id` (**Sayı** olarak).
   Başlık: "Bulunan karşılaşmalar ve neden dışlandıkları". Bu grafik, liman ve servis gemisi filtrelerini hocaya
   göstermek için önemli.
7. **Çubuk grafik (gemi tipleri)**: Y ekseni `encounter_ships[ship_group_filled]`, X ekseni
   `encounter_ships[mmsi]` (**Sayı**). Görsel filtresi olarak `encounters[kept] = True` ekle (ilişki sayesinde
   çalışır). `ship_group_filled` sütununda AIS tipi bilinmeyen gemiler, veri madenciliği modelinin tahminiyle
   ("Cargo (predicted)" gibi) görünür. İki dersin birleştiği yer burası.
8. **Dilimleyiciler**: `encounters[date]`, `encounters[encounter_type]`.

## Sayfa 4: Talay ve gerçek deniz

Bu sayfa projenin ana bulgusu, en önemli görsel burada.

1. **Dağılım grafiği**: X ekseni `scenario_points[course_diff_deg]`, Y ekseni `speed_ratio`
   (ikisinde de **Toplama yapma**), Gösterge `source`. Ayrıntılar alanına `label` koy (fareyle üzerine gelince
   senaryo adı görünür).
   - Görsel filtreleri: `close_call = True`. Böylece gerçek tarafta sadece 500 m'den yakın geçişler kalır.
   - Biçim → İşaretçiler: "Talay scenario" için büyük ve koyu renk, "Real encounter" için küçük ve açık renk.
   - Başlık: "Talay senaryoları ve gerçek yakın geçişler: rota açısı – hız oranı".
2. Aynı dağılım grafiğini kopyala ve Y eksenine `bearing_deg` koy. Başlık: "Karşı geminin baştan açısı".
3. **Dilimleyici**: `scenario_points[encounter_type]`. Bunu iki grafiğe de bağla: Görünüm → *Dilimleyicileri eşitle*.
4. **Tablo**: `talay_vs_real` tablosundan `condition_id`, `metric_label`, `talay_value`, `real_p10`, `real_median`,
   `real_p90`, `within_real_range`.
   - Dilimleyici `real_group`: "Close calls (< 500 m)" seç.
   - Koşullu biçimlendirme: `within_real_range` sütununa sağ tıkla → Koşullu biçimlendirme → Arka plan rengi →
     Kurallar: False ise turuncu.
5. Sayfanın altına kısa bir **metin kutusu** ekle (Ekle → Metin kutusu):
   > Gerçek veri, Talay'ın kesişen rota senaryolarının gerçekçi olduğunu gösteriyor. Karşı karşıya ve geçme
   > senaryoları ise uç durumları test ediyor: hız oranları gerçek denizde nadir görülüyor. Önerilen yeni koşullar:
   > ~120° çapraz kesişme, ~0,8 hız oranı, baştan ~10° açıyla karşı karşıya geliş, ~0,7 hız oranıyla geçme.

---

## Görünüm için kısa ipuçları

- Görünüm → **Tema**: sade bir tema seç ve tüm sayfalarda aynı renkleri kullan. Karşılaşma tipleri her yerde aynı
  renkte olmalı: Head-on, Crossing, Overtaking.
- Her sayfanın üstüne bir başlık metin kutusu koy.
- Gereksiz kenarlık ve gölgeleri kapat. 4–6 görsel bir sayfa için yeterli.

## Kaydetme ve yayınlama (public link)

1. Dosyayı `powerbi/Talay_AIS_Dashboard.pbix` olarak kaydet (repo klasöründe `powerbi/` klasörü oluştur).
2. Giriş → **Yayımla** → *Çalışma alanım* (My workspace).
3. Tarayıcıda app.powerbi.com → raporu aç → **Dosya → Ekle (Embed) → Web'de yayımla (genel)**.
   Çıkan bağlantı, proje formundaki "public dashboard link".
   - "Web'de yayımla" seçeneği kapalıysa, okul hesabında yönetici izni yok demektir. Bu durumda Volkan Hoca'ya ya da
     okulun IT birimine sor. Olmazsa yedek plan olarak raporu PDF'e aktar (Dosya → PDF'e aktar), `.pbix`
     dosyasını GitHub'a koy ve ekran görüntüleriyle sun.
   - Web'de yayımlanan rapor herkese açık olur. Bizim verimiz zaten açık DMA verisi olduğu için sorun yok.

## Veri güncellenirse

Python tarafında yeni gün eklersen ya da bir eşiği değiştirirsen:

```bash
python -m src.big_data.encounters
python -m src.big_data.compare_talay
python -m src.big_data.powerbi_export
```

Ardından Power BI'da **Giriş → Yenile** yap. Görseller korunur, sadece sayılar güncellenir.
