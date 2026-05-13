#!/usr/bin/env python3
"""
Human-Like Typer (v2)
Metni text.txt'ye yapıştır, scripti çalıştır, hedef alana tıkla.

  Cmd+Shift+Y → Durdur / Devam et
  Cmd+Shift+V → Tamamen kapat

Yenilikler:
  - QWERTY komşu-tuş bazlı akıllı typo (instant + delayed detection)
  - Yorgunluk simülasyonu (zamanla yavaşlama + typo artışı)
  - Mini ve uzun molalar (yorgunluğu kısmen kurtarır)
  - 10dk'lık bucket varyasyonu → Hubstaff aktivite yüzdesi her bucket'ta farklı
"""

import random
import time
import os
import sys
import math
import threading

try:
    from pynput import keyboard as kb
    from pynput.keyboard import Key, Controller as KeyboardController
    from pynput.mouse import Controller as MouseController
except ImportError:
    print("Eksik paket. Şunu çalıştırın: pip install pynput")
    sys.exit(1)

# ─── TEMEL AYARLAR ────────────────────────────────────────
WPM           = 65    # Ortalama yazma hızı
TYPO_RATE     = 0.03  # Temel typo oranı
STARTUP_DELAY = 5     # Başlamadan önce bekleme süresi

# ─── YORGUNLUK ────────────────────────────────────────────
FATIGUE_RAMP_MIN     = 25     # bu sürede maksimum yorgunluğa ulaş (dk)
FATIGUE_MAX_SLOWDOWN = 0.35   # maks. yavaşlama oranı (%35)
FATIGUE_TYPO_BOOST   = 1.7    # maks. typo oranı çarpanı

# ─── MOLA SİSTEMİ (Hubstaff varyasyonu için kritik) ───────
# Mini mola: her N saniyede bir, M saniye uzunluğunda
MINI_BREAK_EVERY     = (90, 240)
MINI_BREAK_DURATION  = (4, 18)
# Uzun mola: her X mini moladan sonra, Y saniye uzunluğunda
LONG_BREAK_EVERY     = (6, 14)
LONG_BREAK_DURATION  = (45, 180)

# ─── 10dk BUCKET VARYASYONU ───────────────────────────────
# Her 10 dakikada bir profil değişir → Hubstaff aktivite yüzdesi farklılaşır
BUCKET_SECONDS         = 600
BUCKET_SPEED_RANGE     = (0.75, 1.20)   # bu bucket'taki hız çarpanı
BUCKET_TYPO_RANGE      = (0.60, 1.70)   # bu bucket'taki typo çarpanı
BUCKET_INTENSITY_RANGE = (0.60, 1.00)   # bu bucket'taki "yoğunluk" (düşükse daha çok mola)

# ─── FARE SİMÜLASYONU ─────────────────────────────────────
ENABLE_MOUSE          = True
MOUSE_WAIT_RANGE      = (6, 30)    # sıradaki harekete kadar bekleme (sn)
MOUSE_LONG_PAUSE_PROB = 0.08       # ara sıra fare "kahve molası"
MOUSE_LONG_PAUSE_MULT = (3, 8)
# Hareket türlerinin olasılık eşikleri (kümülatif):
MOUSE_P_MICRO   = 0.55             # 0..0.55  → mikro jitter (en sık)
MOUSE_P_DRIFT   = 0.80             # 0.55..0.80 → 50-150px drift
MOUSE_P_SCROLL  = 0.93             # 0.80..0.93 → scroll burst
#                  0.93..1.00 → büyük süpürme (en seyrek)
# Ekran sınırları (off-screen'e kaçmasın diye clamp için — gerekirse büyüt)
SCREEN_W              = 2560
SCREEN_H              = 1600
# ──────────────────────────────────────────────────────────

# QWERTY komşu tuş haritası (TR/EN karışık)
NEIGHBORS = {
    'q': 'wa1',   'w': 'qeas2',  'e': 'wrd3',   'r': 'etfd4',
    't': 'ryfg5', 'y': 'tuhg6',  'u': 'yij7',   'i': 'uok8',
    'o': 'ipl9',  'p': 'olğ0',
    'a': 'qwsz',  's': 'awedxşz','d': 'serfcx', 'f': 'drtgvc',
    'g': 'ftyhbv','h': 'gyujnb', 'j': 'huiknm', 'k': 'jiolm,',
    'l': 'kop.',
    'z': 'asx',   'x': 'zsdc',   'c': 'xdfvç',  'v': 'cfgb',
    'b': 'vghn',  'n': 'bhjm',   'm': 'njk,',
    # Türkçe karakterler
    'ç': 'cv',    'ğ': 'gp',     'ı': 'iuş',    'ö': 'op',
    'ş': 'sıd',   'ü': 'iyu',
}

writer  = KeyboardController()
paused  = False
stopped = False
_lock   = threading.Lock()

_CMDS   = {Key.cmd, Key.cmd_l, Key.cmd_r}
_SHIFTS = {Key.shift, Key.shift_l, Key.shift_r}
_held   = set()


# ─── DURUM ────────────────────────────────────────────────
class State:
    def __init__(self):
        self.start_time = None
        self.last_mini_break = 0.0
        self.mini_breaks_since_long = 0
        self.next_mini_break_in = random.uniform(*MINI_BREAK_EVERY)
        self.next_long_break_after = random.randint(*LONG_BREAK_EVERY)
        self.bucket_id = -1
        self.bucket_speed = 1.0
        self.bucket_typo = 1.0
        self.bucket_intensity = 1.0
        self.fatigue = 0.0  # 0..1

    def init_clock(self):
        now = time.time()
        self.start_time = now
        self.last_mini_break = now

    def update(self):
        now = time.time()
        elapsed = now - self.start_time
        # Yorgunluk doğrusal artış
        ramp = FATIGUE_RAMP_MIN * 60.0
        self.fatigue = min(1.0, elapsed / ramp)
        # 10dk bucket değişimi
        new_bucket = int(elapsed / BUCKET_SECONDS)
        if new_bucket != self.bucket_id:
            self.bucket_id = new_bucket
            self.bucket_speed     = random.uniform(*BUCKET_SPEED_RANGE)
            self.bucket_typo      = random.uniform(*BUCKET_TYPO_RANGE)
            self.bucket_intensity = random.uniform(*BUCKET_INTENSITY_RANGE)
            print(
                f"\n  [bucket #{new_bucket}: hız×{self.bucket_speed:.2f}, "
                f"typo×{self.bucket_typo:.2f}, yoğunluk×{self.bucket_intensity:.2f}]",
                flush=True,
            )
            # Yoğunluk düşükse molalar daha sık
            self.next_mini_break_in /= max(0.5, self.bucket_intensity)

    def effective_wpm(self):
        slowdown = 1.0 - FATIGUE_MAX_SLOWDOWN * self.fatigue
        return WPM * slowdown * self.bucket_speed

    def effective_typo_rate(self):
        fmult = 1.0 + (FATIGUE_TYPO_BOOST - 1.0) * self.fatigue
        return TYPO_RATE * fmult * self.bucket_typo

    def should_mini_break(self):
        return (time.time() - self.last_mini_break) > self.next_mini_break_in

    def take_break(self):
        self.mini_breaks_since_long += 1
        if self.mini_breaks_since_long >= self.next_long_break_after:
            duration = random.uniform(*LONG_BREAK_DURATION)
            kind = "UZUN"
            self.mini_breaks_since_long = 0
            self.next_long_break_after = random.randint(*LONG_BREAK_EVERY)
            self.fatigue *= 0.35  # uzun mola → yorgunluk büyük ölçüde geri
        else:
            duration = random.uniform(*MINI_BREAK_DURATION)
            kind = "mini"
            self.fatigue *= 0.88  # mini mola → yorgunluk biraz geri

        # Düşük yoğunluklu bucket'ta molalar uzar
        if self.bucket_intensity < 0.85:
            duration *= random.uniform(1.0, 1.5)

        print(f"\n  [{kind} mola: {duration:.1f}s]", flush=True)
        _sleep_interruptible(duration)
        self.last_mini_break = time.time()
        self.next_mini_break_in = random.uniform(*MINI_BREAK_EVERY)
        if self.bucket_intensity < 1.0:
            self.next_mini_break_in /= max(0.5, self.bucket_intensity)


state = State()


# ─── FARE SİMÜLATÖRÜ ──────────────────────────────────────
class MouseSimulator(threading.Thread):
    """
    Arka planda fare hareketi. Tıklama YOK (güvenlik).

      - Yazarken çoğunlukla mikro-jitter (el klavyede, parmaklar fareye değiyor)
      - Bezier curve + ease-in-out + mikro-titreme → düz çizgi yok
      - Ara sıra drift, scroll burst (okuma), nadiren büyük süpürme
      - Yoğunluk düşükse hareket aralığı uzar
      - Yorgun → daha titrek, daha yavaş
    """
    def __init__(self, state):
        super().__init__(daemon=True)
        self.state = state
        self.controller = MouseController()
        self._stop = False

    def stop(self):
        self._stop = True

    def _alive(self):
        return not self._stop and not should_stop()

    def _sleep_chunked(self, secs):
        end = time.time() + secs
        while True:
            if not self._alive():
                return
            wait_if_paused()
            remaining = end - time.time()
            if remaining <= 0:
                return
            time.sleep(min(0.25, remaining))

    def run(self):
        # Yazma başlayana kadar bekle
        while self.state.start_time is None and self._alive():
            time.sleep(0.5)

        while self._alive():
            intensity = max(0.4, self.state.bucket_intensity)
            wait = random.uniform(*MOUSE_WAIT_RANGE) / intensity
            if random.random() < MOUSE_LONG_PAUSE_PROB:
                wait *= random.uniform(*MOUSE_LONG_PAUSE_MULT)
            self._sleep_chunked(wait)
            if not self._alive():
                return

            r = random.random()
            if r < MOUSE_P_MICRO:
                self._micro_jitter()
            elif r < MOUSE_P_DRIFT:
                self._drift()
            elif r < MOUSE_P_SCROLL:
                self._scroll_burst()
            else:
                self._sweep()

    # ── Yardımcılar ────────────────────────────────────────
    def _clamp(self, x, y):
        return (max(2, min(SCREEN_W - 2, x)),
                max(2, min(SCREEN_H - 2, y)))

    def _move_curve(self, dx, dy, duration):
        """Bezier eğrisi ile göreceli hareket, ease-in-out + mikro-jitter."""
        sx, sy = self.controller.position
        ex, ey = self._clamp(sx + dx, sy + dy)
        dx, dy = ex - sx, ey - sy
        dist = math.hypot(dx, dy)
        if dist < 1.0:
            return
        # Kontrol noktası: yola dik bir offset → eğri
        mx, my = (sx + ex) / 2.0, (sy + ey) / 2.0
        perp_x, perp_y = -dy, dx
        norm = math.hypot(perp_x, perp_y) or 1.0
        offset = random.uniform(-0.25, 0.25) * dist
        cx = mx + perp_x / norm * offset
        cy = my + perp_y / norm * offset
        # Yorgun → daha titrek
        wobble = 0.4 + self.state.fatigue * 0.9
        steps = max(8, int(duration / 0.018))
        for i in range(1, steps + 1):
            t = i / steps
            t_e = 0.5 - 0.5 * math.cos(t * math.pi)  # ease-in-out
            x = (1 - t_e) ** 2 * sx + 2 * (1 - t_e) * t_e * cx + t_e ** 2 * ex
            y = (1 - t_e) ** 2 * sy + 2 * (1 - t_e) * t_e * cy + t_e ** 2 * ey
            x += random.gauss(0, wobble)
            y += random.gauss(0, wobble)
            x, y = self._clamp(x, y)
            self.controller.position = (x, y)
            time.sleep(duration / steps)
            if not self._alive():
                return

    # ── Hareket tipleri ────────────────────────────────────
    def _micro_jitter(self):
        """El klavyede dinlenirken küçük titremeler."""
        scale = 1.0 + self.state.fatigue * 0.6
        dx = random.gauss(0, 4) * scale
        dy = random.gauss(0, 4) * scale
        self._move_curve(dx, dy, duration=random.uniform(0.10, 0.35))

    def _drift(self):
        """50-150px Bezier hareket."""
        dx = random.gauss(0, 55)
        dy = random.gauss(0, 40)
        dur = random.uniform(0.30, 0.95) * (1.0 + self.state.fatigue * 0.2)
        self._move_curve(dx, dy, duration=dur)

    def _scroll_burst(self):
        """Okuma simülasyonu — birkaç scroll tick (genelde aşağı)."""
        direction = random.choice([-1, -1, -1, 1])
        ticks = random.randint(2, 7)
        for _ in range(ticks):
            self.controller.scroll(0, direction)
            time.sleep(random.uniform(0.18, 0.55))
            if not self._alive():
                return

    def _sweep(self):
        """Büyük curve hareket — sayfa içinde başka bir bölgeye geçiş."""
        dx = random.uniform(-300, 300)
        dy = random.uniform(-200, 200)
        dur = random.uniform(0.7, 1.6) * (1.0 + self.state.fatigue * 0.35)
        self._move_curve(dx, dy, duration=dur)


# ─── KLAVYE DİNLEYİCİ ─────────────────────────────────────
def on_press(key):
    global paused, stopped
    _held.add(key)
    cmd   = any(k in _held for k in _CMDS)
    shift = any(k in _held for k in _SHIFTS)
    char  = getattr(key, 'char', None)
    if cmd and shift and char == 'y':
        with _lock:
            paused = not paused
        print("\n⏸  Durduruldu  (Cmd+Shift+Y ile devam)" if paused
              else "▶  Devam ediyor...", flush=True)
    elif cmd and shift and char == 'v':
        with _lock:
            stopped = True
        print("\n⏹  Kapatılıyor...", flush=True)


def on_release(key):
    _held.discard(key)


def wait_if_paused():
    while True:
        with _lock:
            if stopped or not paused:
                break
        time.sleep(0.05)


def should_stop():
    with _lock:
        return stopped


def _sleep_interruptible(secs):
    end = time.time() + secs
    while True:
        if should_stop():
            return
        wait_if_paused()
        remaining = end - time.time()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))


# ─── YAZMA MOTORU ─────────────────────────────────────────
def char_delay():
    wpm = max(15.0, state.effective_wpm())
    base = 60.0 / (wpm * 5.0)
    return max(0.02, random.gauss(base, base * 0.55))


def backspace(n=1):
    for _ in range(n):
        writer.press(Key.backspace)
        writer.release(Key.backspace)
        time.sleep(random.uniform(0.04, 0.10))


def pick_wrong_char(c):
    """QWERTY komşu tuş bazlı yanlış karakter."""
    lower = c.lower()
    if lower in NEIGHBORS and random.random() < 0.85:
        wrong = random.choice(NEIGHBORS[lower])
    else:
        wrong = random.choice("qwertyuıopasdfghjklzxcvbnmşğç")
    if c.isupper():
        wrong = wrong.upper()
    return wrong


def maybe_typo_kind():
    """
    Geri döner: None | ('instant', 1) | ('delayed', n)
      - instant: hemen fark et, 1 backspace, doğrusunu yaz
      - delayed: n-1 doğru karakter daha yaz, sonra n backspace
    """
    r = random.random()
    if r < 0.45:
        return ('instant', 1)
    elif r < 0.80:
        return ('delayed', random.randint(1, 2))
    else:
        return ('delayed', random.randint(3, 5))


def type_text(text):
    i = 0
    while i < len(text):
        if should_stop():
            return False
        wait_if_paused()
        if should_stop():
            return False

        state.update()

        # Mola kontrolü (karakter yazmadan önce)
        if state.should_mini_break():
            state.take_break()
            if should_stop():
                return False

        ch = text[i]

        # ─── Typo? Sadece harflerde ─────────────────────
        if ch.isalpha() and random.random() < state.effective_typo_rate():
            kind, n = maybe_typo_kind()

            if kind == 'instant':
                wrong = pick_wrong_char(ch)
                writer.type(wrong)
                time.sleep(char_delay())
                if should_stop():
                    return False
                # "ah hata" duraksaması
                time.sleep(random.uniform(0.10, 0.40))
                backspace(1)
                time.sleep(random.uniform(0.06, 0.18))
                # devam: aşağıdaki normal akış doğru karakteri yazacak

            else:  # delayed
                wrong = pick_wrong_char(ch)
                writer.type(wrong)
                time.sleep(char_delay())
                extra = 0
                # n-1 doğru karakter daha yaz (newline/noktalamada dur)
                for k in range(1, n):
                    if i + k >= len(text):
                        break
                    c2 = text[i + k]
                    if c2 in ".!?,;:\n":
                        break
                    if should_stop():
                        return False
                    writer.type(c2)
                    time.sleep(char_delay())
                    extra += 1
                # şimdi fark et
                time.sleep(random.uniform(0.20, 0.55))
                backspace(extra + 1)
                time.sleep(random.uniform(0.10, 0.25))
                # i değişmedi — döngü doğru karakterleri tekrar yazacak

        # ─── Normal karakter ────────────────────────────
        if ch == "\n":
            writer.press(Key.enter)
            writer.release(Key.enter)
            time.sleep(random.uniform(0.35, 0.9))

        elif ch in ".!?":
            writer.type(ch)
            time.sleep(random.uniform(0.45, 1.0))

        elif ch in ",;:":
            writer.type(ch)
            time.sleep(random.uniform(0.18, 0.40))

        elif ch == " ":
            writer.type(" ")
            # Yoğunluk düşükse uzun düşünme molaları daha sık
            think_prob = 0.06 + (1.0 - state.bucket_intensity) * 0.10
            if random.random() < think_prob:
                time.sleep(random.uniform(0.6, 2.4))
            else:
                time.sleep(char_delay() * 0.55)

        else:
            writer.type(ch)
            time.sleep(char_delay())

        i += 1

    return not should_stop()


# ─── MAIN ─────────────────────────────────────────────────
def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    text_file  = os.path.join(script_dir, "text.txt")

    if not os.path.exists(text_file):
        print(f"HATA: text.txt bulunamadı → {text_file}")
        sys.exit(1)

    with open(text_file, "r", encoding="utf-8") as f:
        text = f.read().strip()

    if not text:
        print("HATA: text.txt boş!")
        sys.exit(1)

    print(f"Metin yüklendi ({len(text)} karakter, ~{len(text.split())} kelime).")
    print(f"  Cmd+Shift+Y → Durdur / Devam")
    print(f"  Cmd+Shift+V → Tamamen kapat")
    print(f"  Yorgunluk: {FATIGUE_RAMP_MIN}dk'da maks. (yavaşlama %{int(FATIGUE_MAX_SLOWDOWN*100)}, typo×{FATIGUE_TYPO_BOOST})")
    print(f"  Mola: mini {MINI_BREAK_EVERY[0]}-{MINI_BREAK_EVERY[1]}sn'de bir ({MINI_BREAK_DURATION[0]}-{MINI_BREAK_DURATION[1]}sn)")
    print(f"        uzun her {LONG_BREAK_EVERY[0]}-{LONG_BREAK_EVERY[1]} mini moladan sonra ({LONG_BREAK_DURATION[0]}-{LONG_BREAK_DURATION[1]}sn)")
    print(f"  Hubstaff: her 10dk farklı hız/typo/yoğunluk profili")
    if ENABLE_MOUSE:
        print(f"  Fare: arka planda mikro-jitter + drift + scroll (tıklama YOK)")
    print()
    print(f"Hedef alana tıklayın. {STARTUP_DELAY} saniye sonra başlıyor...\n")

    for k in range(STARTUP_DELAY, 0, -1):
        print(f"  {k}...", flush=True)
        time.sleep(1)

    listener = kb.Listener(on_press=on_press, on_release=on_release)
    listener.daemon = True
    listener.start()

    state.init_clock()
    state.update()  # bucket 0 profilini bas

    mouse_sim = MouseSimulator(state) if ENABLE_MOUSE else None
    if mouse_sim:
        mouse_sim.start()

    print("\n▶  Yazılıyor...\n")
    try:
        completed = type_text(text)
    finally:
        if mouse_sim:
            mouse_sim.stop()
    print("\nTamamlandı!" if completed else "\nİptal edildi.")


if __name__ == "__main__":
    main()
