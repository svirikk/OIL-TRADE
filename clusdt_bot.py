"""
Binance CLUSDT Perpetual Futures Monitor Bot
============================================
Моніторить Open Interest та ціну CLUSDT кожну хвилину.
Надсилає алерти в Telegram при аномальних змінах.
"""

import os
import time
import logging
import requests
from collections import deque
from datetime import datetime, timezone

# ──────────────────────────────────────────────
#  КОНФІГУРАЦІЯ
# ──────────────────────────────────────────────
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")

SYMBOL          = "CLUSDT"          # Торгова пара
CHECK_INTERVAL  = 60                # Секунди між перевірками
HISTORY_SIZE    = 6                 # Зберігаємо 6 точок → 5-хвилинне вікно

# ──── Порогові значення (у відсотках) ─────────
OI_THRESHOLD_ACCUM   = 1.5   # OI зріс більше ніж на X% (аномальне накопичення)
PRICE_STABLE_MAX     = 0.2   # Ціна змінилася менше ніж на Y% (ціна стабільна)
OI_IMPULSE_MIN       = 0.5   # Мінімальний ріст OI для long/short-імпульсу
PRICE_LONG_MIN       = 0.5   # Ціна зросла більше ніж на Z% → LONG
PRICE_SHORT_MAX      = -0.5  # Ціна впала більше ніж на -Z% → SHORT

# ──── Binance Futures REST ─────────────────────
BASE_URL = "https://fapi.binance.com"
TIMEOUT  = 10   # секунди

# ──────────────────────────────────────────────
#  ЛОГУВАННЯ
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("clusdt_bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
#  СТАН (кільцеві буфери)
# ──────────────────────────────────────────────
oi_history    = deque(maxlen=HISTORY_SIZE)   # (timestamp, oi_value)
price_history = deque(maxlen=HISTORY_SIZE)   # (timestamp, price_value)


# ──────────────────────────────────────────────
#  BINANCE API
# ──────────────────────────────────────────────
def _get(endpoint: str, params: dict = None, retries: int = 3) -> dict | list:
    """
    HTTP GET до Binance Futures API з повторними спробами.
    Захист від бану: між спробами зростаюча затримка.
    """
    url = BASE_URL + endpoint
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            log.warning("Timeout на %s (спроба %d/%d)", endpoint, attempt, retries)
        except requests.exceptions.ConnectionError as exc:
            log.warning("ConnectionError: %s (спроба %d/%d)", exc, attempt, retries)
        except requests.exceptions.HTTPError as exc:
            # 429 = Rate Limit → чекаємо довше
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 60))
                log.warning("Rate limit (429). Чекаємо %d с...", retry_after)
                time.sleep(retry_after)
            else:
                log.error("HTTP %s на %s: %s", resp.status_code, endpoint, exc)
                raise
        if attempt < retries:
            sleep_sec = 2 ** attempt          # 2, 4, 8...
            log.info("Пауза %d с перед повтором...", sleep_sec)
            time.sleep(sleep_sec)
    raise RuntimeError(f"Не вдалося отримати дані з {endpoint} після {retries} спроб")


def fetch_open_interest() -> float:
    """Повертає поточний Open Interest через fapi/v1/openInterest."""
    data = _get("/fapi/v1/openInterest", {"symbol": SYMBOL})
    return float(data["openInterest"])


def fetch_ticker() -> dict:
    """
    Повертає словник із last price та 5-хвилинним об'ємом (з kline).
    """
    # Остання ціна
    ticker = _get("/fapi/v1/ticker/price", {"symbol": SYMBOL})
    last_price = float(ticker["price"])

    # 5-хвилинна свічка для обсягу
    klines = _get(
        "/fapi/v1/klines",
        {"symbol": SYMBOL, "interval": "5m", "limit": 1},
    )
    # kline[5] = quote asset volume (USDT)
    volume_5m = float(klines[0][7]) if klines else 0.0

    return {"price": last_price, "volume_5m": volume_5m}


# ──────────────────────────────────────────────
#  TELEGRAM
# ──────────────────────────────────────────────
def send_telegram(text: str) -> None:
    """Надсилає повідомлення в Telegram через Bot API."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       text,
        "parse_mode": "HTML",
    }
    try:
        resp = requests.post(url, json=payload, timeout=TIMEOUT)
        resp.raise_for_status()
        log.info("✅ Telegram-повідомлення надіслано")
    except Exception as exc:
        log.error("❌ Помилка надсилання в Telegram: %s", exc)


# ──────────────────────────────────────────────
#  ЛОГІКА АЛЕРТІВ
# ──────────────────────────────────────────────
def pct_change(old: float, new: float) -> float:
    """Відсоткова зміна від old до new."""
    if old == 0:
        return 0.0
    return (new - old) / old * 100


def check_alerts(oi_pct: float, price_pct: float, volume_5m: float) -> None:
    """
    Аналізує зміни OI та ціни й надсилає алерти за потреби.
    Пріоритет: спочатку перевіряємо найсильніший сигнал.
    """
    ts  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    vol = f"{volume_5m:,.0f} USDT"

    # ── 1. Аномальне накопичення (OI ↑, ціна стабільна) ──
    if oi_pct > OI_THRESHOLD_ACCUM and abs(price_pct) < PRICE_STABLE_MAX:
        msg = (
            f"🚨 <b>АНОМАЛЬНЕ НАКОПИЧЕННЯ!</b>\n"
            f"Пара: <b>{SYMBOL}</b> | {ts}\n"
            f"📊 OI виріс на <b>+{oi_pct:.2f}%</b>\n"
            f"💵 Ціна стабільна (<b>{price_pct:+.2f}%</b>)\n"
            f"📦 Об'єм за 5 хв: <b>{vol}</b>\n"
            f"⚡ Можливий імпульс!"
        )
        send_telegram(msg)
        log.info("АЛЕРТ: аномальне накопичення — OI %+.2f%%, ціна %+.2f%%", oi_pct, price_pct)

    # ── 2. Сильний LONG-імпульс (OI ↑ + ціна ↑) ──────────
    elif oi_pct > OI_IMPULSE_MIN and price_pct > PRICE_LONG_MIN:
        msg = (
            f"📈 <b>СИЛЬНИЙ LONG-ІМПУЛЬС!</b>\n"
            f"Пара: <b>{SYMBOL}</b> | {ts}\n"
            f"📊 OI: <b>+{oi_pct:.2f}%</b>\n"
            f"💵 Ціна: <b>+{price_pct:.2f}%</b>\n"
            f"📦 Об'єм за 5 хв: <b>{vol}</b>"
        )
        send_telegram(msg)
        log.info("АЛЕРТ: LONG-імпульс — OI %+.2f%%, ціна %+.2f%%", oi_pct, price_pct)

    # ── 3. Сильний SHORT-імпульс (OI ↑ + ціна ↓) ─────────
    elif oi_pct > OI_IMPULSE_MIN and price_pct < PRICE_SHORT_MAX:
        msg = (
            f"📉 <b>СИЛЬНИЙ SHORT-ІМПУЛЬС!</b>\n"
            f"Пара: <b>{SYMBOL}</b> | {ts}\n"
            f"📊 OI: <b>+{oi_pct:.2f}%</b>\n"
            f"💵 Ціна: <b>{price_pct:.2f}%</b>\n"
            f"📦 Об'єм за 5 хв: <b>{vol}</b>"
        )
        send_telegram(msg)
        log.info("АЛЕРТ: SHORT-імпульс — OI %+.2f%%, ціна %+.2f%%", oi_pct, price_pct)
    else:
        log.info(
            "Без алерту | OI %+.2f%% | Ціна %+.2f%% | Об'єм %s",
            oi_pct, price_pct, vol,
        )


# ──────────────────────────────────────────────
#  ОСНОВНИЙ ЦИКЛ
# ──────────────────────────────────────────────
def tick() -> None:
    """Одна ітерація: збирає дані та перевіряє умови алертів."""
    now = time.time()

    oi         = fetch_open_interest()
    ticker_data = fetch_ticker()
    price      = ticker_data["price"]
    volume_5m  = ticker_data["volume_5m"]

    oi_history.append((now, oi))
    price_history.append((now, price))

    log.info("OI=%.2f | Ціна=%.6f | Об'єм=%.0f USDT", oi, price, volume_5m)

    # Потрібно мінімум 2 точки (тобто ≥1 хв. даних) для розрахунку
    if len(oi_history) < 2:
        log.info("Збираємо початкові дані... (%d/%d)", len(oi_history), HISTORY_SIZE)
        return

    # Порівнюємо з найстаршою точкою у буфері (до 5 хвилин тому)
    old_oi    = oi_history[0][1]
    old_price = price_history[0][1]

    oi_pct    = pct_change(old_oi, oi)
    price_pct = pct_change(old_price, price)

    check_alerts(oi_pct, price_pct, volume_5m)


def run() -> None:
    """Нескінченний цикл з перевіркою кожні CHECK_INTERVAL секунд."""
    log.info("🚀 Бот запущено. Пара: %s | Інтервал: %ds", SYMBOL, CHECK_INTERVAL)
    send_telegram(
        f"🤖 <b>Бот CLUSDT запущено</b>\n"
        f"Моніторинг: <b>{SYMBOL}</b> Perpetual Futures\n"
        f"Інтервал перевірки: {CHECK_INTERVAL} с\n"
        f"Порогові значення:\n"
        f"  • OI накопичення: &gt;{OI_THRESHOLD_ACCUM}%\n"
        f"  • Стабільна ціна: &lt;{PRICE_STABLE_MAX}%\n"
        f"  • Long/Short імпульс: OI&gt;{OI_IMPULSE_MIN}%, ціна&gt;±{PRICE_LONG_MIN}%"
    )

    while True:
        start = time.monotonic()
        try:
            tick()
        except RuntimeError as exc:
            log.error("Помилка в tick(): %s", exc)
            # Пауза 30 с перед наступною спробою щоб не бити API
            time.sleep(30)
            continue
        except Exception as exc:
            log.exception("Непередбачена помилка: %s", exc)
            time.sleep(30)
            continue

        # Точний sleep з урахуванням часу виконання
        elapsed = time.monotonic() - start
        sleep_for = max(0, CHECK_INTERVAL - elapsed)
        log.debug("Наступна перевірка через %.1f с", sleep_for)
        time.sleep(sleep_for)


# ──────────────────────────────────────────────
if __name__ == "__main__":
    run()