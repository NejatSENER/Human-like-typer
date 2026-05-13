#!/usr/bin/env python3
"""
activity_monitor.py
Hubstaff-tarzı aktivite ölçer. Klavye + fare olaylarını dakika bazında sayar,
10dk pencerelerinde "aktif dakika yüzdesi" hesaplar.

Kullanım:
  Bir terminalde monitor'ü, diğerinde typer.py'yi çalıştır:
    Terminal 1: python3 activity_monitor.py
    Terminal 2: python3 typer.py        (veya ./calistir.sh)

Opsiyonlar:
  --every 60         konsol özeti aralığı (varsayılan 30 sn)
  --throttle 0.2     fare hareketi throttle eşiği (sn)
  --window 10        Hubstaff penceresi (varsayılan 10 dk)

Çıkış:
  Ctrl+C → final özet + tam CSV
  CSV:    activity_log_YYYY-MM-DD_HHMMSS.csv
"""

import time
import datetime
import os
import sys
import threading
import csv
import argparse
from dataclasses import dataclass

try:
    from pynput import keyboard, mouse
except ImportError:
    print("Eksik paket: pip install pynput")
    sys.exit(1)


@dataclass
class MinuteBucket:
    keys: int = 0
    mouse_moves: int = 0
    scrolls: int = 0
    clicks: int = 0

    def total(self):
        return self.keys + self.mouse_moves + self.scrolls + self.clicks

    def active(self):
        return self.total() > 0


# ─── Global durum ─────────────────────────────────────────
buckets = {}                   # minute_unix → MinuteBucket
buckets_lock = threading.Lock()
last_move_ts = 0.0
session_start = time.time()
stopped = False
last_logged_minute = -1

# Argparse ile override edilen ayarlar
MOVE_THROTTLE_SEC = 0.2
WINDOW_MIN = 10
# ──────────────────────────────────────────────────────────


def current_minute():
    return int(time.time() // 60)


def bump(field_name):
    m = current_minute()
    with buckets_lock:
        b = buckets.get(m)
        if b is None:
            b = MinuteBucket()
            buckets[m] = b
        setattr(b, field_name, getattr(b, field_name) + 1)


# ─── Olay handler'lar ─────────────────────────────────────
def on_key_press(key):
    bump('keys')


def on_move(x, y):
    global last_move_ts
    now = time.time()
    if now - last_move_ts >= MOVE_THROTTLE_SEC:
        last_move_ts = now
        bump('mouse_moves')


def on_click(x, y, button, pressed):
    if pressed:
        bump('clicks')


def on_scroll(x, y, dx, dy):
    bump('scrolls')


# ─── Görselleştirme ───────────────────────────────────────
def render_bar(now_m, span):
    out = ""
    for m in range(now_m - span + 1, now_m + 1):
        b = buckets.get(m)
        if b is None or not b.active():
            out += "·"
        else:
            t = b.total()
            if t < 15:
                out += "▁"
            elif t < 50:
                out += "▃"
            elif t < 150:
                out += "▅"
            else:
                out += "█"
    return out


def write_csv_row(writer, m, b, fp):
    ts = datetime.datetime.fromtimestamp(m * 60).strftime("%Y-%m-%d %H:%M")
    writer.writerow([m, ts, b.keys, b.mouse_moves, b.scrolls, b.clicks,
                     b.total(), int(b.active())])
    fp.flush()


# ─── Reporter thread ──────────────────────────────────────
def reporter(log_path, report_every):
    global last_logged_minute

    with open(log_path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['minute_unix', 'minute_human', 'keys', 'mouse_moves',
                    'scrolls', 'clicks', 'total', 'active'])
        f.flush()

        while not stopped:
            # Küçük parçalarda uyu ki stopped flag'i hızlı algılansın
            chunks = max(1, int(report_every * 5))
            for _ in range(chunks):
                if stopped:
                    break
                time.sleep(0.2)
            if stopped:
                break

            now_m = current_minute()
            with buckets_lock:
                # Tamamlanmış dakikaları CSV'ye yaz
                pending = sorted(m for m in buckets
                                 if m < now_m and m > last_logged_minute)
                for m in pending:
                    write_csv_row(w, m, buckets[m], f)
                if pending:
                    last_logged_minute = pending[-1]

                # Son WINDOW dakikası konsol özeti
                window_mins = list(range(now_m - WINDOW_MIN + 1, now_m + 1))
                active_count = sum(1 for m in window_mins
                                   if buckets.get(m) and buckets[m].active())
                keys = sum(buckets.get(m, MinuteBucket()).keys for m in window_mins)
                moves = sum(buckets.get(m, MinuteBucket()).mouse_moves for m in window_mins)
                scrolls = sum(buckets.get(m, MinuteBucket()).scrolls for m in window_mins)
                clicks = sum(buckets.get(m, MinuteBucket()).clicks for m in window_mins)
                pct = active_count * (100 // WINDOW_MIN)
                elapsed = (time.time() - session_start) / 60.0

                print(f"\n[{datetime.datetime.now().strftime('%H:%M:%S')}] "
                      f"oturum: {elapsed:.1f}dk")
                print(f"  Son {WINDOW_MIN}dk aktivite: "
                      f"{active_count}/{WINDOW_MIN} dk  →  "
                      f"Hubstaff ≈ %{pct}")
                print(f"  Tuş:{keys}  Hareket:{moves}  "
                      f"Scroll:{scrolls}  Tık:{clicks}")
                print(f"  [{render_bar(now_m, WINDOW_MIN)}]  "
                      f"({WINDOW_MIN}dk × 1dk)")


# ─── Final özet ───────────────────────────────────────────
def final_summary(log_path):
    print("\n" + "=" * 64)
    print("FİNAL ÖZET")
    print("=" * 64)

    with buckets_lock:
        if not buckets:
            print("Hiç aktivite kaydedilmedi.")
            return

        first_m = min(buckets.keys())
        now_m = current_minute()

        # Kalan dakikaları CSV'ye ekle
        with open(log_path, 'a', newline='') as f:
            w = csv.writer(f)
            for m in range(first_m, now_m + 1):
                if m <= last_logged_minute:
                    continue
                b = buckets.get(m, MinuteBucket())
                write_csv_row(w, m, b, f)

        # 10dk pencereleri özeti (hizalı)
        print(f"\n{WINDOW_MIN} dakikalık pencereler (Hubstaff frame'leri):")
        print(f"  {'pencere':<13} {'aktif':>9} {'tuş':>7} {'hareket':>9}"
              f" {'scroll':>7} {'%':>5}")
        print("  " + "-" * 55)

        align_start = (first_m // WINDOW_MIN) * WINDOW_MIN
        win = align_start
        windows_seen = []
        while win <= now_m:
            mins = range(win, win + WINDOW_MIN)
            active = sum(1 for m in mins
                         if buckets.get(m) and buckets[m].active())
            keys = sum(buckets.get(m, MinuteBucket()).keys for m in mins)
            moves = sum(buckets.get(m, MinuteBucket()).mouse_moves for m in mins)
            scrolls = sum(buckets.get(m, MinuteBucket()).scrolls for m in mins)
            pct = int(active * 100 / WINDOW_MIN)
            t_start = datetime.datetime.fromtimestamp(win * 60).strftime("%H:%M")
            t_end = datetime.datetime.fromtimestamp(
                (win + WINDOW_MIN) * 60).strftime("%H:%M")
            print(f"  {t_start}-{t_end}    "
                  f"{active:>3}/{WINDOW_MIN:<4} {keys:>7} {moves:>9}"
                  f" {scrolls:>7} {pct:>3}%")
            windows_seen.append((win, active, keys, moves, scrolls, pct))
            win += WINDOW_MIN

        # Varyasyon analizi — Hubstaff için kritik
        if len(windows_seen) >= 2:
            pcts = [w[5] for w in windows_seen]
            avg = sum(pcts) / len(pcts)
            spread = max(pcts) - min(pcts)
            # Standart sapma
            var = sum((p - avg) ** 2 for p in pcts) / len(pcts)
            std = var ** 0.5
            print(f"\nVaryasyon: ortalama %{avg:.1f}  "
                  f"min-max: %{min(pcts)}-%{max(pcts)}  "
                  f"spread: %{spread}  std: {std:.1f}")
            if spread < 15:
                print("  ⚠  Pencereler çok benzer — Hubstaff için şüpheli")
            elif spread > 50:
                print("  ✓  Sağlıklı varyasyon")
            else:
                print("  ~  Makul varyasyon")

        total_mins = now_m - first_m + 1
        total_active = sum(1 for m in range(first_m, now_m + 1)
                           if buckets.get(m) and buckets[m].active())
        if total_mins > 0:
            overall_pct = total_active * 100.0 / total_mins
            print(f"\nGenel: {total_active}/{total_mins} dakika aktif → "
                  f"%{overall_pct:.1f}")
        print(f"\nCSV log: {log_path}")


# ─── Main ─────────────────────────────────────────────────
def main():
    global MOVE_THROTTLE_SEC, WINDOW_MIN, stopped

    ap = argparse.ArgumentParser(
        description="Hubstaff-tarzı aktivite ölçer")
    ap.add_argument('--every', type=int, default=30,
                    help='Konsol özeti aralığı (sn). Varsayılan 30.')
    ap.add_argument('--throttle', type=float, default=0.2,
                    help='Fare hareketi throttle (sn). Varsayılan 0.2.')
    ap.add_argument('--window', type=int, default=10,
                    help='Hubstaff pencere boyutu (dk). Varsayılan 10.')
    args = ap.parse_args()

    MOVE_THROTTLE_SEC = args.throttle
    WINDOW_MIN = args.window

    log_dir = os.path.dirname(os.path.abspath(__file__))
    fname = (f"activity_log_"
             f"{datetime.datetime.now().strftime('%Y-%m-%d_%H%M%S')}.csv")
    log_path = os.path.join(log_dir, fname)

    print("Activity Monitor — Hubstaff-tarzı aktivite ölçer")
    print(f"  Konsol özeti:    her {args.every} sn")
    print(f"  Pencere boyutu:  {WINDOW_MIN} dk")
    print(f"  Mouse throttle:  {MOVE_THROTTLE_SEC} sn")
    print(f"  CSV log:         {log_path}")
    print(f"  Ctrl+C ile çık (final özet basılır)\n")
    print("Dinleyiciler başlatılıyor...")

    rep = threading.Thread(target=reporter, args=(log_path, args.every),
                           daemon=True)
    rep.start()

    k_listener = keyboard.Listener(on_press=on_key_press)
    m_listener = mouse.Listener(on_move=on_move, on_click=on_click,
                                on_scroll=on_scroll)
    k_listener.start()
    m_listener.start()
    print("Dinleniyor. Tuşa basın veya fareyi oynatın.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\nKapatılıyor...")
        stopped = True
        rep.join(timeout=args.every + 3)
        k_listener.stop()
        m_listener.stop()
        final_summary(log_path)
        sys.exit(0)


if __name__ == "__main__":
    main()
