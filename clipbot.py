#!/usr/bin/env python3
"""clipbot — canlı yayından dikey klip üretimi.

Akış:  watch  -> yayını kaydeder, chat hızı + ses seviyesini loglar
       detect -> kayıttaki patlamaları bulur, adaylar.json yazar
       cut    -> adayları 9:16 (veya 16:9) klip olarak render eder
       queue  -> üretilen klipleri ve yükleme durumunu listeler

Sadece standart kütüphane + ffmpeg gerekir.
streamlink / yt-dlp : yayın URL'sini çözmek için
faster-whisper      : altyazı için (opsiyonel)
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import http.server
import json
import os
import random
import re
import secrets
import shutil
import socket
import subprocess
import sys
import sysconfig
import threading
import time
import tomllib
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# yardımcılar
# --------------------------------------------------------------------------- #
def cfg_get(cfg, path, default=None):
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return default if cur is None else cur


def load_config(path):
    p = Path(path)
    if not p.exists():
        sys.exit(f"[hata] config bulunamadi: {p}")
    with p.open("rb") as f:
        return tomllib.load(f)


def ffmpeg_bin(cfg):
    exe = cfg_get(cfg, "paths.ffmpeg") or os.environ.get("FFMPEG") or ""
    if exe:
        return exe
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def run_ff(args, cfg, desc="", capture=False, cwd=None):
    cmd = [ffmpeg_bin(cfg), "-hide_banner"] + [str(a) for a in args]
    if desc:
        print(f"  $ {desc}")
    if capture:
        return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    return subprocess.run(cmd, cwd=cwd)


def slug(text, limit=48):
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (text or "klip")[:limit]


def stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def ass_time(t):
    t = max(0.0, float(t))
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def srt_time(t):
    t = max(0.0, float(t))
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def stdev(values, mean):
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return 0.0
    return (sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5


# --------------------------------------------------------------------------- #
# canlı kaynak
# --------------------------------------------------------------------------- #
def tool_argv(name, module):
    """Aracı PATH'te, pip'in Scripts dizininde ya da python -m ile bulur."""
    exe = shutil.which(name)
    if exe:
        return [exe]
    candidates = []
    for scheme in ("nt_user", "nt", "posix_user", "posix_prefix"):
        try:
            base = sysconfig.get_path("scripts", scheme)
        except (KeyError, TypeError):
            continue
        if base:
            candidates += [Path(base) / name, Path(base) / f"{name}.exe"]
    for cand in candidates:
        if cand.exists():
            return [str(cand)]
    try:
        __import__(module)
        return [sys.executable, "-m", module]
    except Exception:
        return None


def load_streams(path):
    p = Path(path)
    if not p.exists():
        return []
    with p.open("rb") as f:
        data = tomllib.load(f)
    return data.get("channels", []) or []


def find_stream(name, path):
    for entry in load_streams(path):
        if str(entry.get("name", "")).strip().lower() == str(name).strip().lower():
            return entry
    return None


def resolve_channel_arg(value, streams_path):
    """--channel: once streams.toml'a bakar, yoksa adi dogrudan platform kanali sayar.
    viper | kick:viper | yt:@viper | https://... hepsi calisir."""
    raw = (value or "").strip()
    if not raw:
        return None
    entry = find_stream(raw, streams_path)
    if entry:
        return entry
    low = raw.lower()
    if low.startswith(("http://", "https://")):
        url = raw
    elif low.startswith("kick:"):
        url = f"https://kick.com/{raw.split(':', 1)[1].strip()}"
    elif low.startswith(("yt:", "youtube:")):
        handle = raw.split(":", 1)[1].strip().lstrip("@")
        url = f"https://www.youtube.com/@{handle}/live"
    else:
        url = f"https://www.twitch.tv/{raw.split('/')[-1].strip()}"
    return {"name": raw, "url": url, "chat": twitch_channel_from_url(url),
            "note": "streams.toml disinda (otomatik)"}


def cmd_channels(args, cfg):
    entries = load_streams(args.streams or str(ROOT / "streams.toml"))
    if not entries:
        print("  streams.toml bos")
        return
    print(f"{'#':>2}  {'isim':<18} {'url'}")
    for i, e in enumerate(entries, 1):
        print(f"{i:>2}  {str(e.get('name', '')):<18} {e.get('url', '')}")
    print("\n  deneme kaydi: python clipbot.py watch --channel <isim> --seconds 90")
    print("  listede olmayan yayinci: --channel viper (Twitch) | --channel kick:viper | --channel yt:@viper")


def resolve_stream(url):
    """Twitch/Kick/YouTube canlı linkini ffmpeg'in okuyabileceği m3u8/mp4 URL'sine çevirir."""
    sl = tool_argv("streamlink", "streamlink")
    yt = tool_argv("yt-dlp", "yt_dlp")
    attempts = []
    if sl:
        attempts.append((sl + ["--stream-url", url, "best"], "streamlink"))
        attempts.append((sl + ["--stream-url", url, "best,worst"], "streamlink"))
    if yt:
        attempts.append((yt + ["-g", "-f", "best[height<=1080]/best", "--no-warnings", url], "yt-dlp"))
        attempts.append((yt + ["-g", "--no-warnings", url], "yt-dlp"))
    for cmd, name in attempts:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        except (subprocess.TimeoutExpired, OSError):
            continue
        if p.returncode == 0 and p.stdout.strip():
            lines = [ln.strip() for ln in p.stdout.splitlines() if ln.strip().startswith("http")]
            if lines:
                print(f"  kaynak: {name} -> {lines[0][:70]}...")
                return lines[0]
    sys.exit(
        f"[hata] yayin URL'si cozulemedi: {url}\n"
        "        en sik sebep: kanal kapali ya da isim yanlis (twitch.tv/<isim> adresini tarayicida ac).\n"
        "        kurulum eksikse: pip install streamlink yt-dlp"
    )


def twitch_channel_from_url(url):
    m = re.search(r"twitch\.tv/([A-Za-z0-9_]+)", url or "")
    return m.group(1).lower() if m else ""


def channel_name_from_url(url):
    """Twitch/Kick/YouTube linkinden kanal adi (klasor ve baslik icin)."""
    url = url or ""
    tw = twitch_channel_from_url(url)
    if tw:
        return tw
    m = re.search(r"kick\.com/([A-Za-z0-9_.-]+)", url)
    if m and m.group(1).lower() not in ("video", "category", "browse"):
        return m.group(1).lower()
    m = re.search(r"youtube\.com/@([A-Za-z0-9_.-]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"youtube\.com/(?:channel|c|user)/([A-Za-z0-9_-]+)", url)
    if m:
        return m.group(1)
    return ""


class TwitchChat(threading.Thread):
    """Anonim IRC ile chat mesaj hızını sayar (token gerekmez)."""

    def __init__(self, channel):
        super().__init__(daemon=True)
        self.channel = channel
        self.events = []
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.connected = False

    def run(self):
        backoff = 5.0
        while not self.stop_flag.is_set():
            try:
                self._session()
                backoff = 5.0
            except Exception as exc:  # noqa: BLE001
                if not self.stop_flag.is_set():
                    print(f"  [chat] baglanti koptu ({exc}); {backoff:.0f}s sonra tekrar")
                    self.stop_flag.wait(backoff)
                    backoff = min(backoff * 2, 60)

    def _session(self):
        sock = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=300)
        nick = f"justinfan{random.randint(10000, 99999)}"
        sock.sendall(f"NICK {nick}\r\nJOIN #{self.channel}\r\n".encode())
        self.connected = True
        print(f"  [chat] #{self.channel} dinleniyor")
        buf = b""
        while not self.stop_flag.is_set():
            try:
                chunk = sock.recv(8192)
            except socket.timeout:
                continue  # sessiz chat: baglantiyi koparma
            if not chunk:
                raise ConnectionError("chat baglantisi kapandi")
            buf += chunk
            while b"\r\n" in buf:
                line, buf = buf.split(b"\r\n", 1)
                text = line.decode("utf-8", "ignore")
                if "PRIVMSG" in text:
                    self._hit()
                elif text.startswith("PING"):
                    sock.sendall(b"PONG :tmi.twitch.tv\r\n")

    def _hit(self):
        now = time.time()
        with self.lock:
            self.events.append(now)
            cutoff = now - 60
            if len(self.events) > 400 and self.events[0] < cutoff:
                self.events = [e for e in self.events if e >= cutoff]

    def rate(self, window=10.0):
        now = time.time()
        with self.lock:
            recent = [e for e in self.events if now - e <= window]
        return len(recent) / window

    def stop(self):
        self.stop_flag.set()


def measure_rms(video, start, dur, cfg):
    """Büyümekte olan kayıttan bir pencerenin RMS seviyesini (dBFS) ölçer."""
    p = run_ff(
        ["-ss", f"{max(0.0, float(start)):.2f}", "-i", str(video), "-t", f"{dur:.0f}",
         "-vn", "-af",
         "astats=metadata=1:reset=1,ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-",
         "-f", "null", "-"],
        cfg,
        capture=True,
    )
    if p.returncode != 0:
        return None
    vals = re.findall(r"RMS_level=(-?\d+(?:\.\d+)?|-inf)", p.stdout or "")
    nums = [float(v) for v in vals if v not in ("-inf", "inf")]
    return nums[-1] if nums else None


def loudness_series(video, start, dur, cfg):
    """Kaydın bir penceresi için (saniye, LUFS) serisi — tek geçiş, ~100 ms çözünürlük."""
    dur = max(0.5, float(dur))
    cmd = [ffmpeg_bin(cfg), "-hide_banner", "-nostats",
           "-ss", f"{max(0.0, float(start)):.2f}", "-i", str(video),
           "-t", f"{dur:.2f}", "-vn",
           "-af", "ebur128=metadata=1,ametadata=mode=print:key=lavfi.r128.M:file=-",
           "-f", "null", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.SubprocessError):
        return []
    text = f"{p.stdout or ''}\n{p.stderr or ''}"
    vals = [float(m.group(1)) for m in
            re.finditer(r"lavfi\.r128\.M=(-?\d+(?:\.\d+)?)", text)]
    if len(vals) < 2:
        return []
    step = dur / len(vals)
    return [(i * step, v) for i, v in enumerate(vals)]


def find_bursts(series, boost_db, min_gap, pre_roll, min_lufs=-38.0, min_len=1.5, step=0.1):
    """Ses serisindeki patlamaları bulur: tabandan boost_db yuksek, en az min_len sn suren bolgeler."""
    vals = [v for _, v in series if v > -70]
    if len(vals) < 5:
        return []
    base = median(vals)
    clusters = []
    for t, v in series:
        if v <= -70 or v - base < boost_db:
            continue
        if clusters and t - clusters[-1]["last"] <= step * 3:
            clusters[-1]["items"].append((t, v))
            clusters[-1]["last"] = t
        else:
            clusters.append({"items": [(t, v)], "last": t})
    out = []
    for cl in clusters:
        peak_t, peak_v = max(cl["items"], key=lambda x: x[1])
        # yayinda sessizliga donerken olusan yapay patlamalari at
        if len(cl["items"]) * step < min_len or peak_v < min_lufs:
            continue
        out.append({"t": max(0.0, peak_t - pre_roll), "gain": peak_v - base,
                    "base": base, "peak": peak_v})
    merged = []
    for b in sorted(out, key=lambda x: x["t"]):
        if merged and b["t"] - merged[-1]["t"] < min_gap:
            if b["gain"] > merged[-1]["gain"]:
                merged[-1] = b
            continue
        merged.append(b)
    return merged


# --------------------------------------------------------------------------- #
# 1) watch
# --------------------------------------------------------------------------- #
def cmd_watch(args, cfg):
    url = args.url
    chat_arg = args.chat
    if not url and getattr(args, "channel", ""):
        entry = resolve_channel_arg(args.channel, getattr(args, "streams", "") or str(ROOT / "streams.toml"))
        if not entry:
            sys.exit("[hata] --channel bos.  ornek: --channel viper  ya da  --url https://...")
        url = str(entry.get("url", ""))
        chat_arg = chat_arg or str(entry.get("chat", ""))
        print(f"  stream: {entry.get('name')} -> {url}  ({entry.get('note', '')})")
    if not url:
        sys.exit("[hata] --url ya da --channel ver (ornek: --channel kanal1)")
    # once yayin linkini coz: kanal kapaliysa bos klasor birakma
    stream_url = resolve_stream(url)

    sessions_dir = ROOT / cfg_get(cfg, "paths.sessions", "sessions")
    sessions_dir.mkdir(parents=True, exist_ok=True)
    channel = chat_arg or channel_name_from_url(url) or "stream"
    session = sessions_dir / f"{slug(channel)}-{stamp()}"
    session.mkdir(parents=True, exist_ok=True)
    video = session / "video.ts"
    max_hours = float(args.max_hours or cfg_get(cfg, "record.max_hours", 6))
    limit_s = float(args.seconds) if getattr(args, "seconds", 0) else max_hours * 3600
    sample = float(cfg_get(cfg, "record.sample_seconds", 5))

    print(f"  kayit: {video}")
    # reconnect: HLS segmenti dusunce kayit kopsun diye (auto ile ayni davranis)
    cmd = [ffmpeg_bin(cfg), "-hide_banner", "-loglevel", "warning", "-y",
           "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
           "-i", stream_url, "-c", "copy", "-f", "mpegts", str(video)]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)

    # ilk baytın yazıldığı an = video zaman çizgisinin başlangıcı
    video_start = None
    for _ in range(120):
        if proc.poll() is not None:
            sys.exit("[hata] ffmpeg kaydi baslatamadi (yayin kapali olabilir)")
        try:
            if video.exists() and video.stat().st_size > 0:
                video_start = time.time()
                break
        except OSError:
            pass
        time.sleep(0.25)
    if video_start is None:
        proc.terminate()
        sys.exit("[hata] kayit baslamadi")

    # chat sadece Twitch'te var; Kick/YouTube'da tetikleyici ses seviyesi
    chat_name = chat_arg or twitch_channel_from_url(url)
    chat = TwitchChat(chat_name) if chat_name and not args.no_chat else None
    if chat:
        chat.start()
    last_rms = None
    next_rms_at = 15.0

    meta = {
        "url": url,
        "channel": channel,
        "video": video.name,
        "video_start_epoch": video_start,
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "chat": bool(chat),
        "notes": args.note or "",
    }
    (session / "session.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    events_path = session / "events.csv"
    peak_count = 0
    print(f"  kayit calisiyor. Bitirmek icin Ctrl+C.  (limit {limit_s / 60:.1f} dk)")

    try:
        with events_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["t", "chat_per_sec", "rms_db"])
            while True:
                if proc.poll() is not None:
                    print("\n  [uyari] yayin/ffmpeg durdu")
                    break
                elapsed = time.time() - video_start
                if elapsed > limit_s:
                    print("\n  sure doldu")
                    break
                rate = chat.rate() if chat else 0.0
                if elapsed >= next_rms_at:
                    measured = measure_rms(video, elapsed - 12, 10, cfg)
                    if measured is not None:
                        last_rms = measured
                    next_rms_at = elapsed + 15
                writer.writerow([f"{elapsed:.2f}", f"{rate:.2f}", "" if last_rms is None else f"{last_rms:.1f}"])
                fh.flush()
                if rate > 0:
                    peak_count = max(peak_count, int(rate))
                if int(elapsed) % 60 < sample:
                    shown = f"{last_rms:.1f}" if last_rms is not None else "--"
                    print(f"\r  {int(elapsed // 60):02d}:{int(elapsed % 60):02d}  chat {rate:4.1f}/sn  ses {shown} dB   ", end="")
                time.sleep(sample)
    except KeyboardInterrupt:
        print("\n  kayit bitiriliyor...")
    finally:
        if chat:
            chat.stop()
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()

    try:
        size_mb = video.stat().st_size / 1_048_576
    except OSError:
        size_mb = 0
    print(f"\n  bitti: {session}  ({size_mb:.0f} MB, tepe chat {peak_count}/sn)")
    print(f"  sonraki adim:  python clipbot.py detect --session \"{session}\"")


# --------------------------------------------------------------------------- #
# 2) detect
# --------------------------------------------------------------------------- #
def cmd_detect(args, cfg):
    session = Path(args.session)
    events_path = session / "events.csv"
    if not events_path.exists():
        sys.exit(f"[hata] events.csv yok: {events_path}")
    meta = json.loads((session / "session.json").read_text(encoding="utf-8"))

    rows = []
    with events_path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                t = float(row["t"])
            except (TypeError, ValueError):
                continue
            chat = float(row.get("chat_per_sec") or 0)
            rms = row.get("rms_db")
            rows.append({"t": t, "chat": chat, "rms": float(rms) if rms not in (None, "") else None})
    if len(rows) < 5:
        sys.exit("[hata] cok az veri var (kayit cok kisa)")

    chats = [r["chat"] for r in rows]
    rms_vals = [r["rms"] for r in rows if r["rms"] is not None]
    chat_mean = sum(chats) / len(chats)
    chat_sd = stdev(chats, chat_mean) or 0.0
    rms_base = median(rms_vals)
    threshold = float(cfg_get(cfg, "detect.z_threshold", 3.0))

    scored = []
    for r in rows:
        z = (r["chat"] - chat_mean) / chat_sd if chat_sd > 0.2 else 0.0
        gain = (r["rms"] - rms_base) / 6.0 if (r["rms"] is not None and rms_base is not None) else 0.0
        score = max(z, gain, 0.0)
        scored.append({**r, "score": score})

    hot = [s for s in scored if s["score"] >= threshold]
    min_gap = float(cfg_get(cfg, "detect.min_gap_seconds", 45))
    pre_roll = float(cfg_get(cfg, "detect.pre_roll", 4))
    clip_len = float(cfg_get(cfg, "detect.clip_seconds", 32))

    clusters = []
    for item in hot:
        if clusters and item["t"] - clusters[-1]["last_t"] < min_gap:
            clusters[-1]["items"].append(item)
            clusters[-1]["last_t"] = item["t"]
        else:
            clusters.append({"items": [item], "last_t": item["t"]})

    items = []
    for cl in clusters:
        best = max(cl["items"], key=lambda x: x["score"])
        start = max(0.0, best["t"] - pre_roll)
        reasons = []
        if chat_sd > 0.2 and (best["chat"] - chat_mean) / chat_sd >= threshold:
            reasons.append(f"chat {best['chat']:.1f}/sn (z={((best['chat'] - chat_mean) / chat_sd):.1f})")
        if best["rms"] is not None and rms_base is not None and best["rms"] - rms_base >= 6:
            reasons.append(f"ses +{best['rms'] - rms_base:.0f}dB")
        items.append({
            "t": round(start, 2),
            "dur": clip_len,
            "score": round(best["score"], 2),
            "reason": ", ".join(reasons) or f"skor {best['score']:.1f}",
        })

    items.sort(key=lambda x: x["score"], reverse=True)
    items = items[: int(args.top or cfg_get(cfg, "detect.max_clips", 8))]
    items.sort(key=lambda x: x["t"])

    out = {
        "session": session.name,
        "channel": meta.get("channel"),
        "video": meta.get("video", "video.ts"),
        "threshold": threshold,
        "baseline": {"chat_mean": round(chat_mean, 2), "chat_sd": round(chat_sd, 2), "rms_db": rms_base},
        "items": items,
    }
    (session / "candidates.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n  taban: chat {chat_mean:.1f}/sn (sd {chat_sd:.1f}), ses {rms_base if rms_base is not None else '--'} dB")
    if not items:
        print("  aday yok. esigi dusur: config.toml -> detect.z_threshold = 2.0 ve tekrar detect")
        return
    print(f"  {len(items)} aday -> {session / 'candidates.json'}")
    for i, it in enumerate(items, 1):
        print(f"   {i:2d}. {int(it['t'] // 60):02d}:{int(it['t'] % 60):02d}  skor {it['score']:.1f}  {it['reason']}")
    print(f"\n  sonraki adim:  python clipbot.py cut --session \"{session}\"")


# --------------------------------------------------------------------------- #
# 3) cut
# --------------------------------------------------------------------------- #
def transcribe_groq(audio_path, cfg):
    """Groq'un ücretsiz Whisper kotasıyla kelime zamanlı transkripsiyon (sadece stdlib)."""
    key = os.environ.get("GROQ_API_KEY") or cfg_get(cfg, "subs.api_key", "")
    if not key:
        return None
    model = cfg_get(cfg, "subs.groq_model", "whisper-large-v3-turbo")
    boundary = f"----clipbot{random.randint(10 ** 8, 10 ** 9)}"
    data = Path(audio_path).read_bytes()
    parts = []

    def field(name, value):
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )

    field("model", model)
    field("response_format", "verbose_json")
    field("timestamp_granularities[]", "word")
    lang = cfg_get(cfg, "subs.language", "")
    if lang:
        field("language", lang)
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="clip.m4a"\r\n'
        f"Content-Type: audio/mp4\r\n\r\n".encode()
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        data=b"".join(parts),
        headers={"Authorization": f"Bearer {key}",
                 "User-Agent": "clipbot/1.0 (+python-urllib)",
                 "Accept": "application/json",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")[:300]
        print(f"  [altyazi] groq HTTP {exc.code}: {body}")
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"  [altyazi] groq hatasi: {exc}")
        return None
    words = []
    # Groq verbose_json: kelimeler kok seviyede "words" altinda gelir
    for w in payload.get("words") or []:
        text = (w.get("word") or "").strip()
        if text:
            words.append({"start": float(w["start"]), "end": float(w["end"]), "text": text})
    # OpenAI uyumlu bicim: segments[].words
    if not words:
        for seg in payload.get("segments") or []:
            for w in seg.get("words") or []:
                text = (w.get("word") or "").strip()
                if text:
                    words.append({"start": float(w["start"]), "end": float(w["end"]), "text": text})
    if not words:
        for seg in payload.get("segments") or []:
            text = (seg.get("text") or "").strip()
            if text:
                words.append({"start": float(seg["start"]), "end": float(seg["end"]), "text": text})
    return words


_WHISPER_CACHE = {}


def _whisper_model(name, device):
    key = (name, device)
    if key not in _WHISPER_CACHE:
        from faster_whisper import WhisperModel  # type: ignore

        _WHISPER_CACHE[key] = WhisperModel(name, device=device, compute_type="int8")
    return _WHISPER_CACHE[key]


def group_words(words, max_words=3, max_span=1.6, max_gap=0.45):
    chunks = []
    cur = []
    for w in words:
        if cur:
            span = w["end"] - cur[0]["start"]
            gap = w["start"] - cur[-1]["end"]
            if len(cur) >= max_words or span > max_span or gap > max_gap:
                chunks.append(cur)
                cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    out = []
    for ch in chunks:
        text = " ".join(w["text"] for w in ch)
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"^[^\w]+|[^\w!?%]+$", "", text)
        if not text:
            continue
        out.append({"start": ch[0]["start"], "end": max(ch[-1]["end"], ch[0]["start"] + 0.4),
                    "text": text.upper(), "words": ch})
    return out


def write_ass(chunks, path, cfg, hook_text="", reserve_top=0):
    font = cfg_get(cfg, "subs.font", "Arial Black")
    size = int(cfg_get(cfg, "subs.size", 92))
    tall = int(cfg_get(cfg, "video.height", 1920)) >= 1920
    margin_v = int(cfg_get(cfg, "subs.margin_v", 470 if tall else 200))
    hook_s = float(cfg_get(cfg, "subs.hook_seconds", 2.6))
    # kanca yazisi ust kenarda: webcam penceresi oradaysa altina inmesin
    hook_mv = max(110, int(reserve_top) + 26)
    highlight = bool(cfg_get(cfg, "subs.highlight", True))
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
        "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Bot,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,"
        f"-1,0,0,0,100,100,1.5,0,1,8,3,2,60,60,{margin_v},1\n"
        f"Style: Hook,{font},{int(size * 0.76)},&H0000FFFF,&H0000FFFF,&HB4000000,&HB4000000,"
        f"-1,0,0,0,100,100,1,0,3,18,0,8,50,50,{hook_mv},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    def esc(t):
        return str(t).replace("{", "(").replace("}", ")").replace("\n", " ")

    lines = []
    # klip basinda ustte buyuk baslik (kaydirmayi durduran kanca)
    if hook_text and hook_s > 0:
        # kucukten buyuyerek girsin: kaydirirken ilk saniyede goze carpar
        lines.append(
            f"Dialogue: 0,0:00:00.00,{ass_time(hook_s)},Hook,,0,0,0,,"
            f"{{\\fad(100,220)\\fscx78\\fscy78\\t(0,150,\\fscx100\\fscy100)}}"
            f"{esc(hook_text.upper())[:44]}"
        )
    for ch in chunks:
        words = ch.get("words") or []
        if highlight and len(words) > 1:
            # konusulan kelime sari yanar (kelime kelime altyazi)
            for wi, w in enumerate(words):
                start = ch["start"] if wi == 0 else float(w["start"])
                end = ch["end"] if wi == len(words) - 1 else float(words[wi + 1]["start"])
                if end - start < 0.08:
                    end = start + 0.08
                marked = []
                for j, w2 in enumerate(words):
                    t = esc(w2["text"])
                    marked.append(f"{{\\c&H00FFFF&}}{t}{{\\c&HFFFFFF&}}" if j == wi else t)
                pop = "\\fscx88\\fscy88\\t(0,110,\\fscx100\\fscy100)" if wi == 0 else ""
                lines.append(
                    f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Bot,,0,0,0,,"
                    f"{{\\fad(20,20){pop}}}{' '.join(marked)}"
                )
        else:
            lines.append(
                f"Dialogue: 0,{ass_time(ch['start'])},{ass_time(ch['end'])},Bot,,0,0,0,,"
                f"{{\\fad(60,40)\\fscx88\\fscy88\\t(0,110,\\fscx100\\fscy100)}}{esc(ch['text'])}"
            )
    Path(path).write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


def write_srt(chunks, path):
    out = []
    for i, ch in enumerate(chunks, 1):
        out.append(f"{i}\n{srt_time(ch['start'])} --> {srt_time(ch['end'])}\n{ch['text']}\n")
    Path(path).write_text("\n".join(out), encoding="utf-8")


# --------------------------------------------------------------------------- #
# webcam (yuz) tespiti — klibin kosesine kucuk pencere olarak basilir
# --------------------------------------------------------------------------- #
FACE_CASCADES = (
    "haarcascade_frontalface_default.xml",
    "haarcascade_frontalface_alt2.xml",
    "haarcascade_profileface.xml",
)


def video_size(video, cfg):
    """Kaynagin (genislik, yukseklik); okunamazsa (0, 0)."""
    p = run_ff(["-i", str(video)], cfg, capture=True)
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", f"{p.stdout}\n{p.stderr}", re.S)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def video_duration(video, cfg):
    """Kaynagin saniye cinsinden suresi; okunamazsa 0.0."""
    p = run_ff(["-i", str(video)], cfg, capture=True)
    m = re.search(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", f"{p.stdout}\n{p.stderr}")
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def fc_detect(video, cfg):
    """Kayittan esit aralikli kareler alip yuz kumelerini sayar -> webcam penceresi.

    Tek karedeki yanlis pozitifleri elemek icin ayni bolgede en az birkac ayri karede
    yuz ariyoruz: webcam'de yuz her karede vardir, oyunun icindeki yuzler tek tuk cikar.
    """
    try:
        import cv2  # type: ignore
    except Exception:
        return None
    samples = int(cfg_get(cfg, "facecam.samples", 18))
    span = float(cfg_get(cfg, "facecam.scan_seconds", 420))
    tmp = ROOT / ".tmp_audio" / "facecam"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    interval = max(1.0, span / max(1, samples))
    res = run_ff(["-y", "-loglevel", "error", "-i", str(video), "-t", f"{span:.0f}",
                  "-vf", f"fps=1/{interval:.3f},scale=1280:-2",
                  "-frames:v", str(samples), str(tmp / "f%03d.png")], cfg, capture=True)
    files = sorted(tmp.glob("f*.png"))
    if not files:
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    cascades = []
    for name in FACE_CASCADES:
        try:
            c = cv2.CascadeClassifier(cv2.data.haarcascades + name)
            if not c.empty():
                cascades.append(c)
        except Exception:
            pass
    if not cascades:
        return None
    boxes_per_frame = []
    sample_w = sample_h = 0
    for f in files:
        img = cv2.imread(str(f))
        if img is None:
            continue
        sample_h, sample_w = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        found = []
        for c in cascades:
            try:
                found += [tuple(int(v) for v in b)
                          for b in c.detectMultiScale(gray, 1.08, 5, minSize=(24, 24))]
            except Exception:
                pass
        boxes_per_frame.append(found)
    shutil.rmtree(tmp, ignore_errors=True)
    frames = len(boxes_per_frame)
    if frames < 3:
        return None
    cell = 80  # 1280 px genislikte 16 sutun

    def in_corner(key):
        """Webcam neredeyse hep bir kosede durur; oyunun ortasindaki yuzler elenir."""
        if str(cfg_get(cfg, "facecam.region", "corner")).lower() != "corner":
            return True
        cx, cy = (key[0] + 0.5) * cell, (key[1] + 0.5) * cell
        out_x = cx < 0.35 * sample_w or cx > 0.65 * sample_w
        out_y = cy < 0.35 * sample_h or cy > 0.65 * sample_h
        return out_x and out_y

    support = {}
    for fi, boxes in enumerate(boxes_per_frame):
        for x, y, bw, bh in boxes:
            key = ((x + bw // 2) // cell, (y + bh // 2) // cell)
            if in_corner(key):
                support.setdefault(key, set()).add(fi)
    if not support:
        return None
    (bx, by), seen = max(support.items(), key=lambda kv: len(kv[1]))
    if len(seen) < max(3, int(round(frames * 0.35))):
        return None
    near = [(x, y, bw, bh) for boxes in boxes_per_frame for x, y, bw, bh in boxes
            if abs((x + bw // 2) // cell - bx) <= 2 and abs((y + bh // 2) // cell - by) <= 2]
    if not near:
        return None
    W, H = video_size(video, cfg)
    if W <= 0 or H <= 0:
        return None
    s = W / float(sample_w or 1280)
    # aykiri tespitler kutuyu sismesin: ortalama degil medyan yuz kutusu
    fw = float(median([b[2] for b in near]) or 40) * s
    fh = float(median([b[3] for b in near]) or 40) * s
    fcx = float(median([b[0] + b[2] / 2 for b in near])) * s
    fcy = float(median([b[1] + b[3] / 2 for b in near])) * s
    fx1, fx2 = fcx - fw / 2, fcx + fw / 2
    fy1, fy2 = fcy - fh / 2, fcy + fh / 2
    right, bottom = fcx > W / 2, fcy > H / 2
    mx, my = max(fw * 0.8, 12.0), max(fh * 0.6, 12.0)
    x1, x2 = max(0.0, fx1 - mx), min(float(W), fx2 + mx)
    y1, y2 = max(0.0, fy1 - my), min(float(H), fy2 + my)
    # kenara %15'ten yakinsa pencere kenari oraya oturur (webcam kosesi gibi)
    if right and W - x2 < 0.15 * W:
        x2 = float(W)
    if not right and x1 < 0.15 * W:
        x1 = 0.0
    if bottom and H - y2 < 0.15 * H:
        y2 = float(H)
    if not bottom and y1 < 0.15 * H:
        y1 = 0.0
    target = 16 / 9  # PIP 16:9 gorunsun: eksik boyutu koseden uygun tarafa buyut
    if (x2 - x1) / max(1.0, y2 - y1) < target:
        need = (y2 - y1) * target
        x1, x2 = (x2 - need, x2) if right else (x1, x1 + need)
        if x1 < 0:
            x1, x2 = 0.0, min(need, float(W))
        elif x2 > W:
            x2, x1 = float(W), max(0.0, W - need)
        if (x2 - x1) / max(1.0, y2 - y1) < target - 0.05:  # genisleyemedi -> yuksekligi kirp
            y2 = y1 + max(1.0, x2 - x1) / target
    else:
        need = (x2 - x1) / target
        y1, y2 = (y2 - need, y2) if bottom else (y1, y1 + need)
        if y1 < 0:
            y1, y2 = 0.0, min(need, float(H))
        elif y2 > H:
            y2, y1 = float(H), max(0.0, H - need)
        if (y2 - y1) / max(1.0, (x2 - x1) / target) < target - 0.05:
            x2 = x1 + (y2 - y1) * target
    w, h = max(2, int(x2 - x1)), max(2, int(y2 - y1))
    return {
        "found": True, "x": int(x1) // 2 * 2, "y": int(y1) // 2 * 2,
        "w": w // 2 * 2, "h": h // 2 * 2, "source_w": W, "source_h": H,
        "corner": ("alt" if bottom else "ust") + "-" + ("sag" if right else "sol"),
        "hits": len(near), "frames": frames,
    }


def facecam_rect(video, cfg, force=False):
    """Oturum icin webcam penceresini bir kez bulur, facecam.json'a yazar."""
    if not bool(cfg_get(cfg, "facecam.enabled", True)):
        return None
    video = Path(video)
    cache = video.parent / "facecam.json"
    retry = float(cfg_get(cfg, "facecam.retry_seconds", 600))
    if cache.exists() and not force:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if data and data.get("video") == video.name:
            if data.get("found"):
                return data
            if time.time() - float(data.get("checked_at") or 0) < retry:
                return None
    data = fc_detect(video, cfg)
    payload = {"video": video.name, "checked_at": time.time(), **(data or {"found": False})}
    try:
        cache.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    if data:
        print(f"  [facecam] {data['corner']}  {data['w']}x{data['h']} @ {data['x']},{data['y']}"
              f"  ({data['hits']}/{data['frames']} kare)")
    return data


def crop_keeps_facecam(fc, mode):
    """Secili crop webcam'i zaten iceriyorsa PIP'e gerek yok."""
    if mode == "stack":
        return False   # stack'te oyun paneli kirpiliyor: kamera karti her zaman ayrica basilir
    W, H = int(fc.get("source_w") or 0), int(fc.get("source_h") or 0)
    if W <= 0 or H <= 0:
        return False
    cw = min(W, H * 9 / 16)
    ax = {"crop_left": 0.0, "crop_right": W - cw}.get(mode, (W - cw) / 2)
    cx = fc["x"] + fc["w"] / 2
    return ax + cw * 0.05 <= cx <= ax + cw * 0.95


def build_filter(mode, cfg, ass_name=None, logo_name=None):
    w = int(cfg_get(cfg, "video.width", 1080))
    h = int(cfg_get(cfg, "video.height", 1920))
    fps = int(cfg_get(cfg, "video.fps", 30))
    parts = []
    if mode == "blurpad":
        parts.append(
            f"[0:v]split=2[bgs][fgs];"
            f"[bgs]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},gblur=sigma=28,"
            f"eq=brightness=-0.05[bg];"
            f"[fgs]scale={w}:-2:flags=lanczos[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps={fps},setsar=1[v0]"
        )
    elif mode == "original":
        parts.append(f"[0:v]scale=1920:-2:flags=lanczos,fps={fps},setsar=1[v0]")
    else:
        anchor = {"crop_left": "0", "crop_right": "iw-ow"}.get(mode, "(iw-ow)/2")
        parts.append(
            f"[0:v]crop=w='min(iw,ih*9/16)':h='min(ih,iw*16/9)':x='{anchor}':y='(ih-oh)/2',"
            f"scale={w}:{h}:flags=lanczos,fps={fps},setsar=1[v0]"
        )
    last = "v0"
    if logo_name:
        parts.append(f"[1:v]scale=160:-1[wm];[{last}][wm]overlay=W-w-36:H-h-36[v1]")
        last = "v1"
    if ass_name:
        parts.append(f"[{last}]ass={ass_name}[vout]")
        last = "vout"
    if last != "vout":
        parts.append(f"[{last}]null[vout]")
    return ";".join(parts)


def caption_block(cfg, title, channel, hook):
    tags = cfg_get(cfg, "caption.hashtags", "#gta6 #shorts #fyp").strip()
    tmpl = cfg_get(
        cfg,
        "caption.template",
        "{title}\n\n{channel} • GTA 6\n\n{hashtags}",
    )
    emoji = str(cfg_get(cfg, "caption.emoji", "") or "").strip()
    body = tmpl.format(title=title, channel=channel, hashtags=tags, hook=hook, emoji=emoji)
    return "\n".join(ln.strip() for ln in body.splitlines() if ln.strip())


def audio_chain(cfg):
    """Yayin sesi cok genis dinamik: once sikistir, sonra normalize et."""
    parts = []
    hp = float(cfg_get(cfg, "audio.highpass", 90.0))
    if hp > 0:
        # 90 Hz alti rumble: telefon hoparlorunde duyulmaz, sadece tepe yer
        parts.append(f"highpass=f={hp}")
    if bool(cfg_get(cfg, "audio.compress", True)):
        th = float(cfg_get(cfg, "audio.compress_threshold", -30.0))
        ratio = float(cfg_get(cfg, "audio.compress_ratio", 8.0))
        attack = float(cfg_get(cfg, "audio.compress_attack", 3.0))
        release = float(cfg_get(cfg, "audio.compress_release", 120.0))
        makeup = float(cfg_get(cfg, "audio.compress_makeup", 10.0))
        parts.append(f"acompressor=threshold={th}dB:ratio={ratio}:attack={attack}"
                     f":release={release}:makeup={makeup}")
    return "".join(p + "," for p in parts)


def loudnorm_filter(video, start, dur, cfg):
    """2 geçişli loudnorm: önce ölç, sonra ölçülen değerlerle uygula (hedefe ~0.3 LU)."""
    target = float(cfg_get(cfg, "audio.target_lufs", -12.0))
    tp = float(cfg_get(cfg, "audio.true_peak", -1.5))
    lra = float(cfg_get(cfg, "audio.lra", 11.0))
    linear = bool(cfg_get(cfg, "audio.linear", False))
    pre = audio_chain(cfg)
    first = f"{pre}loudnorm=I={target}:TP={tp}:LRA={lra}"
    p = run_ff(["-ss", f"{float(start):.3f}", "-i", str(video), "-t", f"{float(dur):.2f}",
                "-vn", "-af", f"{first}:print_format=json", "-f", "null", "-"],
               cfg, capture=True)
    text = f"{p.stdout or ''}\n{p.stderr or ''}"
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", text, re.S)
    if not m:
        return first
    limit = float(cfg_get(cfg, "audio.limiter", 0.95))
    # level=false: alimiter seviyeyi geri yukseltmesin (yoksa tepe 0 dB'ye dayaniyor)
    tail = f",alimiter=limit={limit}:level=false" if 0 < limit <= 1 else ""
    try:
        d = json.loads(m.group(0))
        if not float(d.get("input_i", "-inf")) > -99:
            return first   # sessiz pencere: olcum -inf, tek gecisli haline don
        return (f"{pre}loudnorm=I={target}:TP={tp}:LRA={lra}"
                f":measured_I={d['input_i']}:measured_TP={d['input_tp']}"
                f":measured_LRA={d['input_lra']}:measured_thresh={d['input_thresh']}"
                f":offset={d['target_offset']}:linear={'true' if linear else 'false'}{tail}")
    except (ValueError, KeyError):
        return first + tail


def ensure_sfx(cfg, name):
    """Ses efektlerini ilk kullanimda ffmpeg ile uretir (assets/sfx/<ad>.wav)."""
    d = ROOT / cfg_get(cfg, "sfx.dir", "assets/sfx")
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.wav"
    if p.exists() and p.stat().st_size > 1000:
        return p
    if name == "boom":
        # alcak frekansta dusen sinüs + yumusak kuyruk: kanca vurusu
        src = "aevalsrc=0.95*exp(-5.5*t)*sin(2*PI*(95-45*t)*t):d=1.35:s=48000"
        af = "lowpass=f=200,afade=t=in:st=0:d=0.004"
    elif name == "whoosh":
        src = "anoisesrc=d=0.95:c=pink:a=0.7:sample_rate=48000"
        af = ("highpass=f=350,lowpass=f=5200,"
              "volume=volume='if(lt(t,0.6),t/0.6,max(0,(0.95-t)/0.35))':eval=frame")
    else:
        return None
    tmp = d / f"{name}.raw.wav"
    res = run_ff(["-y", "-loglevel", "error", "-f", "lavfi", "-i", src, "-af", af,
                  "-ac", "2", "-ar", "48000", str(tmp)], cfg, desc=f"sfx -> {p.name}")
    if res.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        return None
    # filtre zinciri seviyeyi cok dusuruyor: tepeyi -3 dB'ye normalize et ki efekt duyulsun
    gain = 0.0
    try:
        probe = run_ff(["-nostats", "-i", str(tmp), "-af", "volumedetect", "-f", "null", "-"],
                       cfg, capture=True)
        m = re.search(r"max_volume:\s*(-?[\d.]+) dB",
                      f"{probe.stdout}\n{probe.stderr}")
        if m:
            gain = -3.0 - float(m.group(1))
    except (OSError, ValueError):
        gain = 0.0
    if abs(gain) > 0.3:
        res = run_ff(["-y", "-loglevel", "error", "-i", str(tmp),
                      "-af", f"volume={gain:.2f}dB", str(p)], cfg, desc=f"sfx normalize -> {p.name}")
    else:
        tmp.replace(p)
    tmp.unlink(missing_ok=True)
    return p if res.returncode == 0 and p.exists() else None


def audio_graph(cfg, video, video_index, start, dur, sfx_inputs):
    """Ses grafigi: normalize + (varsa) boom/whoosh karisimi.

    Doner: (filter_complex parcalari, map edilecek etiket)
    """
    if cfg_get(cfg, "audio.normalize", True):
        base = loudnorm_filter(video, start, dur, cfg)
    else:
        base = audio_chain(cfg).rstrip(",") or "anull"
    # sonda ses de kisilsin (gorselle ayni anda)
    fade = float(cfg_get(cfg, "video.fade_seconds", 0.3))
    fade_out = (f",afade=t=out:st={max(0.0, float(dur) - fade):.2f}:d={fade:.2f}"
                if fade > 0 and float(dur) > fade + 0.5 else "")
    if not sfx_inputs:
        if not base and not fade_out:
            return [], f"{video_index}:a:0?"
        return ([f"[{video_index}:a]{base or 'anull'}{fade_out}[aout]"], "[aout]")
    parts = [f"[{video_index}:a]{base or 'anull'}[abase]"]
    used = ["[abase]"]
    limit = float(cfg_get(cfg, "audio.limiter", 0.79))
    if "boom" in sfx_inputs:
        ms = int(max(0.0, float(cfg_get(cfg, "sfx.boom_at", 0.08))) * 1000)
        vol = float(cfg_get(cfg, "sfx.boom_volume", 0.8))
        parts.append(f"[{sfx_inputs['boom']}:a]volume={vol},adelay={ms}:all=1[aboom]")
        used.append("[aboom]")
    if "whoosh" in sfx_inputs:
        at = max(0.0, float(dur) - float(cfg_get(cfg, "sfx.whoosh_before_end", 1.1)))
        ms = int(at * 1000)
        vol = float(cfg_get(cfg, "sfx.whoosh_volume", 0.6))
        parts.append(f"[{sfx_inputs['whoosh']}:a]volume={vol},adelay={ms}:all=1[awhoosh]")
        used.append("[awhoosh]")
    tail = f",alimiter=limit={limit}:level=false" if 0 < limit <= 1 else ""
    parts.append("".join(used) + f"amix=inputs={len(used)}:duration=first"
                 f":dropout_transition=0:normalize=0{tail}{fade_out}[aout]")
    return parts, "[aout]"


def session_channel(video_path):
    """sessions/xrohat-20261115-083015/video.ts -> 'xrohat' (caption'da 'stream' yazmasin)"""
    p = Path(video_path)
    meta = p.parent / "session.json"
    if meta.exists():
        try:
            name = str(json.loads(meta.read_text(encoding="utf-8")).get("channel", "")).strip()
            if name:
                return name
        except (OSError, ValueError):
            pass
    stem = p.parent.name
    return re.sub(r"-\d{8}-\d{6}$", "", stem) if re.search(r"-\d{8}-\d{6}$", stem) else ""


def cmd_cut(args, cfg):
    mode = args.mode or cfg_get(cfg, "video.mode", "blurpad")
    default_dur = float(cfg_get(cfg, "detect.clip_seconds", 32))
    clips_root = ROOT / cfg_get(cfg, "paths.clips", "clips")
    subs_on = bool(cfg_get(cfg, "subs.enabled", True)) and not args.no_subs
    min_words = getattr(args, "min_words", None)
    if min_words is None:
        min_words = int(cfg_get(cfg, "detect.min_words", 3) or 0)
    min_words = int(min_words or 0)
    logo = cfg_get(cfg, "video.logo", "") or ""

    jobs = []
    if args.video:
        video_path = Path(args.video).resolve()
        if not video_path.exists():
            sys.exit(f"[hata] video yok: {video_path}")
        jobs.append({
            "video": video_path,
            "session": getattr(args, "session_name", "") or "manual",
            "channel": args.channel or session_channel(video_path) or "stream",
            "t": float(args.at or 0),
            "dur": float(args.dur) if args.dur else 0.0,
            "score": None,
            "reason": getattr(args, "reason", "") or "manuel",
        })
    else:
        session = Path(args.session)
        cand_path = session / "candidates.json"
        if not cand_path.exists():
            sys.exit(f"[hata] aday yok, once detect: {cand_path}")
        cand = json.loads(cand_path.read_text(encoding="utf-8"))
        video_path = (session / cand["video"]).resolve()
        if not video_path.exists():
            sys.exit(f"[hata] kayit dosyasi yok: {video_path}")
        meta_path = session / "session.json"
        channel = cand.get("channel") or ""
        if not channel and meta_path.exists():
            channel = json.loads(meta_path.read_text(encoding="utf-8")).get("channel", "")
        limit = int(args.top or len(cand["items"]))
        for c in cand["items"][:limit]:
            jobs.append({
                "video": video_path,
                "session": session.name,
                "channel": channel,
                "t": float(c["t"]),
                "dur": float(c.get("dur") or default_dur),
                "score": c.get("score"),
                "reason": c.get("reason", ""),
            })

    if not jobs:
        sys.exit("[hata] kesilecek bir sey yok")

    made = []
    size_cache = {}
    dur_cache = {}
    for idx, job in enumerate(jobs, 1):
        # kaydin sonunu gecen istek sonsuza kadar bekliyordu (ffmpeg bos girdiyle kilitleniyor):
        # baslangici/sureyi kaydin icine kirp
        if job["video"] not in dur_cache:
            dur_cache[job["video"]] = video_duration(job["video"], cfg)
        total = dur_cache[job["video"]]
        if total > 0:
            if job["t"] >= total - 0.5:
                new_t = max(0.0, total - (job["dur"] or default_dur))
                print(f"  [uyari] {job['t']:.1f}s kaydin sonunu ({total:.1f}s) gecti -> {new_t:.1f}s'den kesiliyor")
                job["t"] = new_t
            if job["dur"] and job["t"] + job["dur"] > total:
                job["dur"] = max(0.5, total - job["t"])
            if job["t"] >= total - 0.2:
                print(f"  [hata] {job['session']} @ {job['t']:.1f}s: kayitta kesilecek goruntu yok")
                continue
        out_dir = clips_root / job["session"] / f"{idx:02d}_{int(job['t']):05d}s"
        if (out_dir / "clip.mp4").exists():
            out_dir = out_dir.with_name(f"{out_dir.name}_{stamp()[-6:]}")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "clip.mp4"
        print(f"\n[{idx}/{len(jobs)}] {job['session']} @ {job['t']:.1f}s ({job['dur']:.0f}s) {job['reason']}")

        cmd = ["-y", "-loglevel", "error"]
        if cfg_get(cfg, "video.logo", ""):
            cmd += ["-i", str(Path(logo).resolve())]
        cmd += ["-ss", f"{job['t']:.3f}", "-i", str(job["video"])]
        # NOT: -t burada verilirse sonraki -i'ye (ses efekti) input option olarak baglaniyor,
        # o yuzden sure siniri asagida cikti tarafinda veriliyor.

        # logo varsa giriş sırası: [0]=logo (ilk -i), [1]=video
        if cfg_get(cfg, "video.logo", ""):
            video_index, logo_index = 1, 0
        else:
            video_index, logo_index = 0, None
        # webcam: oturumda bir kez bulunur, klibin kosesine kucuk pencere olarak basilir
        facecam = None
        if not getattr(args, "no_facecam", False):
            fc = facecam_rect(job["video"], cfg)
            if fc and not crop_keeps_facecam(fc, mode):
                facecam = fc
        # kamera ust kosedeyse kanca yazisi altina insin (ustuste binmesin)
        reserve_top = 0
        if facecam:
            fc_pos = str(cfg_get(cfg, "facecam.position", "auto") or "auto").lower()
            if fc_pos in ("auto", "top-left", "top-right"):
                ow = int(cfg_get(cfg, "video.width", 1080) * float(cfg_get(cfg, "facecam.size", 0.34)))
                ow = max(120, ow - ow % 2)
                oh = int(round(ow * float(facecam["h"]) / float(facecam["w"] or 1)))
                reserve_top = (int(cfg_get(cfg, "facecam.margin", 40)) + oh
                               + 2 * int(cfg_get(cfg, "facecam.border", 4)))
        if mode == "stack":
            reserve_top = 0   # kameram karti ust kosede degil, kanca en ustte kalabilir

        ass_name = None
        chunks = None
        if subs_on:
            # altyazı için önce sessiz geçici klip üretmek gerekmez:
            # transkripsiyonu doğrudan kaynak penceresi üzerinden yapıyoruz
            chunks, tr_status = transcribe_window(job["video"], job["t"], job["dur"] or default_dur, cfg)
            # konusmasiz (menu/gecis) anlar klibe donusmesin; backend hatasinda karar veremiyoruz, devam
            if min_words > 0 and tr_status in ("ok", "silent"):
                n_words = sum(len((c.get("text") or "").split()) for c in (chunks or []))
                if n_words < min_words:
                    print(f"  [atla] @ {job['t']:.1f}s: {n_words} kelime (< {min_words}) -> konusma yok, klip yapilmadi")
                    for leftover in (out_dir, out_dir.parent):
                        try:
                            leftover.rmdir()   # bos klasor kalmasin (diger klipler varsa rmdir hata verir)
                        except OSError:
                            pass
                    continue
            if chunks:
                hook_banner = (args.title or "").strip() or chunks[0]["text"].title()
                write_ass(chunks, out_dir / "captions.ass", cfg, hook_text=hook_banner,
                          reserve_top=reserve_top)
                write_srt(chunks, out_dir / "captions.srt")
                ass_name = "captions.ass"

        dur_total = float(job["dur"] or default_dur)
        if job["video"] not in size_cache:
            size_cache[job["video"]] = video_size(job["video"], cfg)
        src_size = size_cache[job["video"]]
        filt = build_filter_with_index(mode, cfg, ass_name, video_index, logo_index,
                                       facecam, dur=dur_total, src_size=src_size)
        # ses efektleri: boom (kanca) + whoosh (sona dogru)
        sfx_inputs = {}
        if bool(cfg_get(cfg, "sfx.enabled", True)):
            nxt = 1 + max(video_index, logo_index or 0)
            for sfx_name in ("boom", "whoosh"):
                sfx_file = ensure_sfx(cfg, sfx_name)
                if not sfx_file:
                    continue
                cmd += ["-i", str(sfx_file)]
                sfx_inputs[sfx_name] = nxt
                nxt += 1
        audio_parts, audio_map = audio_graph(cfg, job["video"], video_index,
                                             job["t"], dur_total, sfx_inputs)
        cmd += ["-filter_complex", ";".join([filt] + audio_parts),
                "-map", "[vout]", "-map", audio_map]
        if job["dur"]:
            cmd += ["-t", f"{job['dur']:.3f}"]
        cmd += ["-c:v", "libx264", "-preset", str(cfg_get(cfg, "video.preset", "veryfast")),
                "-crf", str(cfg_get(cfg, "video.crf", 20)), "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k", "-ac", "2", "-ar", "48000",
                "-movflags", "+faststart", str(out_file)]

        res = run_ff(cmd, cfg, desc=f"render -> {out_file.name}", cwd=out_dir)
        if res.returncode != 0 and ass_name:
            print("  [uyari] altyazi ile render basarisiz (libass yok olabilir), altyazisiz tekrar deneniyor")
            (out_dir / "captions.ass").unlink(missing_ok=True)
            filt = build_filter_with_index(mode, cfg, None, video_index, logo_index,
                                           facecam, dur=dur_total, src_size=src_size)
            cmd[cmd.index("-filter_complex") + 1] = ";".join([filt] + audio_parts)
            res = run_ff(cmd, cfg, desc=f"render (altyazisiz) -> {out_file.name}", cwd=out_dir)
        if res.returncode != 0 and sfx_inputs:
            print("  [uyari] ses efekti eklenemedi, efektsiz tekrar deneniyor")
            audio_parts, audio_map = audio_graph(cfg, job["video"], video_index,
                                                 job["t"], dur_total, {})
            cmd[cmd.index("-filter_complex") + 1] = ";".join([filt] + audio_parts)
            cmd[cmd.index("[vout]") + 2] = audio_map
            res = run_ff(cmd, cfg, desc=f"render (efektsiz) -> {out_file.name}", cwd=out_dir)
        if res.returncode != 0 or not out_file.exists() or out_file.stat().st_size == 0:
            print("  [hata] render basarisiz")
            out_file.unlink(missing_ok=True)
            for leftover in ("captions.ass", "captions.srt"):
                (out_dir / leftover).unlink(missing_ok=True)
            try:
                out_dir.rmdir()   # bos klasor kalmasin (yarim klipler kuyruga girmesin)
            except OSError:
                pass
            continue

        # kapak
        run_ff(["-y", "-loglevel", "error", "-ss", "1.2", "-i", str(out_file), "-frames:v", "1",
                "-q:v", "3", str(out_dir / "cover.jpg")], cfg, cwd=out_dir)

        hook = chunks[0]["text"].title() if chunks else ""
        title = args.title or (hook[:70] if hook else f"{job['channel'].title()} | GTA 6")
        (out_dir / "caption.txt").write_text(caption_block(cfg, title, job["channel"], hook), encoding="utf-8")
        meta = {**{k: (str(v) if isinstance(v, Path) else v) for k, v in job.items()},
                "clip": str(out_file.relative_to(ROOT)), "title": title, "mode": mode}
        (out_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

        append_queue(clips_root / "queue.csv", meta)
        made.append(out_file)
        size_mb = out_file.stat().st_size / 1_048_576
        print(f"  ok: {out_file}  ({size_mb:.1f} MB)")

    print(f"\n  {len(made)} klip hazir -> {clips_root}")
    if made:
        print(f"  liste: python clipbot.py queue")
    return made


def build_filter_with_index(mode, cfg, ass_name, video_index, logo_index, facecam=None, dur=0.0,
                            src_size=(0, 0)):
    w = int(cfg_get(cfg, "video.width", 1080))
    h = int(cfg_get(cfg, "video.height", 1920))
    fps = int(cfg_get(cfg, "video.fps", 30))
    v = f"[{video_index}:v]"
    pre = ""
    src = v
    if mode == "stack" and not src_size[0]:
        mode = "fill"   # kaynak boyutu bilinmiyorsa panel hesabi yapilamaz
    split_out = ["[fmain]"]
    stack_cam = False   # stack modunda kamera karti yerlestirildiyse PIP tekrar basilmasin
    if facecam and mode != "original":
        split_out.append("[fcsrc]")
    if mode == "stack":
        split_out.append("[bgsrc]")  # bulanik zemin: kirpilmamis, renkli goruntuden
    if len(split_out) > 1:
        # ayni girdiden dallar: klip / webcam penceresi / bulanik zemin
        pre = f"{v}split={len(split_out)}" + "".join(split_out) + ";"
        src = "[fmain]"
        if "[bgsrc]" not in split_out:
            bg_src = "[fcsrc]"
        else:
            bg_src = "[bgsrc]"
    else:
        facecam = None
        bg_src = v
    # yayinin kendi olu alanlarini (tarayici cubugu, gorev cubugu, bos zemin) kirp:
    # oran olarak verilir, 0 = kirpma yok
    trims = [max(0.0, min(0.45, float(cfg_get(cfg, f"video.trim_{k}", 0.0) or 0)))
             for k in ("top", "bottom", "left", "right")]
    if mode != "original" and any(trims):
        t_top, t_bot, t_left, t_right = trims
        src += (f"crop=w='trunc(iw*{1 - t_left - t_right:.4f}/2)*2'"
                f":h='trunc(ih*{1 - t_top - t_bot:.4f}/2)*2'"
                f":x='iw*{t_left:.4f}':y='ih*{t_top:.4f}',")
    blur_bg = (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
               f"gblur=sigma=44,eq=brightness=-0.26:saturation=1.05")
    if mode == "stack":
        # oyun ustte tam genislikte panel, altinda kamera karti, en altta altyazi
        # (Shorts/Reels arayuzu alt ~%15'i kapatiyor: orayi bos birakiyoruz)
        a_top, a_bot, a_left, a_right = trims
        tw = max(2, int(src_size[0] * (1 - a_left - a_right)) // 2 * 2)
        th = max(2, int(src_size[1] * (1 - a_top - a_bot)) // 2 * 2)
        gw = w
        gh = int(round(th * gw / tw) / 2) * 2
        cap = int(h * float(cfg_get(cfg, "video.stack_max_game", 0.72))) // 2 * 2
        if gh > cap:
            gh = cap
            gw = int(round(tw * gh / th) / 2) * 2
        gap = int(cfg_get(cfg, "video.stack_gap", 24))
        cam = ""
        cam_w = 0
        if facecam:
            fw = max(2.0, float(facecam["w"]))
            fh = max(2.0, float(facecam["h"]))
            cam_w = int(w * float(cfg_get(cfg, "video.cam_size", 0.66))) // 2 * 2
            cam_h = int(round(cam_w * fh / fw) / 2) * 2
            room = h - gh - gap - int(h * 0.16)   # altta altyazi icin yer kalsin
            if room < 160:
                cam_w = 0
            elif cam_h > room:
                cam_w = max(240, int(round(cam_w * room / cam_h)) // 2 * 2)
                cam_h = int(round(cam_w * fh / fw) / 2) * 2
            if cam_w:
                stack_cam = True
                border = max(0, int(cfg_get(cfg, "facecam.border", 4)))
                cam = (f"[fcsrc]crop={int(facecam['w'])}:{int(facecam['h'])}:{int(facecam['x'])}"
                       f":{int(facecam['y'])},scale={cam_w}:{cam_h}:flags=lanczos,fps={fps},setsar=1")
                if border:
                    cam += f",pad=iw+{2 * border}:ih+{2 * border}:{border}:{border}:color=white@0.9"
                    cam_w += 2 * border
                cam += "[fcard]"
        chain = pre + (
            f"{bg_src}{blur_bg}[bg];"
            f"{src}scale={gw}:{gh}:flags=lanczos[game];"
            f"[bg][game]overlay=(W-w)/2:0[g0]"
        )
        last = "g0"
        if cam_w:
            chain += f";{cam};[g0][fcard]overlay=(W-w)/2:{gh + gap}[gcam]"
            last = "gcam"
        chain += f";[{last}]fps={fps},setsar=1[v0]"
    elif mode == "fill":
        # oyunu panel olarak ortalar, kalan yuksekligi bulanik dolguyla kapatir
        a = float(cfg_get(cfg, "video.panel_aspect", 1.2)) or 1.2
        chain = pre + (
            f"{src}crop=w='min(iw,ih*{a:.4f})':h='min(ih,iw/{a:.4f})'"
            f":x='(iw-ow)/2':y='(ih-oh)/2',split=2[bgs][fgs];"
            f"[bgs]{blur_bg}[bg];"
            f"[fgs]scale={w}:-2:flags=lanczos[sg];"
            f"[bg][sg]overlay=(W-w)/2:(H-h)/2,fps={fps},setsar=1[v0]"
        )
    elif mode == "blurpad":
        chain = pre + (
            f"{src}split=2[bgs][fgs];"
            f"[bgs]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},gblur=sigma=28,"
            f"eq=brightness=-0.05[bg];"
            f"[fgs]scale={w}:-2:flags=lanczos[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps={fps},setsar=1[v0]"
        )
    elif mode == "original":
        chain = pre + f"{src}scale=1920:-2:flags=lanczos,fps={fps},setsar=1[v0]"
    else:
        anchor = {"crop_left": "0", "crop_right": "iw-ow"}.get(mode, "(iw-ow)/2")
        chain = pre + (
            f"{src}crop=w='min(iw,ih*9/16)':h='min(ih,iw*16/9)':x='{anchor}':y='(ih-oh)/2',"
            f"scale={w}:{h}:flags=lanczos,fps={fps},setsar=1[v0]"
        )
    last = "v0"
    if logo_index is not None:
        chain += f";[{logo_index}:v]scale=160:-1[wm];[{last}][wm]overlay=W-w-36:H-h-36[v1]"
        last = "v1"
    if facecam and not stack_cam:
        pos = str(cfg_get(cfg, "facecam.position", "auto") or "auto").lower()
        if pos not in ("top-left", "top-right", "bottom-left", "bottom-right"):
            pos = "top-right"
        pw = int(round(w * float(cfg_get(cfg, "facecam.size", 0.34))))
        pw = max(120, pw - pw % 2)
        border = max(0, int(cfg_get(cfg, "facecam.border", 4)))
        margin = int(cfg_get(cfg, "facecam.margin", 40))
        cam = (f"[fcsrc]crop={int(facecam['w'])}:{int(facecam['h'])}:{int(facecam['x'])}"
               f":{int(facecam['y'])},scale={pw}:-2:flags=lanczos,fps={fps},setsar=1")
        if border:
            cam += f",pad=iw+{2 * border}:ih+{2 * border}:{border}:{border}:color=white@0.9"
        chain += f";{cam}[fcam]"
        ox = f"main_w-overlay_w-{margin}" if pos.endswith("right") else f"{margin}"
        oy = f"{margin}" if pos.startswith("top") else f"main_h-overlay_h-{margin}"
        chain += f";[{last}][fcam]overlay={ox}:{oy}[fcamv]"
        last = "fcamv"
    if ass_name:
        chain += f";[{last}]ass={ass_name}[vas]"
        last = "vas"
    # alt kenarda ince ilerleme cubugu: videoda bir yere gidildigi hissi izlemeyi uzatiyor
    if bool(cfg_get(cfg, "video.progress_bar", True)) and dur > 1:
        bh = max(2, int(cfg_get(cfg, "video.progress_height", 8)))
        col = str(cfg_get(cfg, "video.progress_color", "0xFFC800"))
        # drawbox genisligi kare kare yenilenmedigi icin: cubugu soldan kaydiriyoruz
        chain += (f";color=c={col}:s={w}x{bh}:r={fps}[barcol];"
                  f"[{last}][barcol]overlay=x='-w+W*t/{dur:.3f}':y=H-{bh}[vbar]")
        last = "vbar"
    # sonda yumusak kararma: klip "bitmis" hissi versin, sonuna kadar izlensin
    fade = float(cfg_get(cfg, "video.fade_seconds", 0.3))
    if fade > 0 and dur > fade + 0.6:
        chain += f";[{last}]fade=t=out:st={max(0.0, dur - fade):.2f}:d={fade:.2f}[vout]"
    else:
        chain += f";[{last}]null[vout]"
    return chain


def transcribe_window(video, t, dur, cfg):
    """Kaynaktan pencereyi sessizce çıkarıp yazıya döker (klip render'ından ayrı)."""
    tmp = ROOT / ".tmp_audio"
    tmp.mkdir(exist_ok=True)
    wav = tmp / f"win_{int(t)}_{int(dur)}.m4a"
    res = run_ff(["-y", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(video),
                  "-t", f"{dur:.3f}", "-vn", "-c:a", "aac", "-b:a", "96k", str(wav)], cfg, capture=True)
    if res.returncode != 0 or not wav.exists():
        return None
    words = None
    status = "error"
    backend = cfg_get(cfg, "subs.backend", "auto")
    try:
        local_ok = False
        if backend in ("auto", "local"):
            try:
                import faster_whisper  # type: ignore  # noqa: F401
                local_ok = True
            except Exception:
                local_ok = False
        if local_ok:
            model_name = cfg_get(cfg, "subs.model", "small")
            device = cfg_get(cfg, "subs.device", "auto")
            model = _whisper_model(model_name, device)
            lang = cfg_get(cfg, "subs.language", "") or None
            print(f"  [altyazi] yerel whisper {model_name}...")
            segments, _ = model.transcribe(str(wav), language=lang, word_timestamps=True,
                                           vad_filter=True, beam_size=1)
            words = []
            for seg in segments:
                for w in seg.words or []:
                    text = (w.word or "").strip()
                    if text:
                        words.append({"start": float(w.start), "end": float(w.end), "text": text})
            status = "ok" if words else "silent"
        if not words and backend in ("auto", "groq"):
            groq_key = os.environ.get("GROQ_API_KEY") or cfg_get(cfg, "subs.api_key", "")
            if not groq_key:
                status = "silent" if status == "silent" else "unavailable"
            else:
                print("  [altyazi] groq whisper...")
                got = transcribe_groq(wav, cfg)
                if got is None:
                    status = "error"
                else:
                    words = got
                    status = "ok" if words else "silent"
        if not words:
            print("  [altyazi] bos sonuc -> altyazisiz render (sessiz bolum ya da backend yok)")
    except Exception as exc:  # noqa: BLE001
        print(f"  [altyazi] basarisiz: {exc}")
        words = None
    finally:
        wav.unlink(missing_ok=True)
    if not words:
        return None, status
    return group_words(words, int(cfg_get(cfg, "subs.max_words", 3))), "ok"


QUEUE_FIELDS = ["clip", "session", "channel", "at", "score", "title",
                "status", "posted_at", "url",
                "tiktok", "tiktok_url", "instagram", "instagram_url"]


def append_queue(queue_path, meta):
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    if queue_path.exists():
        with queue_path.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        clip_key = str(meta.get("clip", "")).replace("\\", "/")
        for row in rows:
            if str(row.get("clip", "")).replace("\\", "/") == clip_key:
                print("  [kuyruk] bu klip zaten listede, tekrar eklenmedi")
                return
    rows.append({"clip": meta.get("clip", ""), "session": meta.get("session", ""),
                 "channel": meta.get("channel", ""), "at": meta.get("t", ""),
                 "score": meta.get("score", ""), "title": meta.get("title", ""),
                 "status": "pending", "posted_at": "", "url": ""})
    # baslik her zaman tam: mark_queue_clip 'url' eklediginde satirlar eksik kalmasin
    with queue_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=QUEUE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in QUEUE_FIELDS})


# --------------------------------------------------------------------------- #
# 4) queue / mark / doctor
# --------------------------------------------------------------------------- #
def cmd_queue(args, cfg):
    path = ROOT / cfg_get(cfg, "paths.clips", "clips") / "queue.csv"
    if not path.exists():
        sys.exit("[bilgi] henuz klip yok")
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    plats = platform_list(cfg)

    def status_of(row, plat):
        return str(row.get("status" if plat == "youtube" else plat) or "").strip()

    # numaralar her zaman tam liste uzerinden: 'mark <#>' filtrelenmis listeyle kaymasin
    numbered = [(i, r) for i, r in enumerate(rows, 1)
                if not args.status or status_of(r, "youtube") == args.status]
    if not numbered:
        print("  (bos)")
        return
    pending_total = sum(1 for r in rows if any(status_of(r, p) != "posted" for p in plats))
    print(f"{'#':>3}  {'youtube':<9} {'tiktok':<9} {'instagram':<9} {'kanal':<14} klip")
    for i, r in numbered:
        print(f"{i:>3}  {(status_of(r, 'youtube') or '-'):<9} {(status_of(r, 'tiktok') or '-'):<9} "
              f"{(status_of(r, 'instagram') or '-'):<9} {(r.get('channel') or '')[:14]:<14} {r.get('clip', '')}")

    print(f"\n  {pending_total} klip [{','.join(plats)}] icin yuklemeyi bekliyor. "
          f"Isaretle: python clipbot.py mark <#> posted")


def cmd_mark(args, cfg):
    path = ROOT / cfg_get(cfg, "paths.clips", "clips") / "queue.csv"
    if not path.exists():
        sys.exit("[hata] queue.csv yok")
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    idx = int(args.index) - 1
    if idx < 0 or idx >= len(rows):
        sys.exit("[hata] gecersiz sira")
    rows[idx]["status"] = args.status
    rows[idx]["posted_at"] = time.strftime("%Y-%m-%d %H:%M")
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"  ok: {rows[idx]['clip']} -> {args.status}")


def cmd_doctor(args, cfg):
    print("clipbot doctor\n" + "-" * 46)
    print(f"python      : {sys.version.split()[0]}")
    ff = ffmpeg_bin(cfg)
    print(f"ffmpeg      : {ff}")
    try:
        p = subprocess.run([ff, "-version"], capture_output=True, text=True, timeout=20)
        print(f"  surum     : {p.stdout.splitlines()[0][:70] if p.stdout else 'okunamadi'}")
        f = subprocess.run([ff, "-filters"], capture_output=True, text=True, timeout=30)
        has_ass = " ass " in (f.stdout or "") or "subtitles" in (f.stdout or "")
        print(f"  altyazi   : {'var (libass)' if has_ass else 'YOK -> altyazi yakilamaz, .srt saklanir'}")
        e = subprocess.run([ff, "-encoders"], capture_output=True, text=True, timeout=30)
        print(f"  libx264   : {'var' if 'libx264' in (e.stdout or '') else 'YOK'}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [hata] ffmpeg calismadi: {exc}")
    for tool, module in (("streamlink", "streamlink"), ("yt-dlp", "yt_dlp")):
        argv = tool_argv(tool, module)
        print(f"{tool:<12}: {' -> '.join(argv) if argv else 'yok (pip install ' + tool + ')'}")
    try:
        import faster_whisper  # type: ignore  # noqa: F401
        print("faster-whisper: var (yerel altyazi)")
    except Exception:
        print("faster-whisper: yok (pyth 3.15'te kurulamiyor -> Groq kullan)")
    try:
        import cv2  # type: ignore
        print(f"facecam     : var (opencv {cv2.__version__}) -> webcam klibe PIP basilir")
    except Exception:
        print("facecam     : yok -> pip install opencv-python-headless numpy (PIP kapanir)")
    groq = os.environ.get("GROQ_API_KEY") or cfg_get(cfg, "subs.api_key", "")
    print(f"GROQ_API_KEY: {'var (altyazi Groq ile)' if groq else 'YOK -> altyazi devre disi'}")
    conf = yt_client_config(cfg)
    _, token_path = yt_paths(cfg)
    if conf and token_path.exists():
        print("youtube     : hazir (token var)")
    elif conf:
        print("youtube     : client var, token yok -> python clipbot.py auth-youtube")
    else:
        print("youtube     : client_secret.json yok -> yukleme elle")
    tkey, tsecret = tt_creds(cfg)
    if tkey and tsecret and tt_token_file(cfg).exists():
        print("tiktok      : hazir (client + token)")
    elif tkey and tsecret:
        print("tiktok      : client var, token yok -> python clipbot.py auth-tiktok")
    else:
        print("tiktok      : client yok -> docs/API-KURULUM.md bolum 2")
    iapp, isecret = ig_creds(cfg)
    if iapp and isecret and ig_token_file(cfg).exists():
        print("instagram   : hazir (app + token)")
    elif iapp and isecret:
        print("instagram   : app var, token yok -> python clipbot.py auth-instagram")
    else:
        print("instagram   : app yok -> docs/API-KURULUM.md bolum 3")
    print(f"platformlar : {','.join(platform_list(cfg))}")
    free_gb = shutil.disk_usage(ROOT).free / 1_073_741_824
    print(f"disk        : {free_gb:.0f} GB bos  (1080p60 ~4-6 GB/saat)")
    print(f"config      : ffmpeg={cfg_get(cfg, 'paths.ffmpeg', '')!r} mod={cfg_get(cfg, 'video.mode')}")


# --------------------------------------------------------------------------- #
# 5) YouTube yukleme (OAuth + resumable upload, sadece stdlib)
# --------------------------------------------------------------------------- #
YT_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
YT_TOKEN = "https://oauth2.googleapis.com/token"
YT_UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
YT_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


def yt_paths(cfg):
    client = Path(str(cfg_get(cfg, "upload.client", "client_secret.json")))
    token = Path(str(cfg_get(cfg, "upload.token", "youtube_token.json")))
    return (client if client.is_absolute() else ROOT / client,
            token if token.is_absolute() else ROOT / token)


def yt_client_config(cfg):
    client_path, _ = yt_paths(cfg)
    if not client_path.exists():
        return None
    try:
        data = json.loads(client_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    conf = data.get("installed") or data.get("web") or {}
    return conf if conf.get("client_id") else None


def _http(req, timeout=120):
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, dict(resp.headers), resp.read()


def yt_access_token(cfg):
    """Kayitli token'i dondurur; suresi dolmussa refresh_token ile yeniler."""
    conf = yt_client_config(cfg)
    _, token_path = yt_paths(cfg)
    if not conf:
        raise RuntimeError("client_secret.json yok -> README'deki 4 adim")
    if not token_path.exists():
        raise RuntimeError("youtube_token.json yok -> python clipbot.py auth-youtube")
    tok = json.loads(token_path.read_text(encoding="utf-8"))
    if tok.get("access_token") and tok.get("expires_at", 0) > time.time() + 120:
        return tok["access_token"]
    if not tok.get("refresh_token"):
        raise RuntimeError("refresh_token yok -> python clipbot.py auth-youtube")
    data = urllib.parse.urlencode({
        "client_id": conf["client_id"], "client_secret": conf["client_secret"],
        "refresh_token": tok["refresh_token"], "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(YT_TOKEN, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        _, _, body = _http(req)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"token yenilenemedi (HTTP {exc.code}): {exc.read()[:200]!r}") from exc
    fresh = json.loads(body)
    tok["access_token"] = fresh["access_token"]
    tok["expires_at"] = time.time() + float(fresh.get("expires_in", 3600)) - 120
    token_path.write_text(json.dumps(tok, indent=2), encoding="utf-8")
    return tok["access_token"]


def cmd_auth_youtube(args, cfg):
    conf = yt_client_config(cfg)
    client_path, token_path = yt_paths(cfg)
    if not conf:
        print("[hata] client_secret.json bulunamadi. tek seferlik kurulum:\n")
        print("  1) https://console.cloud.google.com -> yeni proje ac")
        print("  2) APIs & Services -> Library -> 'YouTube Data API v3' -> Enable")
        print("  3) OAuth consent screen -> External, kendini 'test user' ekle")
        print("  4) Credentials -> Create credentials -> OAuth client ID -> Desktop app -> JSON indir")
        print(f"  5) dosyayi buraya koy: {client_path}\n")
        sys.exit(1)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(16)
    port = int(cfg_get(cfg, "upload.oauth_port", 8765))
    redirect = f"http://127.0.0.1:{port}/"
    url = f"{YT_AUTH}?" + urllib.parse.urlencode({
        "client_id": conf["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": YT_SCOPE, "code_challenge": challenge, "code_challenge_method": "S256",
        "state": state, "access_type": "offline", "prompt": "consent",
    })
    got = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>clipbot: yetki alindi, sekmeyi kapatabilirsin.</h2>".encode("utf-8"))

        def log_message(self, *a):
            return

    try:
        srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        sys.exit(f"[hata] port {port} kullanilamiyor: {exc}  (config: upload.oauth_port)")
    srv.timeout = 300
    threading.Thread(target=srv.handle_request, daemon=True).start()
    print(f"  tarayici aciliyor. acilmazsa linke tikla:\n\n  {url}\n")
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    deadline = time.time() + 310
    while not got and time.time() < deadline:
        time.sleep(0.4)
    srv.server_close()
    if not got:
        sys.exit("[hata] 5 dakika icinde yetki gelmedi")
    if got.get("state") != state:
        sys.exit("[hata] state uyusmadi, tekrar dene")
    if "code" not in got:
        sys.exit(f"[hata] yetki verilmedi: {got.get('error', got)}")
    data = urllib.parse.urlencode({
        "client_id": conf["client_id"], "client_secret": conf["client_secret"],
        "code": got["code"], "code_verifier": verifier,
        "grant_type": "authorization_code", "redirect_uri": redirect,
    }).encode()
    req = urllib.request.Request(YT_TOKEN, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        _, _, body = _http(req)
    except urllib.error.HTTPError as exc:
        sys.exit(f"[hata] token alinamadi (HTTP {exc.code}): {exc.read()[:300]!r}")
    tok = json.loads(body)
    if not tok.get("refresh_token"):
        print("  [uyari] refresh_token gelmedi; yetkiyi kaldirip tekrar dene")
    tok["expires_at"] = time.time() + float(tok.get("expires_in", 3600)) - 120
    token_path.write_text(json.dumps(tok, indent=2), encoding="utf-8")
    print(f"  ok: {token_path}")
    print("  artik: python clipbot.py upload  (ya da auto --upload)")


def yt_upload(clip, title, description, tags, cfg, privacy=None):
    """Resumable upload: oturum ac, tek PUT ile gonder."""
    privacy = privacy or str(cfg_get(cfg, "upload.privacy", "public"))
    token = yt_access_token(cfg)
    clip = Path(clip)
    size = clip.stat().st_size
    body = {
        "snippet": {
            "title": (title or clip.parent.name)[:100],
            "description": (description or "")[:4900],
            "tags": tags or [],
            "categoryId": str(cfg_get(cfg, "upload.category", "20")),
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    req = urllib.request.Request(
        YT_UPLOAD + "?uploadType=resumable&part=snippet,status",
        data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Length": str(size),
                 "X-Upload-Content-Type": "video/mp4"},
    )
    try:
        _, resp_headers, _ = _http(req)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"oturum acilamadi (HTTP {exc.code}): {exc.read()[:400]!r}") from exc
    location = resp_headers.get("Location") or resp_headers.get("location")
    if not location:
        raise RuntimeError("YouTube upload oturumu dondurmedi (Location yok)")
    put = urllib.request.Request(
        location, data=clip.read_bytes(), method="PUT",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4",
                 "Content-Length": str(size)},
    )
    try:
        _, _, body_bytes = _http(put, timeout=1800)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"yukleme basarisiz (HTTP {exc.code}): {exc.read()[:400]!r}") from exc
    info = json.loads(body_bytes or b"{}")
    video_id = info.get("id")
    if not video_id:
        raise RuntimeError(f"YouTube video id dondurmedi: {info}")
    return video_id, info


def queue_path(cfg):
    return ROOT / cfg_get(cfg, "paths.clips", "clips") / "queue.csv"


def mark_queue_clip(cfg, clip_path, status, url="", platform="youtube"):
    """Klip durumunu platform bazinda isaretler (youtube: status/url, digerleri: <plat>/<plat>_url)."""
    path = queue_path(cfg)
    if not path.exists():
        return
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    if not rows:
        return
    status_key = "status" if platform == "youtube" else platform
    url_key = "url" if platform == "youtube" else f"{platform}_url"
    fields = list(dict.fromkeys(list(rows[0].keys()) + QUEUE_FIELDS + [status_key, url_key]))
    want = Path(clip_path)
    try:
        want_rel = str(want.resolve().relative_to(ROOT))
    except ValueError:
        want_rel = str(want)
    hit = False
    for row in rows:
        row.setdefault(status_key, "")
        row.setdefault(url_key, "")
        if str(row.get("clip", "")).replace("\\", "/") == want_rel.replace("\\", "/"):
            row[status_key] = status
            row[url_key] = url or row.get(url_key, "")
            row["posted_at"] = time.strftime("%Y-%m-%d %H:%M")
            hit = True
    if not hit:
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def upload_clip(clip_path, cfg, privacy=None, dry_run=False, platform="youtube"):
    clip_path = Path(clip_path)
    if not clip_path.exists():
        raise RuntimeError(f"klip yok: {clip_path}")
    caption_file = clip_path.parent / "caption.txt"
    text = caption_file.read_text(encoding="utf-8").strip() if caption_file.exists() else ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    title = lines[0] if lines else clip_path.parent.name
    desc = text or title
    if platform == "tiktok":
        # TikTok tek satir baslik/"description" alani kabul ediyor
        flat = re.sub(r"\s+", " ", " ".join(lines) or title).strip()
        res = tt_publish(clip_path, cfg, flat, privacy=privacy, dry_run=dry_run)
        if dry_run or not res:
            return None
        status, data = tt_status(cfg, res["publish_id"])
        if status not in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
            raise RuntimeError(f"tiktok durum: {status} {data.get('fail_reason', '')}".strip())
        print(f"    tiktok: {status} ({res['mode']})" + (f" -> {res['url']}" if res.get("url") else ""))
        mark_queue_clip(cfg, clip_path, "posted", res.get("url", ""), platform="tiktok")
        return res["publish_id"]
    if platform == "instagram":
        url = ig_publish(clip_path, cfg, desc, dry_run=dry_run)
        if dry_run or not url:
            return None
        print(f"    instagram: {url}")
        mark_queue_clip(cfg, clip_path, "posted", url, platform="instagram")
        return url
    tags = [t for t in re.findall(r"#(\w+)", text)]
    extra = str(cfg_get(cfg, "upload.extra_tags", "") or "")
    tags += [t.strip() for t in extra.split(",") if t.strip()]
    if dry_run:
        print(f"    [dry-run][youtube] {clip_path.parent.name}: baslik={title!r} "
              f"etiket={tags[:8]} gizlilik={privacy or cfg_get(cfg, 'upload.privacy', 'public')}")
        return None
    video_id, info = yt_upload(clip_path, title, desc, tags[:30], cfg, privacy)
    applied = (info.get("status") or {}).get("privacyStatus", "?")
    url = f"https://youtube.com/shorts/{video_id}"
    print(f"    yuklendi: {url}  (gizlilik: {applied})")
    mark_queue_clip(cfg, clip_path, "posted", url)
    return video_id


def cmd_upload(args, cfg):
    clips_root = ROOT / cfg_get(cfg, "paths.clips", "clips")
    platforms = ready_platforms(cfg, platform_list(cfg, getattr(args, "platform", "") or ""))
    if not platforms:
        print("  hazir platform yok. kurulum: docs/API-KURULUM.md")
        return
    targets = []
    if args.clip:
        p = Path(args.clip)
        targets = [p if p.is_absolute() else ROOT / p]
    else:
        qp = clips_root / "queue.csv"
        if not qp.exists():
            sys.exit("[bilgi] kuyruk bos")
        for row in csv.DictReader(qp.open(encoding="utf-8")):
            if all(str(row.get("status" if p == "youtube" else p) or "").strip() == "posted"
                   for p in platforms):
                continue
            cp = Path(row.get("clip", ""))
            targets.append(cp if cp.is_absolute() else ROOT / cp)
    targets = [t for t in targets if t.exists()]
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        print("  yuklenecek klip yok")
        return
    print(f"  {len(targets)} klip | platform: {','.join(platforms)}"
          + (" (dry-run, yukleme yapilmaz)" if args.dry_run else ""))
    for t in targets:
        for plat in platforms:
            try:
                upload_clip(t, cfg, args.privacy or None, dry_run=args.dry_run, platform=plat)
            except Exception as exc:  # noqa: BLE001
                print(f"    [hata:{plat}] {t.parent.name}: {exc}")


# --------------------------------------------------------------------------- #
# 5b) TikTok / Instagram — Content Posting API + Instagram Reels API
# --------------------------------------------------------------------------- #
TT_AUTH = "https://www.tiktok.com/v2/auth/authorize/"
TT_TOKEN = "https://open.tiktokapis.com/v2/oauth/token/"
TT_API = "https://open.tiktokapis.com/v2"
TT_PRIVACY = {"public": "PUBLIC_TO_EVERYONE", "unlisted": "SELF_ONLY", "private": "SELF_ONLY"}

IG_AUTH = "https://www.instagram.com/oauth/authorize"
IG_CODE = "https://api.instagram.com/oauth/access_token"
IG_GRAPH = "https://graph.instagram.com"
IG_SCOPE = "instagram_business_basic,instagram_business_content_publish"

PLATFORMS = ("youtube", "tiktok", "instagram")


def platform_list(cfg, override=""):
    raw = (override or str(cfg_get(cfg, "upload.platforms", "youtube") or "youtube")).strip().lower()
    if raw in ("all", "hepsi"):
        return list(PLATFORMS)
    out = []
    for part in raw.replace(";", ",").split(","):
        name = part.strip().lower()
        if name in PLATFORMS and name not in out:
            out.append(name)
    return out or ["youtube"]


def platform_ready(cfg, plat):
    if plat == "youtube":
        if not yt_client_config(cfg):
            return False, "client_secret.json yok"
        if not yt_paths(cfg)[1].exists():
            return False, "youtube_token.json yok -> python clipbot.py auth-youtube"
        return True, ""
    if plat == "tiktok":
        key, secret = tt_creds(cfg)
        if not key or not secret:
            return False, "client_key/client_secret yok -> docs/API-KURULUM.md bolum 2"
        if not tt_token_file(cfg).exists():
            return False, "tiktok_token.json yok -> python clipbot.py auth-tiktok"
        return True, ""
    if plat == "instagram":
        app_id, secret = ig_creds(cfg)
        if not app_id or not secret:
            return False, "app_id/app_secret yok -> docs/API-KURULUM.md bolum 3"
        if not ig_token_file(cfg).exists():
            return False, "instagram_token.json yok -> python clipbot.py auth-instagram"
        return True, ""
    return False, "bilinmeyen platform"


def ready_platforms(cfg, plats):
    out = []
    for plat in plats:
        ok, why = platform_ready(cfg, plat)
        if ok:
            out.append(plat)
        else:
            print(f"  [atlandi:{plat}] {why}")
    return out


def _http_json(url, payload=None, token="", form=False, method=None, timeout=180):
    """JSON/form istegi; HTTP hatasini okunabilir RuntimeError'a cevirir."""
    headers = {"User-Agent": "clipbot/1.0 (+python-urllib)"}
    data = None
    if payload is not None:
        if form:
            data = urllib.parse.urlencode(payload).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=UTF-8"
    elif method in ("POST", "PUT"):
        data = b""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        _, _, body = _http(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'ignore')[:500]}") from exc
    return json.loads(body or b"{}")


def _code_from(value):
    """--code: kodu ya da tum geri donus URL'sini kabul et."""
    value = (value or "").strip()
    if "code=" in value:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(value).query)
        return (q.get("code") or [""])[0]
    return value


def oauth_wait(url, port, state, timeout=300):
    """Tarayiciyi acar, 127.0.0.1:<port> geri donusunu bekler."""
    got = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>clipbot: yetki alindi, sekmeyi kapatabilirsin.</h2>".encode("utf-8"))

        def log_message(self, *a):
            return

    try:
        srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        sys.exit(f"[hata] port {port} kullanilamiyor: {exc}")
    srv.timeout = timeout
    threading.Thread(target=srv.handle_request, daemon=True).start()
    print(f"  tarayici aciliyor. acilmazsa linke tikla:\n\n  {url}\n")
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    deadline = time.time() + timeout + 10
    while not got and time.time() < deadline:
        time.sleep(0.4)
    srv.server_close()
    if not got:
        sys.exit(f"[hata] {timeout // 60} dk icinde yetki gelmedi")
    if got.get("state") != state:
        sys.exit("[hata] state uyusmadi, tekrar dene")
    return got


def tt_creds(cfg):
    key = os.environ.get("TIKTOK_CLIENT_KEY") or str(cfg_get(cfg, "tiktok.client_key", "") or "")
    secret = os.environ.get("TIKTOK_CLIENT_SECRET") or str(cfg_get(cfg, "tiktok.client_secret", "") or "")
    return key.strip(), secret.strip()


def tt_token_file(cfg):
    p = Path(str(cfg_get(cfg, "tiktok.token", "tiktok_token.json")))
    return p if p.is_absolute() else ROOT / p


def tt_token(cfg):
    """Kayitli TikTok token'i; suresi dolmak uzereyse refresh_token ile yeniler."""
    key, secret = tt_creds(cfg)
    if not key or not secret:
        raise RuntimeError("tiktok client_key/client_secret yok -> docs/API-KURULUM.md, bolum 2")
    path = tt_token_file(cfg)
    if not path.exists():
        raise RuntimeError("tiktok_token.json yok -> python clipbot.py auth-tiktok")
    tok = json.loads(path.read_text(encoding="utf-8"))
    if tok.get("access_token") and tok.get("expires_at", 0) > time.time() + 120:
        return tok
    if not tok.get("refresh_token"):
        raise RuntimeError("tiktok refresh_token yok -> python clipbot.py auth-tiktok")
    fresh = _http_json(TT_TOKEN, {"client_key": key, "client_secret": secret,
                                  "grant_type": "refresh_token",
                                  "refresh_token": tok["refresh_token"]},
                       form=True, method="POST")
    if not fresh.get("access_token"):
        raise RuntimeError(f"tiktok token yenilenemedi: {str(fresh)[:300]}")
    tok.update({"access_token": fresh["access_token"],
                "refresh_token": fresh.get("refresh_token", tok.get("refresh_token")),
                "expires_at": time.time() + float(fresh.get("expires_in", 86400)) - 120,
                "open_id": fresh.get("open_id", tok.get("open_id", ""))})
    path.write_text(json.dumps(tok, indent=2), encoding="utf-8")
    return tok


def cmd_auth_tiktok(args, cfg):
    key, secret = tt_creds(cfg)
    if not key or not secret:
        print("[hata] TikTok client_key/client_secret yok. tek seferlik kurulum:\n")
        print("  1) https://developers.tiktok.com -> Manage apps -> Connect an app")
        print("  2) Products -> Content Posting API ekle, Direct Post'u ac")
        print("  3) Scopes: video.publish (dogrudan) ve/veya video.upload (taslak/inbox)")
        print("  4) Login Kit -> Redirect URI: http://127.0.0.1:8766/")
        print("  5) client_key / client_secret -> config.toml [tiktok]")
        print("     (ya da TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET ortam degiskeni)\n")
        sys.exit(1)
    scope = str(cfg_get(cfg, "tiktok.scope", "video.publish,video.upload"))
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(16)
    port = int(cfg_get(cfg, "tiktok.oauth_port", 8766))
    redirect = str(cfg_get(cfg, "tiktok.redirect_uri", "") or f"http://127.0.0.1:{port}/")
    url = TT_AUTH + "?" + urllib.parse.urlencode({
        "client_key": key, "scope": scope, "response_type": "code",
        "redirect_uri": redirect, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    manual = _code_from(getattr(args, "code", ""))
    got = {"code": manual, "state": state} if manual else oauth_wait(url, port, state)
    if not got.get("code"):
        sys.exit(f"[hata] yetki verilmedi: {got.get('error', got)}")
    tok = _http_json(TT_TOKEN, {"client_key": key, "client_secret": secret, "code": got["code"],
                                "grant_type": "authorization_code", "redirect_uri": redirect,
                                "code_verifier": verifier}, form=True, method="POST")
    if not tok.get("access_token"):
        sys.exit(f"[hata] token alinamadi: {str(tok)[:300]}")
    tok["expires_at"] = time.time() + float(tok.get("expires_in", 86400)) - 120
    path = tt_token_file(cfg)
    path.write_text(json.dumps(tok, indent=2), encoding="utf-8")
    print(f"  ok: {path}  (open_id {tok.get('open_id', '?')}, scope {tok.get('scope', scope)})")
    print("  not: denetlenmemis uygulamada public gonderim 403 verir -> "
          "[tiktok] privacy=\"SELF_ONLY\" ya da mode=\"inbox\"")


def tt_privacy_level(cfg, privacy=None):
    raw = str(privacy or cfg_get(cfg, "tiktok.privacy", "PUBLIC_TO_EVERYONE") or "").strip()
    return TT_PRIVACY.get(raw.lower(), raw.upper() or "PUBLIC_TO_EVERYONE")


def tt_publish(clip, cfg, title, privacy=None, dry_run=False):
    """Content Posting API: init -> upload_url'e PUT. Doner: {publish_id, mode, url}."""
    clip = Path(clip)
    mode = str(cfg_get(cfg, "tiktok.mode", "direct")).strip().lower()
    level = "SELF_ONLY" if mode == "inbox" else tt_privacy_level(cfg, privacy)
    size = clip.stat().st_size
    if dry_run:
        print(f"    [dry-run][tiktok] {clip.parent.name}: mod={mode} gizlilik={level} "
              f"boyut={size / 1_048_576:.1f} MB baslik={title[:60]!r}")
        return None
    if size > 64 * 1024 * 1024:
        raise RuntimeError("tiktok: tek parca yukleme 64 MB ile sinirli (klibi kucult)")
    tok = tt_token(cfg)
    token = tok["access_token"]
    payload = {"source_info": {"source": "FILE_UPLOAD", "video_size": size,
                                "chunk_size": size, "total_chunk_count": 1}}
    if mode == "inbox":
        url = f"{TT_API}/post/publish/inbox/video/init/"
    else:
        url = f"{TT_API}/post/publish/video/init/"
        payload["post_info"] = {"title": (title or "clip")[:2200], "privacy_level": level,
                                "disable_comment": False, "disable_duet": False,
                                "disable_stitch": False, "video_cover_timestamp_ms": 1200}
    res = _http_json(url, payload, token=token, method="POST")
    data = res.get("data") or {}
    err = res.get("error") or {}
    if err.get("code", "ok") != "ok" or not data.get("upload_url"):
        hint = ""
        if "unaudited" in str(res).lower():
            hint = ("\n      -> uygulama denetlenmemis: hesabi PRIVATE yapip [tiktok] privacy=\"SELF_ONLY\" "
                    "kullan ya da mode=\"inbox\" ile taslak gonder")
        raise RuntimeError(f"tiktok init basarisiz: {err.get('code', '?')} "
                           f"{err.get('message', '')}{hint}")
    req = urllib.request.Request(data["upload_url"], data=clip.read_bytes(), method="PUT", headers={
        "Content-Type": "video/mp4",
        "Content-Length": str(size),
        "Content-Range": f"bytes 0-{size - 1}/{size}",
    })
    try:
        _http(req, timeout=1800)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"tiktok yukleme basarisiz (HTTP {exc.code}): "
                           f"{exc.read().decode('utf-8', 'ignore')[:300]}") from exc
    return {"publish_id": data.get("publish_id", ""), "mode": mode,
            "url": "" if mode == "inbox" else "https://www.tiktok.com/"}


def tt_status(cfg, publish_id, timeout=300):
    tok = tt_token(cfg)
    deadline = time.time() + timeout
    data = {}
    status = ""
    while time.time() < deadline:
        res = _http_json(f"{TT_API}/post/publish/status/fetch/", {"publish_id": publish_id},
                         token=tok["access_token"], method="POST")
        data = res.get("data") or {}
        status = str(data.get("status", ""))
        if status in ("PUBLISH_COMPLETE", "FAILED", "SEND_TO_USER_INBOX"):
            return status, data
        time.sleep(5)
    return status or "TIMEOUT", data


def ig_creds(cfg):
    app_id = os.environ.get("INSTAGRAM_APP_ID") or str(cfg_get(cfg, "instagram.app_id", "") or "")
    secret = os.environ.get("INSTAGRAM_APP_SECRET") or str(cfg_get(cfg, "instagram.app_secret", "") or "")
    return app_id.strip(), secret.strip()


def ig_token_file(cfg):
    p = Path(str(cfg_get(cfg, "instagram.token", "instagram_token.json")))
    return p if p.is_absolute() else ROOT / p


def ig_api_version(cfg):
    return str(cfg_get(cfg, "instagram.api_version", "v26.0") or "v26.0")


def ig_token(cfg):
    """Uzun omurlu Instagram token'i dondurur; suresi dolmak uzereyse yeniler."""
    path = ig_token_file(cfg)
    if not path.exists():
        raise RuntimeError("instagram_token.json yok -> python clipbot.py auth-instagram")
    tok = json.loads(path.read_text(encoding="utf-8"))
    if not tok.get("access_token"):
        raise RuntimeError("instagram token dosyasi bozuk -> python clipbot.py auth-instagram")
    if tok.get("expires_at", 0) > time.time() + 86400:
        return tok
    url = IG_GRAPH + "/refresh_access_token?" + urllib.parse.urlencode({
        "grant_type": "ig_refresh_token", "access_token": tok["access_token"]})
    fresh = _http_json(url, timeout=60)
    if not fresh.get("access_token"):
        raise RuntimeError(f"instagram token yenilenemedi: {str(fresh)[:300]}")
    tok["access_token"] = fresh["access_token"]
    tok["expires_at"] = time.time() + float(fresh.get("expires_in", 5_184_000))
    path.write_text(json.dumps(tok, indent=2), encoding="utf-8")
    return tok


def cmd_auth_instagram(args, cfg):
    app_id, secret = ig_creds(cfg)
    if not app_id or not secret:
        print("[hata] Instagram app_id/app_secret yok. tek seferlik kurulum:\n")
        print("  1) Instagram uygulamasi -> Ayarlar -> Hesap turu -> Creator/Business")
        print("  2) https://developers.facebook.com -> Create app -> type: Business")
        print("  3) Add product -> Instagram -> 'API setup with Instagram login'")
        print("  4) Permissions: instagram_business_basic, instagram_business_content_publish")
        print("  5) OAuth Redirect URI: https (http kabul edilmezse) kendi alan adin")
        print("  6) app_id / app_secret -> config.toml [instagram]")
        print("     (ya da INSTAGRAM_APP_ID / INSTAGRAM_APP_SECRET ortam degiskeni)\n")
        sys.exit(1)
    port = int(cfg_get(cfg, "instagram.oauth_port", 8768))
    redirect = str(cfg_get(cfg, "instagram.redirect_uri", "") or f"http://127.0.0.1:{port}/")
    state = secrets.token_urlsafe(16)
    url = IG_AUTH + "?" + urllib.parse.urlencode({
        "client_id": app_id, "redirect_uri": redirect, "response_type": "code",
        "scope": IG_SCOPE, "state": state})
    manual = _code_from(getattr(args, "code", ""))
    got = {"code": manual, "state": state} if manual else oauth_wait(url, port, state)
    if not got.get("code"):
        sys.exit(f"[hata] yetki verilmedi: {got.get('error', got)}")
    short = _http_json(IG_CODE, {"client_id": app_id, "client_secret": secret,
                                 "grant_type": "authorization_code",
                                 "redirect_uri": redirect, "code": got["code"]},
                       form=True, method="POST")
    if not short.get("access_token"):
        sys.exit(f"[hata] token alinamadi: {str(short)[:300]}")
    long_url = IG_GRAPH + "/access_token?" + urllib.parse.urlencode({
        "grant_type": "ig_exchange_token", "client_secret": secret,
        "access_token": short["access_token"]})
    long_tok = _http_json(long_url, timeout=60)
    token = long_tok.get("access_token") or short["access_token"]
    tok = {"access_token": token, "user_id": str(short.get("user_id", "")),
           "expires_at": time.time() + float(long_tok.get("expires_in", 5_184_000))}
    path = ig_token_file(cfg)
    path.write_text(json.dumps(tok, indent=2), encoding="utf-8")
    print(f"  ok: {path}  (ig user id {tok['user_id']}, gecerli ~{float(long_tok.get('expires_in', 5_184_000)) / 86400:.0f} gun)")


def ig_graph(cfg, token, path, params=None, method="GET"):
    base = f"{IG_GRAPH}/{ig_api_version(cfg)}/{path}"
    params = {k: v for k, v in (params or {}).items() if v is not None}
    params["access_token"] = token
    if method == "GET":
        req = urllib.request.Request(base + "?" + urllib.parse.urlencode(params),
                                     headers={"User-Agent": "clipbot/1.0"})
        try:
            _, _, body = _http(req, timeout=120)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"instagram HTTP {exc.code}: "
                               f"{exc.read().decode('utf-8', 'ignore')[:400]}") from exc
        return json.loads(body or b"{}")
    req = urllib.request.Request(base, data=urllib.parse.urlencode(params).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded",
                                          "User-Agent": "clipbot/1.0"})
    try:
        _, _, body = _http(req, timeout=300)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"instagram HTTP {exc.code}: "
                           f"{exc.read().decode('utf-8', 'ignore')[:400]}") from exc
    return json.loads(body or b"{}")


def ig_publish(clip, cfg, caption, dry_run=False):
    """Reels: container (resumable ya da public video_url) -> FINISHED -> media_publish."""
    clip = Path(clip)
    tok = ig_token(cfg)
    token = tok["access_token"]
    ig_id = str(tok.get("user_id") or cfg_get(cfg, "instagram.ig_user_id", "") or "")
    if not ig_id:
        raise RuntimeError("instagram user_id yok -> python clipbot.py auth-instagram")
    public_tpl = str(cfg_get(cfg, "instagram.public_url_template", "") or "")
    if dry_run:
        how = "public_url" if public_tpl else "resumable"
        print(f"    [dry-run][instagram] {clip.parent.name}: REELS {how} "
              f"{clip.stat().st_size / 1_048_576:.1f} MB caption={caption[:60]!r}")
        return None
    base_params = {"media_type": "REELS", "caption": caption[:2200],
                   "share_to_feed": "true" if bool(cfg_get(cfg, "instagram.share_to_feed", True)) else "false"}
    if public_tpl:
        public_url = public_tpl.format(name=clip.parent.name, file=clip.name,
                                       path=str(clip).replace("\\", "/"))
        container = ig_graph(cfg, token, f"{ig_id}/media",
                             {**base_params, "video_url": public_url}, method="POST")
    else:
        container = ig_graph(cfg, token, f"{ig_id}/media",
                             {**base_params, "upload_type": "resumable"}, method="POST")
        container_id = container.get("id")
        uri = str(container.get("uri", ""))
        if not container_id or not uri:
            raise RuntimeError(f"instagram container hatasi: {str(container)[:300]}")
        size = clip.stat().st_size
        upload_url = uri if uri.startswith("http") else f"https://{uri}"
        req = urllib.request.Request(upload_url, data=clip.read_bytes(), method="POST", headers={
            "Authorization": f"OAuth {token}",
            "offset": "0",
            "file_size": str(size),
            "Content-Type": "application/octet-stream",
        })
        try:
            _http(req, timeout=1800)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"instagram video yukleme basarisiz (HTTP {exc.code}): "
                               f"{exc.read().decode('utf-8', 'ignore')[:300]}") from exc
    container_id = container.get("id")
    if not container_id:
        raise RuntimeError(f"instagram container id yok: {str(container)[:300]}")
    deadline = time.time() + 600
    status = ""
    while time.time() < deadline:
        st = ig_graph(cfg, token, str(container_id), {"fields": "status_code,status"})
        status = str(st.get("status_code", ""))
        if status == "FINISHED":
            break
        if status in ("ERROR", "EXPIRED", "PUBLISHED"):
            raise RuntimeError(f"instagram container {status}: {st.get('status', '')}")
        time.sleep(6)
    else:
        raise RuntimeError(f"instagram container FINISHED olmadi (son durum {status})")
    pub = ig_graph(cfg, token, f"{ig_id}/media_publish", {"creation_id": container_id}, method="POST")
    media_id = pub.get("id")
    if not media_id:
        raise RuntimeError(f"instagram media_publish hatasi: {str(pub)[:300]}")
    info = ig_graph(cfg, token, str(media_id), {"fields": "permalink"})
    return info.get("permalink") or f"https://www.instagram.com/p/{media_id}"


# --------------------------------------------------------------------------- #
# 6) auto — canliyi izle, kes, yukle
# --------------------------------------------------------------------------- #
def spawn_recorder(cfg, stream_url, video):
    # reconnect: HLS segmenti dusunce kayit devam etsin (yayin gunu sart)
    cmd = [ffmpeg_bin(cfg), "-hide_banner", "-loglevel", "warning", "-y",
           "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
           "-i", stream_url, "-c", "copy", "-f", "mpegts", str(video)]
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL)


def wait_first_bytes(proc, video, timeout=120):
    started = time.time()
    while time.time() - started < timeout:
        if proc.poll() is not None:
            return None
        try:
            if video.exists() and video.stat().st_size > 0:
                return time.time()
        except OSError:
            pass
        time.sleep(0.25)
    return None


def render_live_clip(video, session, channel, start, dur, mode, title, cfg, reason="", min_words=None):
    shim = argparse.Namespace(
        video=str(video), session=session.name, session_name=session.name,
        at=float(start), dur=float(dur), top=0, mode=mode, no_subs=False,
        channel=channel, title=title, reason=reason, min_words=min_words,
    )
    made = cmd_cut(shim, cfg)
    return Path(made[0]) if made else None


def chat_need(samples, floor=0.3, mult=2.0):
    """Bir anin 'chat patlamasi' sayilmasi icin gereken mesaj/sn esigi."""
    rates = [r for _, r in samples if r is not None]
    if not rates:
        return float(floor)
    return max(float(floor), median(rates) * float(mult))


def chat_peaks(samples, floor, min_gap, start, end, limit=2):
    """Bir aralikta esigi gecen chat patlamalari: en yuksekler, aralarinda min_gap."""
    hits = sorted((t, r) for t, r in samples if start <= t <= end and r >= floor)
    picked = []
    for t, r in sorted(hits, key=lambda x: -x[1]):
        if all(abs(t - p) >= min_gap for p, _ in picked):
            picked.append((t, r))
        if len(picked) >= limit:
            break
    return sorted(picked)


def cmd_auto(args, cfg):
    streams_path = getattr(args, "streams", "") or str(ROOT / "streams.toml")
    url, chat_arg = args.url, args.chat
    if not url and args.channel:
        entry = resolve_channel_arg(args.channel, streams_path)
        if not entry:
            sys.exit("[hata] --channel cozulemedi")
        url = str(entry.get("url", ""))
        chat_arg = chat_arg or str(entry.get("chat", ""))
        print(f"  stream: {entry.get('name')} -> {url}")
    if not url:
        sys.exit("[hata] --channel ya da --url ver")
    stream_url = resolve_stream(url)

    channel = chat_arg or channel_name_from_url(url) or "stream"
    sessions_dir = ROOT / cfg_get(cfg, "paths.sessions", "sessions")
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session = sessions_dir / f"{slug(channel)}-{stamp()}"
    session.mkdir(parents=True, exist_ok=True)
    video = session / "video.ts"

    proc = spawn_recorder(cfg, stream_url, video)
    video_start = wait_first_bytes(proc, video)
    if video_start is None:
        proc.terminate()
        sys.exit("[hata] kayit baslamadi (yayin kapali olabilir)")

    # chat sadece Twitch'te var; Kick/YouTube'da tetikleyici ses seviyesi
    chat_name = chat_arg or twitch_channel_from_url(url)
    chat = None if (args.no_chat or not chat_name) else TwitchChat(chat_name)
    if chat:
        chat.start()

    clip_len = float(args.clip_seconds or cfg_get(cfg, "detect.clip_seconds", 32))
    pre_roll = float(cfg_get(cfg, "detect.pre_roll", 4))
    boost = float(args.boost or cfg_get(cfg, "auto.boost_db", 6.0))
    min_gap = float(cfg_get(cfg, "detect.min_gap_seconds", 45))
    every = float(args.every or cfg_get(cfg, "auto.analyze_every", 20))
    warmup = float(cfg_get(cfg, "auto.warmup_seconds", 120))
    max_clips = int(args.max_clips or cfg_get(cfg, "auto.max_clips", 0) or 0)
    require_chat = bool(cfg_get(cfg, "auto.require_chat", True))
    chat_min = float(cfg_get(cfg, "auto.chat_min", 0.3))
    chat_mult = float(cfg_get(cfg, "auto.chat_mult", 2.0))
    chat_only = bool(cfg_get(cfg, "auto.chat_only", True))
    do_upload = (platform_list(cfg, getattr(args, "platform", "") or "")
                 if bool(args.upload or cfg_get(cfg, "upload.enabled", False)) else [])
    do_upload = ready_platforms(cfg, do_upload) if do_upload else []
    max_hours = float(args.max_hours or cfg_get(cfg, "record.max_hours", 6))
    limit_s = float(args.seconds) if args.seconds else max_hours * 3600

    def upload_all(saved):
        for plat in do_upload:
            try:
                upload_clip(saved, cfg, args.privacy or None,
                            dry_run=args.dry_run, platform=plat)
            except Exception as exc:  # noqa: BLE001
                print(f"    [yukleme hatasi:{plat}] {exc}")

    meta = {
        "url": url, "channel": channel, "video": video.name,
        "video_start_epoch": video_start, "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "chat": bool(chat), "mode": "auto", "upload": do_upload,
    }
    (session / "session.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"  kayit: {video}")
    print(f"  otomatik: esik +{boost:.0f} dB, klip {clip_len:.0f}s, analiz {every:.0f}s, isinma {warmup:.0f}s")
    print(f"  secim: chat patlamasi {'sart' if require_chat and chat else 'yok (ses)'}; "
          f"konusmasiz klipler atilir")
    if require_chat and not chat:
        print("  [not] bu platformda chat okunamiyor; secim sadece ses + konusma kontrolu ile")
    elif require_chat:
        print(f"  [not] chat esigi: en az {chat_min:.1f}/sn ya da tabanin {chat_mult:.1f} kati")
    print(f"  yukleme: {'ACIK -> ' + ', '.join(do_upload) if do_upload else 'kapali (--upload ile ac)'}")
    print(f"  bitirmek icin Ctrl+C.  (limit {limit_s / 60:.0f} dk)\n")

    scan_from = warmup
    last_end = 0.0
    made_total = 0
    chat_samples = []
    # watch ile ayni events.csv: auto oturumu da sonradan 'detect' ile analiz edilebilsin
    events_path = session / "events.csv"
    last_rms = None
    next_rms_at = 15.0
    try:
        with events_path.open("w", newline="", encoding="utf-8") as ev:
            ev_writer = csv.writer(ev)
            ev_writer.writerow(["t", "chat_per_sec", "rms_db"])
            while True:
                if proc.poll() is not None:
                    print("\n  [uyari] yayin/ffmpeg durdu")
                    break
                elapsed = time.time() - video_start
                if elapsed > limit_s:
                    print("\n  sure doldu")
                    break
                rate = chat.rate() if chat else 0.0
                chat_samples.append((elapsed, rate))
                chat_samples = [s for s in chat_samples if elapsed - s[0] <= 900]
                if elapsed >= next_rms_at:
                    measured = measure_rms(video, elapsed - 12, 10, cfg)
                    if measured is not None:
                        last_rms = measured
                    next_rms_at = elapsed + 15
                ev_writer.writerow([f"{elapsed:.2f}", f"{rate:.2f}",
                                    "" if last_rms is None else f"{last_rms:.1f}"])
                ev.flush()
                print(f"\r  {int(elapsed // 60):02d}:{int(elapsed % 60):02d}  kayit  chat {rate:4.1f}/sn  "
                      f"{made_total} klip   ", end="", flush=True)

                ready_until = elapsed - clip_len - 1.0
                if ready_until - scan_from >= every:
                    series = loudness_series(video, scan_from, ready_until - scan_from, cfg)
                    bursts = find_bursts(series, boost, min_gap, pre_roll,
                                         min_lufs=float(cfg_get(cfg, "auto.min_lufs", -38.0)),
                                         min_len=float(cfg_get(cfg, "auto.min_len", 1.5)))
                    if not series:
                        print(f"\n  [uyari] {int(scan_from)}-{int(ready_until)}s okunamadi (kayit devam ediyor?)")
                    elif bursts:
                        print(f"\n  analiz {int(scan_from)}-{int(ready_until)}s -> {len(bursts)} patlama")
                    # chat patlamasi olmayan ses olaylari (menu, oyun gecisi) klip olmasin
                    chat_live = chat is not None and any(r > 0 for _, r in chat_samples)
                    chat_floor = chat_need(chat_samples, chat_min, chat_mult)
                    strong_audio = boost + 5
                    for burst in bursts:
                        if max_clips and made_total >= max_clips:
                            break
                        start = scan_from + burst["t"]
                        if start < last_end + min_gap:
                            continue
                        peak_chat = max((r for t, r in chat_samples if start <= t <= start + clip_len), default=0.0)
                        if require_chat and chat_live and burst["gain"] < strong_audio and peak_chat < chat_floor:
                            print(f"  - atlandi @ {int(start // 60):02d}:{int(start % 60):02d}  "
                                  f"(chat {peak_chat:.1f}/sn < {chat_floor:.1f}, ses +{burst['gain']:.0f}dB)")
                            continue
                        reason = f"ses +{burst['gain']:.0f}dB" + (f", chat {peak_chat:.1f}/sn" if peak_chat else "")
                        print(f"  -> patlama @ {int(start // 60):02d}:{int(start % 60):02d}  ({reason})  kesiliyor...")
                        saved = render_live_clip(video, session, channel, start, clip_len,
                                                 args.mode or "", args.title or "", cfg,
                                                 reason=reason,
                                                 min_words=0 if burst["gain"] >= strong_audio else None)
                        if not saved:
                            continue
                        last_end = start + clip_len
                        made_total += 1
                        upload_all(saved)
                    # ses patlamasi olmasa da chat'in patladigi anlar (GTA6'da bolca olur) klip olsun
                    if chat_only and chat_live and (not max_clips or made_total < max_clips):
                        for peak_t, peak_r in chat_peaks(chat_samples, chat_floor, min_gap,
                                                         scan_from, ready_until, limit=2):
                            if max_clips and made_total >= max_clips:
                                break
                            start = max(0.0, peak_t - pre_roll)
                            if start < last_end + min_gap:
                                continue
                            reason = f"chat {peak_r:.1f}/sn patlamasi"
                            print(f"  -> chat @ {int(start // 60):02d}:{int(start % 60):02d}  ({reason})  kesiliyor...")
                            # chat patlamasi tek basina yeterli sinyal: sessiz aksiyon da kalsin
                            saved = render_live_clip(video, session, channel, start, clip_len,
                                                     args.mode or "", args.title or "", cfg,
                                                     reason=reason, min_words=0)
                            if saved:
                                last_end = start + clip_len
                                made_total += 1
                                upload_all(saved)
                    scan_from = max(scan_from, ready_until - pre_roll)
                    if max_clips and made_total >= max_clips:
                        print(f"\n  {max_clips} klip kesildi, duruyorum")
                        break
                time.sleep(5)
    except KeyboardInterrupt:
        print("\n  durduruluyor...")
    finally:
        if chat:
            chat.stop()
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()

    size_mb = video.stat().st_size / 1_048_576 if video.exists() else 0
    print(f"\n  bitti: {session}  ({size_mb:.0f} MB, {made_total} klip)")
    if made_total:
        print("  liste: python clipbot.py queue")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None):
    parser = argparse.ArgumentParser(prog="clipbot", description="Canlidan dikey klip uretimi")
    parser.add_argument("--config", default=str(ROOT / "config.toml"))
    sub = parser.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("watch", help="canli yayini kaydet, chat/ses logla")
    w.add_argument("--url", default="", help="twitch/kick/youtube canli linki")
    w.add_argument("--channel", default="", help="streams.toml'daki isim (url yerine)")
    w.add_argument("--streams", default="", help="streams.toml yolu")
    w.add_argument("--chat", default="", help="twitch kanal adi (URL'den otomatik bulunur)")
    w.add_argument("--no-chat", action="store_true", help="chat dinleme")
    w.add_argument("--max-hours", type=float, default=0)
    w.add_argument("--seconds", type=float, default=0, help="deneme icin saniye siniri")
    w.add_argument("--note", default="")

    d = sub.add_parser("detect", help="kayittaki patlamalari bul")
    d.add_argument("--session", required=True)
    d.add_argument("--top", type=int, default=0)

    c = sub.add_parser("cut", help="klip render et")
    c.add_argument("--session", default="")
    c.add_argument("--video", default="")
    c.add_argument("--at", type=float, default=0)
    c.add_argument("--dur", type=float, default=0)
    c.add_argument("--top", type=int, default=0)
    c.add_argument("--mode", default="", choices=["", "stack", "fill", "blurpad", "crop_center", "crop_left", "crop_right", "original"])
    c.add_argument("--no-facecam", action="store_true", help="webcam PIP'i bu kesimde ekleme")
    c.add_argument("--no-subs", action="store_true")
    c.add_argument("--channel", default="")
    c.add_argument("--title", default="")
    c.add_argument("--session-name", default="", help="klip klasoru adi (auto kullanir)")

    q = sub.add_parser("queue", help="klip listesi")
    q.add_argument("--status", default="")

    m = sub.add_parser("mark", help="klip durumunu isaretle")
    m.add_argument("index", type=int)
    m.add_argument("status", nargs="?", default="posted")

    ch = sub.add_parser("channels", help="streams.toml listesi")
    ch.add_argument("--streams", default="")

    a = sub.add_parser("auto", help="canliyi izle: guzel yeri bul, kes, yukle (tek komut)")
    a.add_argument("--url", default="")
    a.add_argument("--channel", default="")
    a.add_argument("--streams", default="")
    a.add_argument("--chat", default="")
    a.add_argument("--no-chat", action="store_true")
    a.add_argument("--clip-seconds", type=float, default=0)
    a.add_argument("--boost", type=float, default=0, help="patlama esigi dB (varsayilan 6)")
    a.add_argument("--every", type=float, default=0, help="analiz araligi sn (varsayilan 20)")
    a.add_argument("--seconds", type=float, default=0, help="deneme icin sure siniri")
    a.add_argument("--max-hours", type=float, default=0)
    a.add_argument("--mode", default="", choices=["", "stack", "fill", "blurpad", "crop_center", "crop_left", "crop_right", "original"])
    a.add_argument("--title", default="")
    a.add_argument("--max-clips", type=int, default=0, help="bu kadar klip kesince dur (0 = sinirsiz)")
    a.add_argument("--upload", action="store_true", help="kesilen klibi platformlara yukle")
    a.add_argument("--platform", default="", choices=["", "youtube", "tiktok", "instagram", "all"],
                   help="yukleme platformlari (varsayilan: config upload.platforms)")
    a.add_argument("--privacy", default="", choices=["", "public", "unlisted", "private"])
    a.add_argument("--dry-run", action="store_true", help="yukleme yapma, sadece plan yaz")

    u = sub.add_parser("upload", help="kuyruktaki klipleri platformlara yukle")
    u.add_argument("--clip", default="")
    u.add_argument("--limit", type=int, default=0)
    u.add_argument("--platform", default="", choices=["", "youtube", "tiktok", "instagram", "all"],
                   help="varsayilan: config upload.platforms")
    u.add_argument("--privacy", default="", choices=["", "public", "unlisted", "private"])
    u.add_argument("--dry-run", action="store_true")

    sub.add_parser("auth-youtube", help="YouTube yetkilendirmesi (tek sefer)")
    at = sub.add_parser("auth-tiktok", help="TikTok yetkilendirmesi (tek sefer)")
    at.add_argument("--code", default="", help="tarayici donmediyse geri donus URL'sini yapistir")
    ai = sub.add_parser("auth-instagram", help="Instagram yetkilendirmesi (tek sefer)")
    ai.add_argument("--code", default="", help="tarayici donmediyse geri donus URL'sini yapistir")
    sub.add_parser("doctor", help="ortami kontrol et")

    args = parser.parse_args(argv)
    cfg = load_config(args.config)

    handlers = {
        "watch": cmd_watch,
        "channels": cmd_channels,
        "detect": cmd_detect,
        "cut": cmd_cut,
        "auto": cmd_auto,
        "upload": cmd_upload,
        "auth-youtube": cmd_auth_youtube,
        "auth-tiktok": cmd_auth_tiktok,
        "auth-instagram": cmd_auth_instagram,
        "queue": cmd_queue,
        "mark": cmd_mark,
        "doctor": cmd_doctor,
    }
    handlers[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
