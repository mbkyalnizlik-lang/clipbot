# API Kurulumu — adım adım

Üç platform da kodda hazır:

| Platform | Komut | Denetim gerekir mi |
|---|---|---|
| YouTube | `auth-youtube` → `upload --platform youtube` | Hayır |
| TikTok | `auth-tiktok` → `upload --platform tiktok` | Public için **evet** (denetimsizde SELF_ONLY / inbox taslak) |
| Instagram | `auth-instagram` → `upload --platform instagram` | Hayır (Creator/Business hesap + Meta uygulaması yeter) |

Toplam süre: **YouTube ~10 dakika**, TikTok ~10, Instagram ~15. Her biri bir kez yapılır.

`python clipbot.py doctor` hangi platformun hazır olduğunu tek satırda söyler.

---

## 1) YouTube — otomatik yükleme (yapılması gereken tek şey)

### 1.1 Google Cloud projesi

1. https://console.cloud.google.com → üstteki proje seçici → **New project**.
2. Ad: `clipbot` (önemli değil) → **Create**. Proje oluşunca üstte seçili olduğundan emin ol.

### 1.2 YouTube Data API v3'ü aç

1. Sol menü → **APIs & Services** → **Library**.
2. Arama: `YouTube Data API v3` → aç → **Enable**.
3. (Kota sayfası: APIs & Services → **Quotas**. Varsayılan olarak günde **100 yükleme**
   hakkı var; bu ayrı bir kovada, okuma çağrılarıyla paylaşılmıyor. Reset: Pasifik saati 00:00.)

### 1.3 OAuth izin ekranı

1. **APIs & Services** → **OAuth consent screen**.
2. User type: **External** → Create.
3. App name: `clipbot`, support e-mail: kendi mailin. Diğer alanlar boş kalabilir → Save.
4. **Audience** (eski adı Test users) → **Add users** → kendi Gmail adresini ekle.
5. **Publishing status**'u **In production** yap (Publish app).
   - **Doğrulama (verification) yapmana gerek yok.** Google'ın dokümanında "kişisel kullanım"
     doğrulama istisnası olarak sayılıyor; uygulama doğrulanmamış kalsa da çalışır, sadece izin
     ekranında "doğrulanmadı" uyarısı görünür.
   - Ama yeni konsolda **Publish app butonu Branding tamamlanmadan kilitli**: ana sayfa URL'i,
     gizlilik politikası URL'i ve **sahibi olduğun** bir domain istiyor (Search Console ile domain
     doğrulaması). Google Sites / `sites.google.com` adresi senin domainin olmadığı için
     doğrulanamaz — o yolu deneme.
   - Publish etmezsen (Testing'de kalırsan) her şey çalışır, ama refresh token **7 günde bir**
     düşer ve `auth-youtube` tekrar gerekir. Gerçek domain + 2 sayfalık site (ana sayfa + gizlilik)
     kurup Search Console'dan doğrularsan Publish açılır ve token kalıcı olur.
   - Kontrolü: `youtube_token.json` içinde **`refresh_token_expires_in` alanı varsa** uygulama
     hâlâ Testing'de; **yoksa** token süresiz.
6. **Data access** (eski adı **Scopes**) → **Add or remove scopes** →
   `https://www.googleapis.com/auth/youtube.upload` ekle → **Update** → **Save**.
   - Yeni konsolda izin ekranı menüsü **Google Auth platform → Data access** altındadır;
     client oluştururken scope sorulmaz, ayrı sayfada tanımlanır.
   - Kod da sadece bu scope'u istiyor (`clipbot.py` → `YT_SCOPE`); fazlasını ekleme, yükleme
     için gerekli olan tek şey bu.

> İzin ekranında "Google bu uygulamayı doğrulamadı" uyarısı çıkar: **Advanced → Go to clipbot
> (unsafe)** → devam. Kendi uygulaman, kendi hesabın; normal.

### 1.4 OAuth client (Desktop)

1. **APIs & Services** → **Credentials** → **Create credentials** → **OAuth client ID**.
2. Application type: **Desktop app**.
3. **"Use this client for an AI-powered agent" / "Bu client'ı yapay zekâ ajanı için kullan"
   kutusunu işaretleme.** Bu sadece bir beyandır, ek scope/izin vermez; Google Workspace'in
   barındırdığı MCP sunucuları 11 Kasım 2026'dan sonra işaretsiz client'ları reddediyor, ama
   biz YouTube Data API'yi doğrudan çağırıyoruz (MCP yok) → gerekmiyor. Ayrıca işaretlersen
   client "agentic" sayılır ve sonradan **değiştirilemez** (yeni client açman gerekir).
4. Create → **Download JSON**.
5. İndirilen dosyayı proje kök klasörüne `client_secret.json` adıyla koy
   (`config.toml` → `[upload] client = "client_secret.json"` bunu bekliyor).

### 1.5 Yetkilendir

```bash
python clipbot.py auth-youtube
```

- Tarayıcı açılır → hesabını seç → izin ver.
- Dönüş adresi `http://127.0.0.1:8765` (kod içinde sabit, `[upload] oauth_port` ile değişir).
- Başarılıysa `youtube_token.json` oluşur ve bir daha tarayıcı açılmaz.
- Tarayıcı açılmazsa konsoldaki URL'i elle açabilirsin.

### 1.6 Test et (hiçbir şey yüklemez)

```bash
python clipbot.py upload --platform youtube --dry-run
```

Kuyrukta yüklenmemiş klipler için ne yükleyeceğini, başlık/açıklama/etiketleri yazar.

### 1.7 İlk gerçek yükleme (gizli)

`config.toml`:

```toml
[upload]
enabled = true
privacy = "unlisted"
platforms = "youtube"      # önce tek platformda test et
```

```bash
python clipbot.py upload --limit 1
```

YouTube Studio'da videoyu gör, beğenirsen `privacy = "public"` yap.

### 1.8 Yayın sırasında tam otomatik

```bash
python clipbot.py auto --channel xrohat --upload
```

Kayıt sürerken ses analizi yapılır, patlama bulununca klip kesilir, altyazı+kapak hazırlanır ve
`[upload] platforms` listesindeki platformlara yüklenir. Yayın bitmeden video kanalda görünür.

### Sık çıkan hatalar

| Hata (HTTP / reason) | Sebep | Çözüm |
|---|---|---|
| `403 accessNotConfigured` | API açılmamış | 1.2'yi yap |
| `403 youtubeSignupRequired` | Kanal yok ya da kanal adı onaylanmamış | youtube.com'da bir kez içerik/kanal kurulumu yap |
| `403 quotaExceeded` | Günlük 100 yükleme bitti | Pasifik 00:00'i bekle |
| `400 invalid_grant` | Token düştü (test modu) | `auth-youtube` tekrar + 1.3/5'i yap |
| Link açılıyor ama "yetki verilmedi" / `state uyusmadi` | 8765 portunu eski bir `auth-youtube` süreci tutuyor | `netstat -ano \| grep 8765` → `taskkill /F /PID <pid>` → `auth-youtube` tekrar |
| "5 dakika içinde yetki gelmedi" | Linki 5 dk içinde tamamlamadın | Komutu tekrar çalıştır, yeni linki hemen kullan |
| `401 invalid_client` | `client_secret.json` başka projeden | 1.4'ü o projede tekrar et |
| Yükleme biter ama video **private** kalır | Kanal doğrulanmamış | YouTube ayarları → kanal doğrulaması (telefon) |

---

## 2) TikTok — Content Posting API

Kod hazır: `auth-tiktok`, `upload --platform tiktok`, `auto --platform tiktok`.

### 2.1 Uygulama

1. https://developers.tiktok.com → giriş → **Manage apps** → **Connect an app**.
2. **Products** → **Content Posting API** → ekle. Aynı sayfada **Direct Post** anahtarını aç
   (kapalıysa `/post/publish/video/init/` 403 verir).
3. **Scopes**: `video.publish` (doğrudan paylaşım) ve/veya `video.upload` (gelen kutusu taslağı).
   Kod ikisini de istiyor (`[tiktok] scope`).
4. **Login Kit** → **Redirect URI**: `http://127.0.0.1:8766/`
   (TikTok https isterse kendi alan adını yazıp `[tiktok] redirect_uri` olarak ver).
5. **client_key / client_secret** → `config.toml [tiktok]`
   (ya da `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET` ortam değişkeni — dosyada tutmak istemiyorsan bunu kullan).

### 2.2 Yetkilendir

```bash
python clipbot.py auth-tiktok
```

- Tarayıcı açılır → izin ver. Dönüş `http://127.0.0.1:8766/`.
- Tarayıcı dönmezse / https zorunluysa dönen URL'i yapıştır:

```bash
python clipbot.py auth-tiktok --code "http://127.0.0.1:8766/?code=...&state=..."
```

`tiktok_token.json` içinde `access_token`, `refresh_token`, `open_id`, `expires_at` tutulur;
kod süresi dolunca `refresh_token` ile kendisi yeniler.

### 2.3 Denetim gerçeği (önemli)

- **Denetlenmemiş uygulama:** yalnızca `SELF_ONLY` paylaşım; hesap **private** olmak zorunda;
  24 saatte en fazla 5 kullanıcı. Public'e zorlarsan
  `403 unaudited_client_can_only_post_to_private_accounts`.
- **Denetim geçince:** public açılır; hesap başına 24 saatte ~15 gönderi (tüm uygulamalarla ortak).
- Denetim şansını yükseltmek için: kullanıcı gizlilik seçimini kendi yapmalı (ekranda seçim),
  gizlilik politikası + kullanım şartları sayfası ve akışın videosu isteniyor. TikTok, "başka
  platformlardan içerik kopyalayan" ve "sadece kendi hesabını yöneten" araçlara sıcak bakmıyor.

Bu yüzden iki mod var (`config.toml [tiktok] mode`):

| mode | Ne yapar | Uç nokta / scope | Denetim gerekir mi |
|---|---|---|---|
| `direct` | Doğrudan profiline paylaşır | `/post/publish/video/init/` — `video.publish` | Public için evet; denetimsizde SELF_ONLY |
| `inbox` | Videoyu TikTok gelen kutusuna **taslak** bırakır | `/post/publish/inbox/video/init/` — `video.upload` | Hayır |

`inbox` modunda klip, TikTok uygulamasında gelen kutusu bildirimi olarak görünür; başlık hazır
gelir, sen sadece paylaş dersin. Denetimsizken en pratik yol budur.

### 2.4 Test

```bash
python clipbot.py upload --platform tiktok --dry-run    # hiçbir şey göndermez
python clipbot.py upload --platform tiktok --limit 1    # tek klip
```

Denetimsizken `config.toml`:

```toml
[tiktok]
privacy = "SELF_ONLY"     # ya da
mode = "inbox"
```

### 2.5 Sık çıkan hatalar

| Hata | Sebep | Çözüm |
|---|---|---|
| `403 unaudited_client_can_only_post_to_private_accounts` | Uygulama denetlenmemiş + hesap public | Hesabı private yap + `privacy = "SELF_ONLY"`, ya da `mode = "inbox"` |
| `scope_not_authorized` / `access_token_invalid` | `video.publish` onaylı değil ya da token eski | Uygulamada scope'u aç, `auth-tiktok` tekrar |
| `spam_risk_too_many_posts` | 24 saatte ~15 gönderi sınırı | Bekle; günde 3–5 klip |
| `video_format_invalid` / süre hatası | 9:16, ≤10 dk, H.264 + AAC bekliyor | `clipbot` zaten 1080x1920/30fps üretiyor |
| init'te `401` | Redirect URI uyuşmuyor | Login Kit'teki URI ile `[tiktok] redirect_uri` aynı olmalı |
| `file_format_check_failed` | Kayıtta bozuk segment / çok büyük dosya | Yeniden kes; tek parça yükleme sınırı 64 MB |

---

## 3) Instagram Reels — Instagram API (Instagram Login)

Kod hazır: `auth-instagram`, `upload --platform instagram`, `auto --platform instagram`.

### 3.1 Hesap + uygulama

1. Instagram → Ayarlar → **Hesap türü** → **Creator** (Business de olur). Kişisel hesap API ile paylaşamaz.
2. https://developers.facebook.com → **Create app** → type: **Business**.
3. **Add product** → **Instagram** → **API setup with Instagram login**.
4. İzinler: `instagram_business_basic`, `instagram_business_content_publish` (kod bunları istiyor).
5. **OAuth Redirect URI**: Meta https istiyor. Kendi alan adın varsa
   `https://alanadin/callback` yaz ve `[instagram] redirect_uri` olarak ver. Yoksa tarayıcıdaki
   dönüş URL'sini `--code` ile yapıştır (3.2).
6. **app_id / app_secret** → `config.toml [instagram]`
   (ya da `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET`).

### 3.2 Yetkilendir

```bash
python clipbot.py auth-instagram
# tarayıcı dönmezse:
python clipbot.py auth-instagram --code "https://alanadin/callback?code=...&state=..."
```

- Kod önce kısa ömürlü token'ı alır, sonra `ig_exchange_token` ile **60 günlük** uzun ömürlü
  token'a çevirir ve `instagram_token.json` içine `user_id` + `expires_at` ile yazar.
- Süresi dolmak üzereyken `refresh_access_token` ile otomatik yenilenir.

### 3.3 Yayınlama akışı (kod bunu yapıyor)

1. `POST /{ig-user-id}/media` → `media_type=REELS`, `caption`, `share_to_feed`.
   Varsayılan **resumable** (`upload_type=resumable`): dosya doğrudan Meta'ya yüklenir,
   **public URL gerekmez**.
2. `POST https://rupload.facebook.com/ig-api-upload/<ver>/<container-id>`
   (`Authorization: OAuth <token>`, `offset: 0`, `file_size`).
3. `GET /<container-id>?fields=status_code` → `FINISHED` olana kadar bekle (en fazla 10 dk).
4. `POST /{ig-user-id}/media_publish?creation_id=...` → `media_publish`, sonra `permalink` okunur.

Public URL'in hazırsa `[instagram] public_url_template` doldur, kod `video_url` yolunu kullanır:

```toml
[instagram]
public_url_template = "https://cdn.ornek.com/{name}/{file}"
```

Sınır: hesap başına 24 saatte 100 API gönderisi (bazı hesaplarda 50); Reels 3 sn–15 dk, ≤300 MB, 9:16.
Not: public URL **o anda** erişilebilir olmalı (auth'lu / süresi geçmiş link kabul edilmez).

### 3.4 Test

```bash
python clipbot.py upload --platform instagram --dry-run
python clipbot.py upload --platform instagram --limit 1
```

### 3.5 Sık çıkan hatalar

| Hata | Sebep | Çözüm |
|---|---|---|
| `(#10) Application does not have permission for this action` | `instagram_business_content_publish` onaylı değil | 3.1/4'ü yap, `auth-instagram` tekrar |
| `(#100) Invalid parameter` (`video_url`) | URL erişilemiyor ya da medya tipi yanlış | `media_type=REELS`; public URL'de auth/expiry olmasın |
| `Media not found or unavailable` (publish) | Container henüz `FINISHED` değil | Kod bekliyor; elle testte `status_code` kontrol et |
| `(#4) Application request limit reached` | 24 saat kotası | Bekle |
| `OAuthException: redirect_uri mismatch` | Redirect URI uygulamada tanımlı değil | Aynı URI'yi uygulama ayarına ekle |
| Reels 90 saniyede kesiliyor | Hesaba göre süre sınırı | `detect.clip_seconds` 30–60 aralığında tut |
| `rupload` 401 | Token `OAuth` başlığıyla gitmeli (Bearer değil) | Kod doğru gönderiyor; elle testte `Authorization: OAuth <token>` kullan |

---

## Ortak: hangi platform, hangi komut

```bash
python clipbot.py doctor                       # ne hazır, ne eksik

python clipbot.py upload --dry-run             # config'teki tüm platformlar için plan
python clipbot.py upload --platform tiktok --limit 1
python clipbot.py upload --platform all --dry-run

python clipbot.py auto --channel xrohat --upload --platform youtube,tiktok
```

- Hazır olmayan platform (kimlik/token yok) **atlanır**, diğerleri yüklemeye devam eder:
  çıktıda `[atlandi:tiktok] ...` satırı görünür.
- `clips/queue.csv` her platformu ayrı takip eder: `status` (YouTube), `tiktok`,
  `instagram` + `tiktok_url`, `instagram_url`. Aynı klip ikinci kez yüklenmez.
- Sırlar: `client_secret.json`, `youtube_token.json`, `tiktok_token.json`,
  `instagram_token.json` `.gitignore` içinde. Repoya/public klasöre koyma.
