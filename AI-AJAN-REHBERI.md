# Opus 5.5 + Higgsfield gibi çalışan AI ajanları — açık kaynak & bedava liste

> Videoda gördüğün şey: Claude Opus 5.5, MCP (`mcp.higgsfield.ai/mcp`) üzerinden Higgsfield'a bağlanıp
> DaVinci Resolve / After Effects benzeri bir timeline'da tek tek konuşarak klip üretiyor, kurgu yapıyor,
> render alıyor ve düzenlenebilir proje (.aep) bırakıyor. Yani "model" tek başına video kesmiyor;
> **kod/terminal ajanı + medya üreten MCP sunucuları + render motoru** birlikte çalışıyor.
> Aşağıdaki liste tam olarak bu üç katmanı bedava kurmanın yolu.

---

## 1. Ajan beyni (hepsinin motoru — bunu bir kez kur)

| Proje | Ne işe yarar | Lisans | Kurulum |
|---|---|---|---|
| [OpenCode](https://github.com/sst/opencode) | Terminal ajanı, Claude Code'un açık kaynak karşılığı, 60+ sağlayıcı + lokal model | MIT | README'deki tek satır installer |
| [Cline](https://github.com/cline/cline) | VS Code içinde otonom ajan, her adımda onay ister | Apache-2.0 | VS Code eklentisi |
| [Kilo Code](https://github.com/Kilo-Org/kilocode) | Cline+Continue birleşimi, VS Code/JetBrains/CLI | MIT | Eklenti / CLI |
| [OpenHands](https://github.com/All-Hands-AI/OpenHands) | Self-hosted "Devin": kod + terminal + tarayıcı | MIT | Docker |
| [Goose](https://github.com/block/goose) | MCP'yi merkeze alan ajan, 70+ eklenti, Linux Foundation | Apache-2.0 | Masaüstü app / CLI |
| [Aider](https://github.com/Aider-AI/aider) | Terminalde git-aware pair programmer | Apache-2.0 | `pip install aider-install` |

Notlar:
- **Claude Code ücretsiz değil** (Opus 5.5'i bedava kullanmanın yolu yok).
- **Gemini CLI'ın bireysel ücretsiz girişi 18 Haziran 2026'da kapandı** → yerine **Google Antigravity** (haftalık limitli $0 plan, Tab tamamen sınırsız).
- **GitHub Copilot Free**: ayda 2.000 tamamlama, ajan modu limitli. Başlangıç için en kolayı.
- Bu ajanların hepsi aynı işi yapar: dosya okur, terminal komutu çalıştırır, MCP araçlarını çağırır. Fark modeli nereden aldıkları.

---

## 2. Video: "konuş, final.mp4 geri gelsin"

| Proje | Ne yapar | Lisans |
|---|---|---|
| [browser-use/video-use](https://github.com/browser-use/video-use) | Klasöre ham görüntü at, ajana "şunu kes" de → `edit/final.mp4`. Dolgu sesleri keser, renk derecelendirir, altyazı yakar, overlay animasyon üretir. Konuşan kafa / eğitim / röportajda çok iyi. | MIT |
| **OpenMontage** (~77k★, GitHub'da "OpenMontage" olarak aranınca çıkıyor) | Uçtan uca prodüksiyon: plan → asset seçimi/üretimi → seslendirme → kompozisyon → önizleme. Remotion **ve** HyperFrames hattı; "anahtar gerekmez" yolu var. | AGPL-3.0 |
| [heygen-com/hyperframes](https://github.com/heygen-com/hyperframes) | HTML+CSS+GSAP yaz, deterministik MP4 render et. Ajanlar HTML yazmayı zaten biliyor → videoyu kod gibi üretiyor. `npx hyperframes skills update` | Apache-2.0 |
| **Kinocut** (eski adı `mcp-video`, `uvx kinocut` ile çalışır) | 200+ korumalı FFmpeg aracını MCP olarak açar; ajan doğrudan kurgu/kesme yapar | Apache-2.0 |
| [MartinDelophy/ai-video-editor](https://github.com/MartinDelophy/ai-video-editor) | Tarayıcıda çalışan, CapCut tarzı çok kanallı timeline + AI (WebGPU müzik/onarım, altyazı, avatar, offline export). Videodaki arayüzün açık kaynak versiyonu gibi. | Açık kaynak |
| [Claude Code video toolkit](https://github.com/digitalsamba/claude-code-video-toolkit) | Brief → senaryo → seslendirme → müzik → görsel → MP4; açık modellerle (~$0.01 seslendirme, ~$0.23 klip) | MIT |

Kurulum (video-use):
```bash
git clone https://github.com/browser-use/video-use
ln -s "$PWD/video-use" ~/.claude/skills/video-use   # Cline/Codex/OpenCode için de skill dizini
cd video-use && uv sync
# ffmpeg + ELEVENLABS_API_KEY gerekir
```

---

## 3. Foto & video **modeli** üretimi (Higgsfield'ın bedava karşılığı)

| Araç | Ne üretir | Lisans | Donanım |
|---|---|---|---|
| [ComfyUI](https://github.com/comfyanonymous/ComfyUI) | Her şeyin merkezi: Flux, Qwen-Image, Wan, LTX, Lip-sync düğümleri | GPL-3.0 | 12 GB+ VRAM |
| [Wan2.2](https://github.com/Wan-Video/Wan2.2) | Açık T2V/I2V, 480p–720p + CosyVoice ile speech-to-video | Apache-2.0 | 24 GB+ (5B model daha az) |
| [LTX-Video](https://github.com/Lightricks/LTX-Video) | Hızlı, hafif video modeli | Apache-2.0 (ağırlık lisansı ayrı) | 12–16 GB |
| [InfiniteTalk](https://github.com/MeiGen-AI/InfiniteTalk) | Fotoğraf + ses → sınırsız uzunlukta konuşan video, dudak senkronu | Açık | 24 GB+ |
| [Open-Sora 2.0](https://github.com/hpcaitech/Open-Sora) | Sora benzeri açık model, 256p/768p | Apache-2.0 | Çok yüksek |
| Flux.1 / Flux.2 ağırlıkları | Midjourney seviyesi fotoğraf üretimi, sınırsız self-host | Açık ağırlık | 12 GB+ |
| [SadTalker](https://github.com/OpenTalker/SadTalker) | Tek fotoğraf + ses → konuşan kafa (HeyGen/D-ID alternatifi) | Apache-2.0 | 8 GB |

⚠️ **Dikkat:** GitHub'da "Open-Higgsfield-AI / Open-Generative-AI" adıyla dolaşan onlarca fork var.
Hepsi aynı README'nin kopyası ve aslında **Muapi.ai adlı ücretli API'ye** bağlanıyor — bedava değil,
kendi modelini çalıştırmıyor. Higgsfield'ın kendi free planı da "limited use". Gerçek bedava = ComfyUI + yukarıdaki açık ağırlıklar.
HunyuanVideo ücretsiz ama lisansı AB/İngiltere/Güney Kore'yi kapsam dışı bırakıyor; Wav2Lip ticari kullanıma kapalı.

---

## 4. Kod editörü + site/app üretimi

| Proje | Ne yapar | Lisans |
|---|---|---|
| [bolt.diy](https://github.com/stackblitz-labs/bolt.diy) | Bolt/Lovable klonu; prompt → tam çalışan site, kendi API anahtarınla | MIT |
| [Dyad](https://github.com/dyad-sh/dyad) | Lokalde çalışan app builder, model seçimi serbest | Apache-2.0 (çoğu) |
| [OpenDesign](https://github.com/nexu-io/open-design) | Ajan tabanlı tasarım workspace'i: prototip, site, dashboard, sunum, motion, video | Apache-2.0 |
| [BuilderIO/agent-native](https://github.com/BuilderIO/agent-native) | Figma alternatifi: çıktı görüntü değil, gerçek HTML+Tailwind sayfası | Açık kaynak |
| [open-pencil](https://github.com/open-pencil/open-pencil) | `.fig` dosyalarını açıp yazan AI-native tasarım editörü + MCP | Açık kaynak |
| [Onlook](https://github.com/onlook-dev/onlook) | Kodunu görsel olarak düzenleyen AI-first tasarım aracı | Apache-2.0 |
| [Penpot](https://github.com/penpot/penpot) | Klasik açık kaynak Figma, self-host, design token | MPL-2.0 |
| [Open CoDesign](https://github.com/OpenCoworkAI/open-codesign) | Prompt → prototip / slayt / PDF, çalıştırılabilir kod | MIT |

---

## 5. Bedava model / API kaynakları (ajanı besleyen yakıt)

| Kaynak | Bedava olan | Limit |
|---|---|---|
| Google AI Studio (Gemini API) | Gemini 3.8 Flash, 3.5 Flash-Lite vb. | Model başına, AI Studio'da görünür; ücretsiz katman verisi eğitime gider |
| Groq | gpt-oss-120b, qwen3.8-27b, Whisper | 30 RPM / 1.000 istek/gün |
| OpenRouter `:free` | 20+ model | 20 RPM, 50 istek/gün ($10 kredi alınca 1.000/gün) |
| NVIDIA NIM | Kimi K3, DeepSeek V4.1 Flash, Nemotron | Yayınlanmamış kota, 40 RPM paylaşımlı |
| Cloudflare Workers AI | Llama 3.3 70B, gpt-oss-120b, Qwen3 | 10.000 neuron/gün |
| Z.AI (GLM) | GLM-4.7-Flash, GLM-4.5-Flash, GLM-4.6V-Flash (vision) | Limit yayınlanmamış, OpenAI uyumlu |
| Google Antigravity | Gemini 3.8 Flash'lı ajan, sınırsız Tab | Haftalık temel limit |
| Lokal (Ollama / LM Studio) | qwen3.8:27b, gpt-oss-120b, Devstral | Sınırsız, GPU'na bağlı |

Kimi'nin ücretsiz katmanı **yok**; Alibaba Qwen'in bedava kotası 90 günle sınırlı.

---

## 6. Sırayla ne kurmalı (en kısa yol)

1. **Ajan**: Cline (VS Code) + OpenCode (terminal). Model olarak önce Antigravity/Copilot Free, yetmezse Groq veya Gemini API anahtarı.
2. **Video**: `npx hyperframes skills update` → ajana "30 saniyelik tanıtım videosu yap" de. Elinde ham görüntü varsa video-use'u aynı skill klasörüne ekle.
3. **Görsel/klip üretimi**: ComfyUI + Flux GGUF (foto) + LTX-Video 2B (kısa klip). Ağır modelleri bu kartta denemeye hiç başlama; kod-tabanlı motion graphics (HyperFrames/Remotion) videodaki estetiğin %80'ini zaten bedava veriyor.
4. **Site**: bolt.diy veya OpenDesign; tasarımı doğrudan repoya dosya olarak yazdır.

### Bu makine için gerçekçi ayar (RTX 5050 Laptop, 8 GB VRAM)

- **Rahat çalışır:** SDXL / Flux GGUF-q4 (fotoğraf), LTX-Video 2B (kısa 480p–720p klip), SadTalker, LatentSync, tüm FFmpeg/video-use/HyperFrames hattı.
- **Zorlar:** Wan 2.2 5B (yavaş, önce offload), Flux fp16.
- **Çalışmaz:** Wan 2.2 14B, Open-Sora, HunyuanVideo, InfiniteTalk — bunlar 24–80 GB istiyor. Bu işler için ücretsiz bulut kota (Hugging Face Spaces, Google AI Studio/Veo kotası) veya Higgsfield'ın limitli free planı daha mantıklı.
- Pratik sonuç: bu kartla **kurgu + altyazı + motion graphics + fotoğraf** tarafını tamamen bedava götürürsün; fotogerçekçi uzun video için bulut/ücretli kota şart.

Not: gerçekçi AI video (fotogerçek insan/ürün) hâlâ VRAM ve para istiyor; bedava tarafın güçlü olduğu yer **kurgu, altyazı, motion graphics, tasarım ve kod**.

---

## 7. "GitHub'a kursam bulut olur mu?"

Kısa cevap: **yarısı olur.** GitHub'ın bulutu CPU'dur, GPU'su yok — bu yüzden ajan + kurgu + site çalışır, model üretimi (ComfyUI/Flux/Wan) çalışmaz.

### GitHub Codespaces (bedava kota, kişisel hesap)
- **120 core-hour/ay** = 2 çekirdekli makinede **60 saat** (4 çekirdek seçersen 30 saat), **15 GB-month** depolama.
- **GPU yok, ücretsiz LLM token'ı yok.** 30 dk boşta kalınca durur, oturum en fazla 12 saat. Depolama, codespace kapalıyken de kotadan düşer.
- Port'u "Public" yaparsan tarayıcıdan erişebilirsin (SupoClip/bolt.diy arayüzü gibi).

| Codespaces'te çalışır | Neden |
|---|---|
| Cline / OpenCode + OmniRoute / FreeLLMAPI | Sadece Node + HTTP, model dışarıda |
| **SupoClip** | Docker destekli; ffmpeg + Whisper CPU'da döner, LLM dışarıda |
| video-use / FFmpeg kurgu | Kesme/altyazı CPU işi, hızlı |
| **HyperFrames / Remotion render** | Headless Chrome + ffmpeg; yavaş ama çalışır (videonun estetiği bu) |
| bolt.diy / OpenDesign / Dyad | Web app, port forward ile |
| ComfyUI / Flux / Wan / LTX / SadTalker | ❌ GPU yok — 2 çekirdekte diffusion pratikte imkânsız |

### GitHub Actions'ı render farm yapmak → yapma
Public repoda dakikalar bedava görünür ama runner **2 çekirdek / 7 GB, GPU yok**, iş başına 6 saat sınırı var ve **CI dışı iş için Actions kullanmak GitHub kullanım şartlarına aykırı**. Hesap kapatma riski.

### GitHub Pages → evet
Ürettiğin siteyi bedava yayınlamak için birebir: 1 GB site, ~100 GB/ay trafik, ücretsiz.

### Gerçek GPU'yu bedava nereden alacaksın
| Yer | Bedava olan | Sınır |
|---|---|---|
| **Kaggle Notebooks** | 2× T4 (16 GB) veya P100, **haftada ~30 saat** | Oturum 12 saat, telefon doğrulaması, kota hesabında görünür |
| Google Colab free | T4 | Kotası yayınlanmamış; SSH / web UI / uzak masaüstü yasak |
| Hugging Face ZeroGPU | Paylaşımlı RTX PRO 6000 | Günde ~5 dk; 30 günden eski hesap 2 Space'i bedava barındırır |
| Lightning AI | ~30 kredi (~75 saat T4) | Yeni hesap |
| Modal | **$30/ay** compute kredisi | Kart gerekir |

**Pratik kurulum:** Codespaces = ajan + SupoClip + HyperFrames; Kaggle = ComfyUI/Flux/LTX (ağır iş); kendi RTX 5050'n = SDXL/Flux-GGUF + LTX 2B (kesintisiz, kuyruk yok); GitHub Pages = çıkan site.

---

## 8. Senin bulduğun 4 repo — tek tek karar

| Repo | Ne | Lisans | Karar |
|---|---|---|---|
| **SupoClip** `FujiwaraChoki/supoclip` | Gerçek OpusClip alternatifi: uzun videodan 3–7 dikey klip, yüz takipli 9:16 kırpma, kelime senkron altyazı, viralite skoru, **MCP sunucusu** (Claude/Cline sürücü olabilir). AssemblyAI + LLM anahtarı ya da tamamen lokal (whisper + Ollama). Docker Compose. Hosted sürümünde ücretsiz dakika yok → self-host tek yol. | AGPL-3.0 (~2,6k★) | ✅ **Al.** Videodaki işin (uzun video → klipler) en olgun açık kaynak hali. Codespaces'te CPU'da çalışır. |
| **FreeLLMAPI** `tashfeenahmed/freellmapi` | 34 sağlayıcının free tier'ını tek `/v1` OpenAI endpoint'ine toplar; AES-256 şifreli anahtar, otomatik failover, kota takibi. Kurulum: `curl -fsSL freellmapi.co/install.sh \| bash` (Docker) → localhost:3001. | MIT (~24k★) | ✅ **Al** ama **sadece lokal/Codespaces**. README "internete açmayın" diyor; ücretsiz kurulumun model kataloğu 30 gün geriden gelir. |
| **OmniRoute** `diegosouzapw/OmniRoute` | 350+ sağlayıcı (150+ ücretsiz), kota-farkında otomatik fallback, RTK+Caveman token sıkıştırma (%15–95), MCP sunucusu; Cline/OpenCode/Claude Code/Cursor ile uyumlu. `npx omniroute` → localhost:20128. | MIT (~60–79k★) | ✅ **Al**, ama FreeLLMAPI ile **birlikte kurma** — ikisi de aynı işi yapar, biri yeter. Proje "Cheaper Inference" çatısına taşınmış ve aynı isimde çok fork var: README'si `omniroute.online`'a işaret edeni kur. |
| **Open-Higgsfield-AI / Open-Generative-AI** (`Anil-matcha` ve onlarca birebir fork) | Higgsfield tarzı arayüz; "200–600 model" listesi aslında **Muapi.ai ücretli API** kataloğu. Yerel ağırlık yok, anahtar/kredi ister. | MIT | ⚠️ **Bedava değil.** Sadece arayüzü seveceksen kur; üretim parası Muapi'ye gider. Gerçek bedava yol: ComfyUI + açık ağırlıklar. |

### Güvenlik notu
FreeLLMAPI/OmniRoute gibi yönlendiriciler **tüm sağlayıcı anahtarlarını tek yerde** tutar. Public port açma, repoyu public yapıp anahtar koyma, ekran görüntüsünde anahtar gösterme. İkisi de kendi README'sinde "kişisel deney, prodüksiyon değil" diyor; sağlayıcıların ToS'u proxy arkasından da geçerli.
