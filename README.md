# clipbot — canlı yayından klip üretimi (GTA 6 için)

Bir yayıncının canlı yayınını kaydeder, **chat hızı + ses seviyesi patlamalarını** bulur, o anları
**9:16 dikey klip** olarak keser, altyazı basar ve yüklemeye hazır paket (video + kapak + başlık/etiket) üretir.

```
auto   ->  kayıt + patlama + render + YouTube yükleme (tek komut, yayın devam ederken)
watch  ->  detect  ->  cut  ->  queue / mark   (elle kontrol etmek istersen)
```

Hiçbir ücretli servis şart değil. Altyazı için Groq'un ücretsiz Whisper kotası (veya yerel faster-whisper).

---

## Kurulum (bu makinede zaten kurulu)

```bash
python clipbot.py doctor          # her şeyi kontrol eder
```

Eksik varsa:

```bash
python -m pip install imageio-ffmpeg streamlink yt-dlp
export GROQ_API_KEY="gsk_..."     # altyazı için (ücretsiz kota). Windows: setx GROQ_API_KEY "..."
```

`faster-whisper` Python 3.15'te derlenmiyor (`av` wheel'i yok) — bu yüzden altyazı **Groq** üzerinden
çalışıyor. Anahtar yoksa klip yine üretilir, sadece altyazı olmaz.

---

## GTA 6 çıkış günü planı (19 Kasım 2026)

Aynı anda 10 yayıncı kaydetmeye çalışmak bant genişliğini ve diskini bitirir. Sıralı çalış:

**18 Kasım (bugün/yarın) — prova**
1. `streams.toml`'a 3–5 yayıncı yaz (Twitch ağırlıklı; chat'i sadece Twitch'te okuyoruz).
2. Kısa bir test kaydı: `python clipbot.py watch --url https://www.twitch.tv/KANAL --max-hours 0.2`
3. `detect` → `cut` zincirini çalıştır, çıkan klibi telefondan izle.
4. Altyazı fontu/boyutu ve `video.mode` ayarını beğenene kadar değiştir.

**19 Kasım — yayın günü**
```bash
# tek komut: yayını izler, patlamayı bulur, keser, YouTube'a yükler
python clipbot.py auto --channel xrohat --upload
```
Yayın devam ederken her ~20 saniyede yeni bölge analiz edilir; patlama bulununca klip kesilip
yüklenir, kayıt kesintisiz sürer. Ctrl+C ile durur.

Elle kontrol istersen eski zincir de duruyor:
```bash
python clipbot.py watch --url https://www.twitch.tv/KANAL --note "GTA6 story"
python clipbot.py detect --session sessions/KANAL-20261119-XXXXXX
python clipbot.py cut --session sessions/KANAL-20261119-XXXXXX
python clipbot.py queue
```

Gün içinde: 1 saat kayıt → 10 dakikada klip çıkarma → yükleme. Aynı anda 2 yayıncıdan fazlasına
çıkmak istiyorsan ikinci bir bilgisayar/disk gerekir (1080p60 ≈ 4–6 GB/saat).

---

## Komutlar

| Komut | Ne yapar |
|---|---|
| `watch --url ...` | Yayını `sessions/<kanal>-<tarih>/video.ts` olarak kaydeder, `events.csv`'ye 5 sn'de bir chat/ses yazar. `--chat KANAL`, `--no-chat`. |
| `watch --channel xrohat --seconds 90` | `streams.toml`'daki isimle kayıt + deneme için saniye sınırı. |
| `channels` | `streams.toml` listesi. |
| `detect --session ...` | Chat z-skoru ≥ 3 veya ses tabandan +6 dB ise aday üretir → `candidates.json`. `--top 3` ile sınırla. |
| `cut --session ...` | Adayları `clips/<oturum>/<no>_<saniye>s/clip.mp4` olarak render eder. `--top`, `--mode`, `--no-subs`, `--title`. |
| `cut --video dosya.mp4 --at 754 --dur 40` | Kaydı elle kes (en çok kullanacağın yol). Manuel modda `--mode original` uzun video verir. |
| `auto --channel xrohat --upload` | **Ana komut.** Kayıt + canlı analiz + kesim + YouTube yükleme. `--boost 5` hassasiyet (dB), `--every 20` analiz aralığı, `--seconds 600` deneme süresi, `--dry-run` yükleme provası. |
| `upload` | Kuyrukta bekleyen klipleri platformlara yükler. `--platform youtube\|tiktok\|instagram\|all`, `--clip yol`, `--limit 2`, `--privacy unlisted`, `--dry-run`. |
| `auth-youtube` | Tek seferlik YouTube yetkilendirmesi (tarayıcı açılır, token dosyaya yazılır). |
| `auth-tiktok` | Tek seferlik TikTok yetkilendirmesi (`video.publish` + `video.upload`). `--code` ile elle yapıştırma. |
| `auth-instagram` | Tek seferlik Instagram (Creator/Business) yetkilendirmesi; 60 günlük token otomatik yenilenir. |
| `queue` / `mark 3 posted` | Yükleme takibi (`clips/queue.csv`). |
| `doctor` | ffmpeg / libass / streamlink / yt-dlp / Groq anahtarı / disk kontrolü. |

### Ayarlar — en çok değiştireceklerin

| `config.toml` | Ne yapar |
|---|---|
| `video.mode` | `stack` (bu makinede varsayılan) / `crop_center` / `crop_left` / `crop_right` / `blurpad` / `original` |
| `facecam.enabled` | Yayıncının webcam'ini kayıtta bulup klibin köşesine küçük pencere (PIP) olarak basar; `false` = sadece oyun |
| `facecam.position` / `size` | PIP köşesi (`auto` = sağ üst) ve genişliği (0.34 = 1080'de ~367 px) |
| `subs.margin_v` | Altyazı yüksekliği; büyük = daha yukarı (1920 px içinde alt kenardan) |
| `subs.size` | Altyazı boyutu (84 iyi; 96 = daha saldırgan) |
| `audio.target_lufs` | Ses hedefi (şu an -12.5 → klipler ~-13.6 LUFS; YouTube -14'e normalize eder) |
| `audio.limiter` | Final limiter; 0.79 = tepe -2 dB, AAC sonrası kırpılma olmuyor |
| `sfx.enabled` | Klip başına **boom** + sonuna **whoosh** ekle (dosyalar `assets/sfx/` ilk render'da üretilir) |
| `sfx.boom_volume` / `whoosh_volume` | Efekt seviyesi (boom 0.8, whoosh 0.6). 1.0 = en sert |
| `auto.boost_db` | Patlama eşiği; **düşür = daha çok klip**, yükselt = sadece büyük anlar |
| `auto.min_lufs` | Bundan kısık ses patlama sayılmaz (sessizliğin bitişini klip yapmasın) |
| `auto.warmup_seconds` | İlk kaç saniye sadece taban ölçümü (erken kesmesin) |
| `auto.require_chat` | Twitch'te chat patlaması olmayan ses olaylarını atlar (menü/oyun geçişi kliplenmesin) |
| `auto.chat_min` / `chat_mult` | Chat patlaması eşiği: en az bu mesaj/sn ya da chat tabanının bu katı |
| `auto.chat_only` | Ses patlaması olmasa da chat'in patladığı anı klip yapar |
| `detect.min_words` | Klipte en az bu kadar kelime olmalı; konuşmasız anlar atılır (0 = kapalı) |
| `upload.privacy` | `public` / `unlisted` / `private` |
| `upload.platforms` | `youtube,tiktok,instagram` — hazır olmayan platform atlanır |
| `tiktok.mode` | `direct` (profiline paylaş) / `inbox` (taslak gönder) |
| `tiktok.privacy` | `PUBLIC_TO_EVERYONE` / `SELF_ONLY` (denetimsizde mecburi) |
| `instagram.public_url_template` | Boşsa resumable upload; doluysa `video_url` yolu |
| `caption.emoji` | Başlığın başına emoji (`""` = yok) |

### Çıktı yapısı
```
clips/<oturum>/01_01234s/
  clip.mp4       1080x1920, H.264 + AAC, yükleme için hazır
  captions.srt   altyazı (Groq/whisper varsa)
  cover.jpg      kapak
  caption.txt    başlık + hashtag (kopyala-yapıştır)
  meta.json      kaynak, saniye, skor
```

### Webcam (kamera) penceresi
`crop_center` kullanınca oyunun ortası kesildiği için webcam çoğu zaman klibin dışında kalıyordu.
Artık kayıttan eşit aralıklı 18 kare alınıp yüz aranıyor; **aynı köşede birkaç ayrı karede tekrar eden**
yüz webcam sayılıyor (oyunun içindeki yüzler tek karede çıkar, elenir). Pencere `sessions/<oturum>/facecam.json`
 içine bir kez yazılır, sonra her klip 9:16'ya kırpılırken o bölge sağ üste küçük pencere olarak basılır
(beyaz çerçeveli, `[facecam]` bölümüyle ayarlanır — `position`, `size`, `border`).

- Kamera kapalıysa ya da yüz bulunamazsa klip eskisi gibi sadece oyun olur (hata vermez).
- O kesimde istemezsen: `python clipbot.py cut ... --no-facecam`.
- `opencv` yoksa `doctor` "facecam: yok" der ve PIP devre dışı kalır; kurulumu:
  `python -m pip install opencv-python-headless numpy`.

### Modlar
- `stack` (bu makinedeki varsayılan): oyun üstte tam genişlikte panel, altında webcam kartı, en altta
  altyazı. Yayının kendi boş alanları `video.trim_*` ile kırpılır; kamera panelin dışında kalsa da
  kart olarak eklenir.
- `crop_center` (kod varsayılanı): tam ekran 9:16, Valorant Shorts görünümü. Elle bir şey yapmıyorsun,
  tüm klipler bu modda çıkıyor. Facecam sağ kenarda kalıyorsa `crop_right`, soldaysa `crop_left`.
- `blurpad`: oyun 16:9 ortada, üst/alt bulanık dolgu — hiçbir şey kaybolmaz ama daha "sakin" durur.
- `original`: 1920x1080 uzun video (YouTube uzun formatı, derleme videoları).

---

## YouTube'a otomatik yükleme — tek seferlik kurulum

2 Kasım 2026'dan itibaren YouTube, **doğrulanmamış** API projelerinin yüklemelerini de public yapıyor
(2020'deki "private'a kilitlenir" kuralı kalktı). Varsayılan kota: günde 100 `videos.insert` — bir
yayın akışı için fazlasıyla yeterli.

1. https://console.cloud.google.com → yeni proje.
2. APIs & Services → Library → **YouTube Data API v3** → Enable.
3. OAuth consent screen → External → kendini **test user** ekle. (Uygulama "doğrulanmadı" uyarısı
   verir; kendi hesabın için Advanced → Continue de, sorun değil.)
4. Credentials → Create credentials → OAuth client ID → **Desktop app** → JSON indir,
   dosyayı `client_secret.json` adıyla proje klasörüne koy.
5. `python clipbot.py auth-youtube` → tarayıcı açılır, izin ver → `youtube_token.json` yazılır.
6. Test: `python clipbot.py upload --dry-run` (hiçbir şey yüklemez, sadece planı yazar).

Ayrıntılı sürüm + hata tablosu: [docs/API-KURULUM.md](docs/API-KURULUM.md).

Sonrası: `python clipbot.py auto --channel xrohat --upload` → yayın devam ederken klipler YouTube'a düşer.
`config.toml` → `[upload] privacy = "unlisted"` ile önce gizli test edebilirsin.

Dikkat: "test user" modundaki uygulamalarda token 7 günde bir düşebilir; kalıcı kullanım için
OAuth consent screen'i **In production** durumuna al (kendi hesabın için doğrulama şart değil).

## Yükleme (üç platform da API ile bağlı)

Her klip klasöründe `caption.txt` hazır; `upload` bunu başlık/açıklama/etiket olarak kullanır.

```bash
python clipbot.py doctor                        # hangi platform hazır
python clipbot.py upload --dry-run              # config'teki platformlar için plan
python clipbot.py upload --platform tiktok --limit 1
python clipbot.py auto --channel xrohat --upload --platform youtube,tiktok
```

Gerçekler:
- **YouTube**: Data API v3, kendi kanalın için OAuth yeterli; günde **100 yükleme**.
- **TikTok**: Content Posting API. Denetlenmemiş uygulamada gönderi `SELF_ONLY` ve hesap private
  olmak zorunda; public için audit lazım. Audit yoksa `[tiktok] mode = "inbox"` ile klibi TikTok
  gelen kutusuna **taslak** bırakabilirsin (başlık hazır, tek dokunuşla paylaşırsın).
- **Instagram**: Instagram API (Instagram Login) + **Creator/Business** hesap. Reels resumable
  upload ile yüklenir, **public URL gerekmez** (istersen `public_url_template`).

Kota / sınır: TikTok hesap başına 24 saatte ~15 gönderi, Instagram ~100 API gönderisi.

Adım adım kurulum (ekran ekran, hata tabloları): **[docs/API-KURULUM.md](docs/API-KURULUM.md)**

Aynı klibi 24 saat arayla tekrar yüklemek TikTok/Instagram'da spam riski; sıra: Shorts → TikTok → Reels.

---

## Neler otomatik, neler değil

| İş | Durum |
|---|---|
| Yayını kaydetme, patlama bulma, kırpma, altyazı, ses, kapak | Otomatik (`auto`) |
| YouTube Shorts yükleme | Otomatik (`auto --upload`, OAuth kurulumu bir kez) |
| TikTok yükleme | API ile (`--platform tiktok`). Public paylaşım uygulama denetimi ister; denetimsizde SELF_ONLY ya da `mode = "inbox"` taslağı |
| Instagram Reels | API ile (`--platform instagram`); Creator/Business hesap + Meta uygulaması yeter |
| Ekran kırpması (crop) | Otomatik: `video.mode = crop_center` |
| Emoji (videonun içine gömülü) | Yok — libass renkli emoji çizemiyor. Emoji sadece YouTube başlığında |
| Ses efekti (boom/whoosh) | Var (`[sfx]`, ilk render'da üretilir) |
| Webcam'i bulma + klibe küçük pencere | Otomatik (`[facecam]`, opencv ile; kamera kapalıysa atlanır) |

## Sınırlar / bilinçli tercihler

- **Telif**: yayıncının izni ya da affiliate/partner anlaşması olmadan klipleri yüklemek telif ihlali ve
  hesap kapanması riski. Kanal açıklamasında kaynak + etiketleme yap, mümkünse yayıncıdan izin al.
- **Chat sadece Twitch'te** okunuyor (anonim IRC, token yok). Kick/YouTube'da tetikleyici ses seviyesi.
- **Senkron**: kayıt dosyası, ffmpeg yayına bağlandıktan ~1–2 sn sonra başlar; 5 dk'dan uzun kayıtlarda
  kayma ihmal edilebilir. Kısa kayıtta klip birkaç saniye kayabilir — `detect.pre_roll` ile telafi et.
- **Altyazı yakma** libass ile yapılıyor (bu makinede var). Yoksa `.srt` yine yazılır, video altyazısız çıkar.
- Uzun süreli kayıtta `video.ts` büyür; `--max-hours` güvenlik sınırı var. Disk: 1080p60 ≈ 4–6 GB/saat.

## Sorun giderme

| Belirti | Çözüm |
|---|---|
| "yayin URL'si cozulemedi" | `streamlink.exe` / `yt-dlp.exe` PATH'te olmayabilir; `doctor` çıktısına bak (pip Scripts dizinini kendi bulur). |
| "aday yok" | `config.toml` → `detect.z_threshold = 2.0`; ya da klibi elle kes: `cut --video ... --at ...`. |
| Chat logu boş | Kanal adı yanlış ya da chat kapalı; `--chat KANAL` ile ver. |
| Altyazı yok | `GROQ_API_KEY` yok. Ücretsiz anahtar: console.groq.com |
| Klip kaymış | `detect.pre_roll` değerini 6–8'e çıkar. |

## Yükleme: elle mi, API mi?

Klipler `clips/<tarih>/` altında `clip.mp4` + `caption.txt` (başlık/hashtag hazır) olarak çıkar.
Üç platforma aynı dosyayı yükleyebilirsin; her platforma **kendi uygulamasından** yükle (TikTok'un
damgalı videosunu Reels'e atma, erişim düşer).

| Platform | API durumu | Pratik yol |
|---|---|---|
| YouTube | **Otomatik.** `upload` / `auto --upload` (yukarıdaki kurulum). Kota: günde 100 yükleme. | Elle bir şey yapma; `caption.txt` başlık/açıklama olarak gider. |
| TikTok | Content Posting API var; uygulama denetimi yoksa video sadece taslak/özel kalır. | Elle yükle (tiktok.com/upload). 9:16, ≤10 dk. |
| Instagram | Graph API `instagram_content_publish` = Business/Creator hesap + Facebook sayfası + uygulama denetimi. | Elle yükle (Reels). ≤90 sn, 9:16. |

Özet: YouTube otomatik; TikTok ve Instagram için 2 ayrı geliştirici hesabı + uygulama denetimi gerekiyor.
O yüzden onlar `clips/queue.csv`'de `pending` kalır, `caption.txt` ile elle yüklersin.
Takip: `python clipbot.py queue` / `mark <#> posted`.
