# Trend Radarı (Türkiye)

Her sabah Türkiye'de TikTok'ta neyin yükseldiğini ölçer ve sonuçları
`rapor.md` + Google Sheets'e yazar. Maliyeti ₺0'dır, bu bilgisayarda çalışır.

TikTok, Explore akışını isteğin geldiği IP'nin ülkesine göre gösterir. Bu
yüzden radar GitHub sunucularında değil, bu bilgisayarda (TR IP) çalışır.
GitHub sadece sonuçları barındırır.

## Ne ölçer

| Kaynak | Ne verir |
|---|---|
| TikTok Explore (Tümü + kategori akışları, girişsiz) | Türkiye'de dolaşan videolar; nişlere ayrılır, sesler sayılır |
| TikTok hashtag sayfaları | İzlenen etiketlerin toplam video/izlenme sayısı → günlük hız, ivme, evre |
| Otomatik keşif | Nişte bir günde 2+ videoda geçen yeni etiketler takibe alınır |
| TikTok Creative Center (TR) | Resmi trend hashtag'ler (girişsiz ilk 3) |
| Google Trends TR (RSS) | Günün gündem aramaları — içerik konusu fikirleri |

**Evreler** (en az 3 günlük veriden sonra anlamlıdır):

- 🔥 **erken yükseliş**: büyüme hızlanıyor ve etiket hâlâ küçük (<20 bin video). En değerli pencere.
- 📈 **hızlanıyor**: büyüme hızlanıyor, etiket büyük.
- **zirvede**: büyüyor ama hızlanmıyor.
- **yavaşlıyor / düşüşte**: geç kalındı.

## Çıktılar

- `rapor.md`: günlük özet (Claude senaryo üretirken bunu okur)
- `data/sheet/hashtagler.csv`, `sesler.csv`, `gundem.csv`, `creative_center.csv`: Google Sheets tabloları
- `data/history/`: günlük anlık görüntüler (hız/ivme hesabının hafızası, silme)

## Kurulum

1. **Elle bir çalıştırma:** `powershell -File run.ps1`. Sonuç `logs\son_calisma.log` ve `rapor.md` içinde.
2. **GitHub'a bağlama** (Sheets için gerekli): `gh auth login` ile oturum aç, sonra
   `gh repo create trend-radar --public --source . --push`. Repo herkese açık
   olmalı ki Sheets okuyabilsin; içinde sadece trend verisi var, kişisel bilgi yok.
3. **Google Sheets:** yeni bir tablo aç, her sekmenin A1 hücresine
   (Türkçe tablo ayarlarında argüman ayırıcı `;`):
   ```
   =IMPORTDATA("https://raw.githubusercontent.com/KULLANICI/trend-radar/main/data/sheet/hashtagler.csv"; ","; "en_US")
   ```
   `hashtagler.csv` yerine `sesler.csv`, `gundem.csv`, `creative_center.csv` ile diğer sekmeleri doldur.
   `"en_US"` parametresi ondalık sayıların doğru okunması için gerekli.
   Sheets veriyi yaklaşık saatte bir yeniler.
4. **Günlük otomatik çalışma:** Windows Görev Zamanlayıcı'da her sabah `run.ps1`
   çalıştıran bir görev. Bilgisayar o saatte kapalıysa açılınca çalışır.

## Nişleri değiştirme

`config.json` içindeki `niches`:

- `categories`: Explore kategori butonlarının **Türkçe** adları (ör. "Güzellik Bakımı", "Kıyafet", "İlişki").
  O akıştaki tüm videolar nişe girer.
- `tags` / `keywords`: diğer akışlardan nişe çekilecek videolar (etiket tam eşleşme, kelime açıklamada).
  Türkçe karakterli/karaktersiz yazımlar aynı sayılır ("kızkıza" = "kizkiza").
- `watch`: her gün istatistiği tutulan sabit etiketler.

`ignoreTags`: keşifte yok sayılacak genel etiketler (fyp, keşfet, viral...).

## Dürüst notlar

- TikTok'u otomatik taramak kullanım şartlarına aykırıdır. Radar bunu girişsiz,
  günde bir kez ve düşük hacimle yapar; hiçbir hesap kullanılmaz. Olası risk bu IP'nin geçici engellenmesidir.
- TikTok sayfa yapısını değiştirirse radar hata verebilir (`logs\son_calisma.log`). Geçmiş veri kaybolmaz.
- tokchart.com'un şartları otomatik çekmeyi yasakladığı için o site sadece raporda elle bakılacak link olarak yer alır.
- Girişsiz modda hashtag altındaki video listeleri görünmez; ses sinyali kategori akışlarından gelir.
