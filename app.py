import time
import json
import re
import threading
from datetime import datetime
import feedparser
import requests
import streamlit as st
from google import genai
from groq import Groq

from config import (
    GEMINI_API_KEY, GROQ_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    RSS_FEEDS, TARGET_ASSETS, KEYWORDS, INTERVAL_SECONDS,
    MAX_ANALYSES_PER_SCAN, GEMINI_CALL_DELAY_SECONDS, ALERT_CONFIDENCE_THRESHOLD,
    COINGECKO_API_KEY,
    get_conn,
)

# ================= CONFIGURATION =================
# ค่าตั้งค่าทั้งหมดอยู่ใน config.py แล้ว (ไฟล์นี้ import มาใช้เท่านั้น)
# แก้ค่าตั้งค่า/threshold ต่างๆ ไปแก้ที่ config.py แทน
# =================================================


def screen_with_groq(groq_client, title):
    """
    คัดกรองก่อนว่าข่าวนี้เป็น catalyst ที่มีโอกาสขยับราคาจริงไหม
    ใช้ Groq (โควตาฟรีสูงกว่า Gemini มาก) เป็นด่านแรก ประหยัดโควตา Gemini
    ไว้สำหรับข่าวที่มีโอกาสเป็นสัญญาณจริงเท่านั้น
    """
    if not GROQ_API_KEY:
        return True  # ไม่ได้ตั้งค่า Groq ไว้ ให้ผ่านไปวิเคราะห์กับ Gemini ตามปกติ
    prompt = f"""Is this news headline likely to be a real market-moving catalyst
(e.g. Fed decision, regulation, earnings, major hack/exploit, macro data)
as opposed to casual opinion/chatter/meme content?
Headline: "{title}"
Respond strictly in JSON: {{"is_catalyst": true or false}}"""
    try:
        response = groq_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        result = json.loads(response.choices[0].message.content)
        return bool(result.get("is_catalyst", False))
    except Exception as e:
        print(f"Groq screen error: {type(e).__name__}: {e}")
        return True  # Groq ล้มเหลว ให้ผ่านไปให้ Gemini ตัดสินแทน กันข่าวสำคัญหลุด


def get_news_lag_minutes(entry):
    """คำนวณว่าข่าวนี้เก่าไปแล้วกี่นาที นับจากเวลา publish ถึงตอนนี้ (สำหรับโชว์ lag จริง)"""
    published = entry.get("published_parsed") or entry.get("updated_parsed")
    if not published:
        return None
    try:
        published_ts = time.mktime(published)
        lag_seconds = time.time() - published_ts
        return max(0, round(lag_seconds / 60))
    except Exception:
        return None


def _word_match(text_upper, keyword):
    """เช็คว่า keyword ปรากฏเป็น 'คำ' จริงๆ ไม่ใช่แค่ substring ที่ซ่อนอยู่ในคำอื่น
    (บั๊กที่เจอ: 'ETH' in 'WHETHER' → True ทั้งที่ไม่เกี่ยวกับ Ethereum เลย ทำให้ข่าว
    'Here's whether the stock...' ถูกแท็กเป็นข่าว ETH ผิดๆ, เช่นเดียวกับ 'PUTS' ใน 'INPUTS',
    'CALLS' ใน 'RECALLS', 'FED' ใน 'FEDEX' ฯลฯ — ใช้ word-boundary แทนแก้ปัญหานี้ทั้งหมด)"""
    return re.search(rf"\b{re.escape(keyword)}\b", text_upper) is not None


def detect_asset(text):
    text_upper = text.upper()
    for asset in TARGET_ASSETS:
        if (_word_match(text_upper, asset)
                or (asset == "BTC" and _word_match(text_upper, "BITCOIN"))
                or (asset == "ETH" and _word_match(text_upper, "ETHEREUM"))
                or (asset == "GOLD" and _word_match(text_upper, "XAU"))):
            return asset
    return "General Market"


# CoinGecko เป็น public API ไม่ต้องขอ key และไม่โดนบล็อกตามภูมิภาคแบบ Binance
# ตอนนี้รองรับแค่ BTC/ETH (สินทรัพย์อื่นยังไม่มีราคาให้เทียบ)
COINGECKO_IDS = {"BTC": "bitcoin", "ETH": "ethereum"}


def _coingecko_get(url, params, max_retries=2):
    """เรียก CoinGecko พร้อม API key (ถ้ามี) และ retry อัตโนมัติเมื่อโดน 429 rate limit"""
    headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    for attempt in range(max_retries + 1):
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        if resp.status_code == 429:
            wait = 15 * (attempt + 1)  # 15s, 30s, ...
            print(f"CoinGecko 429 rate limit, รอ {wait}s แล้วลองใหม่ (attempt {attempt + 1}/{max_retries})")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()  # หมด retry แล้วยัง 429 อยู่ ให้ raise ปกติ
    return resp


def get_live_price(asset):
    """คืนราคาปัจจุบันจาก CoinGecko สำหรับสินทรัพย์ที่รองรับ คืน None ถ้าไม่มีราคาให้ดึง (เช่น FED, GOLD ตอนนี้)"""
    coingecko_id = COINGECKO_IDS.get(asset)
    if not coingecko_id:
        return None
    try:
        resp = _coingecko_get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": coingecko_id, "vs_currencies": "usd"},
        )
        return float(resp.json()[coingecko_id]["usd"])
    except Exception as e:
        print(f"CoinGecko price fetch error ({asset}): {type(e).__name__}: {e}")
        return None


def get_price_context(asset):
    """ดึงราคาปัจจุบันของ asset พร้อม % เปลี่ยนแปลงช่วง 1 ชม. และ 24 ชม. ล่าสุดจาก CoinGecko
    (endpoint เดียวจบ ไม่ต้องยิง 2 รอบ) เพื่อป้อนเป็น "บริบทราคาจริง" ให้ Gemini ตอนวิเคราะห์ข่าว
    แทนที่จะวิเคราะห์จากแค่ข้อความข่าวเปล่าๆ โดยไม่รู้เลยว่าตอนนี้ตลาดกำลังขึ้น/ลง/นิ่งอยู่
    คืน None ถ้าไม่มีราคาให้ดึง (เช่น FED, GOLD, NVDA ตอนนี้ยังไม่รองรับ)"""
    coingecko_id = COINGECKO_IDS.get(asset)
    if not coingecko_id:
        return None
    try:
        resp = _coingecko_get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "ids": coingecko_id,
                "price_change_percentage": "1h,24h",
            },
        )
        data = resp.json()
        if not data:
            return None
        row = data[0]
        return {
            "price": row.get("current_price"),
            "change_1h": row.get("price_change_percentage_1h_in_currency"),
            "change_24h": row.get("price_change_percentage_24h_in_currency"),
        }
    except Exception as e:
        print(f"CoinGecko price context error ({asset}): {type(e).__name__}: {e}")
        return None


def update_pending_price_checks():
    """เช็คว่าข่าวไหนถึงเวลาต้องบันทึกราคาหลังข่าวแล้วบ้าง (15นาที/1ชม./4ชม./24ชม.)
    เพื่อใช้วัด accuracy จริงทีหลังว่าราคาขยับตามที่ทำนายไหม"""
    windows = [
        ("price_15m", "15 minutes"),
        ("price_1h", "1 hour"),
        ("price_4h", "4 hours"),
        ("price_24h", "24 hours"),
    ]
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                for column, interval in windows:
                    cur.execute(
                        f"""
                        SELECT id, asset FROM market_sentiment
                        WHERE price_at_alert IS NOT NULL
                          AND {column} IS NULL
                          AND timestamp <= NOW() - INTERVAL '{interval}'
                        LIMIT 20;
                        """
                    )
                    rows = cur.fetchall()
                    for row_id, asset in rows:
                        price = get_live_price(asset)
                        if price is not None:
                            cur.execute(
                                f"UPDATE market_sentiment SET {column} = %s WHERE id = %s;",
                                (price, row_id)
                            )
                            conn.commit()
                        time.sleep(0.3)  # ใจดีกับ Binance API นิดหน่อย
    except Exception as e:
        print(f"Price check update error: {type(e).__name__}: {e}")


def send_telegram_alert(source, asset, title, sentiment, confidence, action, reason, link, lag_minutes=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    emoji = "🟢" if sentiment == "Bullish" else "🔴" if sentiment == "Bearish" else "⚪"
    lag_line = ""
    if lag_minutes is not None:
        lag_warning = " ⚠️ ข่าวช้าไปแล้ว ตรวจราคาก่อนเข้าไม้" if lag_minutes >= 5 else ""
        lag_line = f"⏱ *News Lag:* {lag_minutes} นาที{lag_warning}\n"
    msg = (
        f"{emoji} *Market Alert [{asset} - {sentiment}]* ({confidence}% Conf)\n\n"
        f"📡 *Source:* {source}\n"
        f"📌 *Title:* {title}\n"
        f"{lag_line}"
        f"⚡ *Action:* `{action}`\n"
        f"💡 *Reason:* {reason}\n"
        f"🔗 [View Source]({link})"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"Telegram Error: {e}")


def _clean_summary(summary, max_len=600):
    """ตัด HTML tag ออกจาก RSS summary/body และจำกัดความยาว ก่อนส่งให้ Gemini วิเคราะห์
    (RSS summary มักมี <a>, <img>, <p> ปนมา ถ้าไม่ตัดออกจะกินโควตา token และรก prompt เปล่าๆ)"""
    text = re.sub(r"<[^>]+>", " ", summary or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


def analyze_advanced(client, title, summary="", asset="", price_context=None):
    """วิเคราะห์ sentiment โดยใช้ทั้ง title และ summary/body ของข่าว (ไม่ใช่แค่ title เฉยๆ เหมือนเดิม)
    และให้เกณฑ์ confidence ที่ชัดเจนขึ้น เพื่อให้ค่าที่ได้สะท้อนโอกาสราคาขยับจริง
    ไม่ใช่แค่ 'โทนข่าวฟังดูมั่นใจแค่ไหน' (ปัญหาที่พบจากการ backtest: confidence สูงกลับแม่นน้อยกว่า confidence ต่ำ)
    price_context (ถ้ามี จาก get_price_context()): dict {price, change_1h, change_24h} ราคาจริง ณ ตอนวิเคราะห์
    ใช้เป็น "บริบทราคาจริง" ให้ Gemini เห็นว่าตลาดกำลังขึ้น/ลง/นิ่งอยู่ ไม่ใช่วิเคราะห์แค่ตัวข้อความข่าวเปล่าๆ"""
    clean_summary = _clean_summary(summary)
    body_block = f'\n    Body/summary: "{clean_summary}"' if clean_summary else ""
    asset_block = f"\n    Asset in focus: {asset}" if asset and asset != "General Market" else ""

    price_block = ""
    price_guidance = ""
    if price_context and price_context.get("price") is not None:
        p = price_context["price"]
        c1h = price_context.get("change_1h")
        c24h = price_context.get("change_24h")
        c1h_str = f"{c1h:+.2f}%" if c1h is not None else "N/A"
        c24h_str = f"{c24h:+.2f}%" if c24h is not None else "N/A"
        price_block = (
            f"\n    Live market data for {asset}: current price ${p:,.2f}, "
            f"change over last 1h: {c1h_str}, change over last 24h: {c24h_str}."
        )
        price_guidance = """

    You are also given the ACTUAL current price and recent trend for the asset above — weigh the news
    against it, don't analyze the text in a vacuum:
    - If the news direction agrees with the recent 1h/24h trend, that's a confirming signal — momentum
      plus a real catalyst is a stronger case for the stated direction.
    - If the news direction contradicts a strong recent trend, be more cautious: the market may already
      have priced this in, or the move may already be exhausted — lower confidence unless the news is a
      strong enough standalone catalyst to plausibly reverse the trend.
    - If price has been essentially flat, treat the news mostly on its own merits."""

    prompt = f"""
    You are a professional market analyst screening news for SHORT-TERM (minutes-to-hours) trading signals.

    Headline: "{title}"{body_block}{asset_block}{price_block}

    Analyze the market sentiment based on the FULL content above (headline AND body, not just the headline).{price_guidance}

    Confidence scoring guide — confidence must reflect how likely the price is to actually move in the
    stated direction within the next few hours, NOT how strongly-worded or dramatic the headline sounds:
    - 85-100: A concrete, already-confirmed event with a clear, direct causal link to price
      (e.g. an actual rate decision, a confirmed hack/exploit with a specific dollar amount,
      a confirmed regulatory ruling, an earnings beat/miss with hard numbers).
    - 60-84: A real potential catalyst, but with meaningful uncertainty — a forecast, a rumor,
      an analyst opinion, or an event whose market impact is plausible but not yet confirmed.
    - Below 60: Vague, speculative, opinion/editorial content, or news that is likely already
      priced in / stale / recycled.
    If the headline is clickbait, an opinion piece, a repeated/stale story, or has no clear causal
    link to near-term price movement, sentiment should be "Neutral" and confidence should be LOW
    regardless of how dramatic the wording is.

    Respond strictly in JSON format with these exact keys:
    {{
        "sentiment": "Bullish" or "Bearish" or "Neutral",
        "confidence": 80,
        "action": "Buy Call/Long" or "Buy Put/Short" or "Wait and See",
        "reason": "1 short concise sentence explaining why, citing the body content if it added information"
    }}
    """
    try:
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
            config={"response_mime_type": "application/json"}
        )
        return json.loads(response.text.strip())
    except Exception as e:
        print(f"Gemini analyze error: {type(e).__name__}: {e}")
        # สำคัญ: คืน None แทนการยัด fallback dict ปลอมๆ เข้าไป — เดิมทีเคย return
        # {"sentiment": "Neutral", "reason": "Analysis unavailable"} ซึ่งทำให้โค้ดที่เรียกใช้แยกไม่ออก
        # ระหว่าง "วิเคราะห์แล้วได้ Neutral จริง" กับ "Gemini ล้มเหลว (โควตาเต็ม/503)" แล้วดัน mark ว่า
        # ข่าวนั้น "ประมวลผลแล้ว" ทั้งที่ไม่ได้วิเคราะห์จริง ทำให้ข่าวที่มาตอนโควตาเต็มถูกทิ้งถาวร ไม่มีวัน retry
        return None


def get_price_at(coingecko_id, dt):
    """ดึงราคา ณ เวลา dt (UTC) จาก CoinGecko โดยขอช่วงแคบๆ รอบเวลานั้น (±30 นาที)
    แล้วเลือกจุดข้อมูลที่ใกล้เวลาเป้าหมายที่สุด (ราคาย้อนหลังจริง ต่างจาก get_live_price ที่ดึงราคาปัจจุบัน)"""
    from datetime import timedelta as _td
    from_ts = int((dt - _td(minutes=30)).timestamp())
    to_ts = int((dt + _td(minutes=30)).timestamp())
    try:
        resp = _coingecko_get(
            f"https://api.coingecko.com/api/v3/coins/{coingecko_id}/market_chart/range",
            params={"vs_currency": "usd", "from": from_ts, "to": to_ts},
        )
        prices = resp.json().get("prices", [])
        if not prices:
            return None
        target_ms = dt.timestamp() * 1000
        closest = min(prices, key=lambda p: abs(p[0] - target_ms))
        return float(closest[1])
    except Exception as e:
        print(f"CoinGecko historical price error: {type(e).__name__}: {e}")
        return None


def get_historical_price_context(coingecko_id, ts, price_now):
    """เวอร์ชัน backtest ของ get_price_context(): คำนวณ % เปลี่ยนแปลงราคาช่วง 1 ชม./24 ชม.
    "ก่อน" เวลา ts ที่กำหนด (ใช้ตอนเทสข่าวเก่า เพราะต้องดูราคา ณ ตอนนั้นจริงๆ ไม่ใช่ราคาปัจจุบัน
    เหมือน get_price_context() ที่ใช้กับข่าวสด) — price_now คือ price_at_alert ที่บันทึกไว้แล้วตอนนั้น"""
    from datetime import timedelta as _td
    price_1h_ago = get_price_at(coingecko_id, ts - _td(hours=1))
    time.sleep(1.5)
    price_24h_ago = get_price_at(coingecko_id, ts - _td(hours=24))
    time.sleep(1.5)
    change_1h = ((price_now - price_1h_ago) / price_1h_ago * 100) if price_1h_ago else None
    change_24h = ((price_now - price_24h_ago) / price_24h_ago * 100) if price_24h_ago else None
    return {"price": price_now, "change_1h": change_1h, "change_24h": change_24h}


def backfill_historical_prices():
    """รันครั้งเดียวตอน bot เริ่มทำงาน: เติมราคาย้อนหลังให้ข่าว BTC/ETH เก่าที่ยังไม่มี price_at_alert
    (ข่าวใหม่จากนี้ไป save_records()/update_pending_price_checks() จะเก็บราคาให้เองแบบ real-time)
    เปิด connection ใหม่ทุกแถวที่บันทึก กันปัญหา connection หลุดตอนรันนาน (เจอ SSL closed มาก่อน)"""
    from datetime import timedelta, timezone as tz
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, asset, timestamp FROM market_sentiment
                    WHERE asset IN ('BTC', 'ETH') AND price_at_alert IS NULL
                    ORDER BY timestamp ASC;
                """)
                rows = cur.fetchall()
    except Exception as e:
        print(f"Backfill error (reading rows): {type(e).__name__}: {e}")
        return

    if not rows:
        print("Backfill: ไม่มีข่าว BTC/ETH เก่าที่ต้องเติมราคาย้อนหลัง")
        return
    print(f"Backfill: พบ {len(rows)} รายการ กำลังเติมราคาย้อนหลัง...")
    now_utc = datetime.now(tz.utc)
    done = 0

    for row_id, asset, ts in rows:
        coingecko_id = COINGECKO_IDS.get(asset)
        if not coingecko_id:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=tz.utc)

        windows = {
            "price_at_alert": ts,
            "price_15m": ts + timedelta(minutes=15),
            "price_1h": ts + timedelta(hours=1),
            "price_4h": ts + timedelta(hours=4),
            "price_24h": ts + timedelta(hours=24),
        }
        updates = {}
        for column, target_dt in windows.items():
            if target_dt > now_utc:
                continue  # ยังไม่ถึงเวลาจริง ปล่อยให้ update_pending_price_checks() เก็บทีหลัง
            price = get_price_at(coingecko_id, target_dt)
            if price is not None:
                updates[column] = price
            time.sleep(2.5)  # ใจดีกับ rate limit ของ CoinGecko free tier (มี retry รองรับ 429 อยู่แล้ว)

        if updates:
            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        set_clause = ", ".join(f"{c} = %s" for c in updates)
                        values = list(updates.values()) + [row_id]
                        cur.execute(f"UPDATE market_sentiment SET {set_clause} WHERE id = %s;", values)
                    conn.commit()
                done += 1
            except Exception as e:
                print(f"Backfill row {row_id} save error: {type(e).__name__}: {e}")

    print(f"Backfill: เติมราคาย้อนหลังเสร็จสิ้น {done}/{len(rows)} รายการ")


def backfill_baseline_prices():
    """เติม baseline sample ย้อนหลัง โดยสุ่มจุดเวลาแบบสม่ำเสมอ (ทุก 6 ชม.) ตั้งแต่ข่าวแรกในระบบจนถึงตอนนี้
    ใช้ get_price_at() แบบเดียวกับที่ backfill_historical_prices() ใช้เติมราคาข่าวเก่า
    เพื่อให้มีข้อมูล baseline เทียบกับข่าวได้ทันที ไม่ต้องรอสะสมทีละ 15 นาทีจากนี้ไปเรื่อยๆ
    รันครั้งเดียวตอน bot เริ่ม และข้ามอัตโนมัติถ้ามี baseline sample อยู่แล้ว (กันเติมซ้ำทุกรอบ restart)"""
    from datetime import timedelta, timezone as tz
    SAMPLE_INTERVAL_HOURS = 6

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT MIN(timestamp) FROM market_sentiment WHERE asset IN ('BTC', 'ETH');")
                earliest = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM baseline_prices;")
                existing_count = cur.fetchone()[0]
    except Exception as e:
        print(f"Baseline backfill error (reading range): {type(e).__name__}: {e}")
        return

    if not earliest:
        print("Baseline backfill: ยังไม่มีข่าว BTC/ETH ในระบบ ข้ามการเติมย้อนหลัง")
        return
    if existing_count > 0:
        print(f"Baseline backfill: มี baseline sample อยู่แล้ว {existing_count} รายการ ข้ามการเติมย้อนหลัง (กันซ้ำ)")
        return

    if earliest.tzinfo is None:
        earliest = earliest.replace(tzinfo=tz.utc)
    now_utc = datetime.now(tz.utc)

    sample_times = []
    t = earliest
    while t <= now_utc:
        sample_times.append(t)
        t += timedelta(hours=SAMPLE_INTERVAL_HOURS)

    print(f"Baseline backfill: จะเติม {len(sample_times)} จุดเวลา x 2 สินทรัพย์ (BTC/ETH) "
          f"ย้อนหลังตั้งแต่ {earliest.isoformat()} ถึงตอนนี้ (ทุก {SAMPLE_INTERVAL_HOURS} ชม.) "
          f"— ใช้เวลาสักพัก เพราะต้องเรียก CoinGecko หลายร้อยครั้ง")

    done = 0
    for sample_dt in sample_times:
        for asset, coingecko_id in COINGECKO_IDS.items():
            price_now = get_price_at(coingecko_id, sample_dt)
            time.sleep(1.5)
            if price_now is None:
                continue

            windows = {
                "price_15m": sample_dt + timedelta(minutes=15),
                "price_1h": sample_dt + timedelta(hours=1),
                "price_4h": sample_dt + timedelta(hours=4),
                "price_24h": sample_dt + timedelta(hours=24),
            }
            updates = {}
            for column, target_dt in windows.items():
                if target_dt > now_utc:
                    continue
                price = get_price_at(coingecko_id, target_dt)
                if price is not None:
                    updates[column] = price
                time.sleep(1.5)

            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cols = ["asset", "sampled_at", "price_now"] + list(updates.keys())
                        vals = [asset, sample_dt, price_now] + list(updates.values())
                        placeholders = ", ".join(["%s"] * len(vals))
                        cur.execute(
                            f"INSERT INTO baseline_prices ({', '.join(cols)}) VALUES ({placeholders});",
                            vals
                        )
                    conn.commit()
                done += 1
            except Exception as e:
                print(f"Baseline backfill save error ({asset} @ {sample_dt.isoformat()}): {type(e).__name__}: {e}")

    print(f"Baseline backfill: เติมย้อนหลังเสร็จสิ้น {done}/{len(sample_times) * len(COINGECKO_IDS)} รายการ")


def sample_baseline_price():
    """สุ่มเก็บราคาปัจจุบันของ BTC/ETH แบบไม่อิงข่าว (baseline) เพื่อไว้เทียบว่าตอนมีข่าว
    ราคาขยับ 'มากผิดปกติ' กว่าตอนไม่มีข่าวจริงไหม (วัด market impact ไม่ใช่แค่ทิศทาง)"""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                for asset in COINGECKO_IDS:
                    price = get_live_price(asset)
                    if price is not None:
                        cur.execute(
                            "INSERT INTO baseline_prices (asset, price_now) VALUES (%s, %s);",
                            (asset, price)
                        )
                    time.sleep(0.3)
            conn.commit()
    except Exception as e:
        print(f"Baseline sample error: {type(e).__name__}: {e}")


def update_pending_baseline_checks():
    """เติมราคา 15นาที/1ชม./4ชม./24ชม. ให้ baseline sample ที่ถึงเวลาแล้ว
    (โครงเดียวกับ update_pending_price_checks() แต่ใช้กับตาราง baseline_prices)"""
    windows = [
        ("price_15m", "15 minutes"),
        ("price_1h", "1 hour"),
        ("price_4h", "4 hours"),
        ("price_24h", "24 hours"),
    ]
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                for column, interval in windows:
                    cur.execute(
                        f"""
                        SELECT id, asset FROM baseline_prices
                        WHERE price_now IS NOT NULL
                          AND {column} IS NULL
                          AND sampled_at <= NOW() - INTERVAL '{interval}'
                        LIMIT 20;
                        """
                    )
                    rows = cur.fetchall()
                    for row_id, asset in rows:
                        price = get_live_price(asset)
                        if price is not None:
                            cur.execute(
                                f"UPDATE baseline_prices SET {column} = %s WHERE id = %s;",
                                (price, row_id)
                            )
                            conn.commit()
                        time.sleep(0.3)
    except Exception as e:
        print(f"Baseline price check update error: {type(e).__name__}: {e}")


def run_prompt_backtest_once(client):
    """เทส prompt ใหม่กับข่าวเก่าที่รู้ผลจริงแล้ว (มี price_4h) โดยเฉพาะกลุ่มที่ prompt เก่าพลาด:
    - confidence สูง (>=80) แต่ทายผิด
    - confidence ต่ำ (<80) แต่ทายถูก
    (เป็นกลุ่มที่ตรงประเด็นที่สุดว่า prompt ใหม่แก้ปัญหา confidence ไม่สัมพันธ์กับความแม่นยำได้จริงไหม)
    รันครั้งเดียวเท่านั้น (เช็คจาก prompt_backtest_results) เพื่อประหยัดโควตา Gemini ที่มีจำกัดมาก
    บันทึกผลเทียบเก่า-ใหม่ลง Neon ดูผลได้ผ่าน query หรือถาม Claude ทีหลัง"""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM prompt_backtest_results;")
                if cur.fetchone()[0] > 0:
                    print("Prompt backtest: เคยรันไปแล้ว ข้าม (กันเปลืองโควตา Gemini)")
                    return

                cur.execute("""
                    SELECT link, asset, title, sentiment, confidence,
                           (sentiment = 'Bullish') = (price_4h > price_at_alert) AS was_correct
                    FROM market_sentiment
                    WHERE asset IN ('BTC','ETH') AND sentiment IN ('Bullish','Bearish')
                      AND price_at_alert IS NOT NULL AND price_4h IS NOT NULL
                      AND price_4h <> price_at_alert AND confidence >= 80
                      AND (sentiment = 'Bullish') != (price_4h > price_at_alert)
                    ORDER BY confidence DESC LIMIT 3;
                """)
                high_conf_wrong = cur.fetchall()

                cur.execute("""
                    SELECT link, asset, title, sentiment, confidence,
                           (sentiment = 'Bullish') = (price_4h > price_at_alert) AS was_correct
                    FROM market_sentiment
                    WHERE asset IN ('BTC','ETH') AND sentiment IN ('Bullish','Bearish')
                      AND price_at_alert IS NOT NULL AND price_4h IS NOT NULL
                      AND price_4h <> price_at_alert AND confidence < 80
                      AND (sentiment = 'Bullish') = (price_4h > price_at_alert)
                    ORDER BY confidence ASC LIMIT 3;
                """)
                low_conf_right = cur.fetchall()

        candidates = list(high_conf_wrong) + list(low_conf_right)
        if not candidates:
            print("Prompt backtest: ไม่พบข่าวเก่าที่เข้าเงื่อนไขให้เทส ข้าม")
            return

        print(f"Prompt backtest: กำลังเทส prompt ใหม่กับข่าวเก่า {len(candidates)} รายการ (title เท่านั้น ไม่มี body เก่าเก็บไว้)")
        for link, asset, title, old_sentiment, old_confidence, old_correct in candidates:
            res = analyze_advanced(client, title, "", asset)
            time.sleep(GEMINI_CALL_DELAY_SECONDS)
            if res is None:
                print(f"Prompt backtest: Gemini ล้มเหลวสำหรับ '{title[:60]}' ข้ามรายการนี้")
                continue
            new_sentiment = res.get("sentiment", "Neutral")
            new_confidence = res.get("confidence", 50)
            new_reason = res.get("reason", "-")

            # ใช้ actual_up จากข้อมูลเดิม (คำนวณซ้ำจาก was_correct + old_sentiment เพื่อไม่ต้อง query อีกรอบ)
            actual_up = (old_sentiment == "Bullish") == old_correct
            new_correct = None
            if new_sentiment in ("Bullish", "Bearish"):
                new_correct = (new_sentiment == "Bullish") == actual_up

            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO prompt_backtest_results
                                (link, asset, title, old_sentiment, old_confidence, old_correct,
                                 new_sentiment, new_confidence, new_correct, new_reason)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                            """,
                            (link, asset, title, old_sentiment, old_confidence, old_correct,
                             new_sentiment, new_confidence, new_correct, new_reason)
                        )
                    conn.commit()
            except Exception as e:
                print(f"Prompt backtest save error: {type(e).__name__}: {e}")

        print(f"Prompt backtest: เทสเสร็จสิ้น {len(candidates)} รายการ ดูผลได้จากตาราง prompt_backtest_results")
    except Exception as e:
        print(f"Prompt backtest error: {type(e).__name__}: {e}")


def run_price_context_backtest_once(client):
    """เทสฟีเจอร์ 'บริบทราคาจริง' (price_context) กับข่าวเก่าชุดเดียวกับที่เคยเทส prompt ใหม่ไปแล้ว
    (แถวใน prompt_backtest_results ที่ยังไม่มี v3_sentiment) เพื่อเทียบ 3 แบบในแถวเดียวกัน:
    old_* = prompt เดิมตอนนั้นจริงๆ, new_* = prompt ใหม่ (body+confidence calibration, ไม่มีราคา),
    v3_* = prompt ใหม่ + บริบทราคาจริง (ราคาย้อนหลัง ณ ตอนนั้น ไม่ใช่ราคาปัจจุบัน)
    รันครั้งเดียวเท่านั้น (เช็คว่ามีแถวไหน v3_sentiment ยังเป็น NULL อยู่บ้าง) เพื่อประหยัดโควตา Gemini"""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT p.link, p.asset, p.title, p.old_sentiment, p.old_confidence, p.old_correct,
                           m.timestamp, m.price_at_alert
                    FROM prompt_backtest_results p
                    JOIN market_sentiment m ON m.link = p.link
                    WHERE p.v3_sentiment IS NULL
                      AND m.asset IN ('BTC','ETH') AND m.price_at_alert IS NOT NULL;
                """)
                rows = cur.fetchall()

        if not rows:
            print("Price-context backtest: เคยรันไปแล้ว หรือไม่มีแถวให้เทส ข้าม (กันเปลืองโควตา Gemini)")
            return

        print(f"Price-context backtest: กำลังเทสบริบทราคาย้อนหลังกับข่าวเก่า {len(rows)} รายการ")
        for link, asset, title, old_sentiment, old_confidence, old_correct, ts, price_at_alert in rows:
            coingecko_id = COINGECKO_IDS.get(asset)
            if not coingecko_id:
                continue
            if ts.tzinfo is None:
                from datetime import timezone as _tz
                ts = ts.replace(tzinfo=_tz.utc)

            price_context = get_historical_price_context(coingecko_id, ts, float(price_at_alert))

            res = analyze_advanced(client, title, "", asset, price_context)
            time.sleep(GEMINI_CALL_DELAY_SECONDS)
            if res is None:
                print(f"Price-context backtest: Gemini ล้มเหลวสำหรับ '{title[:60]}' ข้ามรายการนี้")
                continue
            v3_sentiment = res.get("sentiment", "Neutral")
            v3_confidence = res.get("confidence", 50)
            v3_reason = res.get("reason", "-")

            actual_up = (old_sentiment == "Bullish") == old_correct
            v3_correct = None
            if v3_sentiment in ("Bullish", "Bearish"):
                v3_correct = (v3_sentiment == "Bullish") == actual_up

            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE prompt_backtest_results
                            SET v3_sentiment = %s, v3_confidence = %s, v3_correct = %s,
                                v3_reason = %s, v3_change_1h = %s, v3_change_24h = %s
                            WHERE link = %s;
                            """,
                            (v3_sentiment, v3_confidence, v3_correct, v3_reason,
                             price_context.get("change_1h"), price_context.get("change_24h"), link)
                        )
                    conn.commit()
            except Exception as e:
                print(f"Price-context backtest save error: {type(e).__name__}: {e}")

        print(f"Price-context backtest: เทสเสร็จสิ้น {len(rows)} รายการ ดูผลได้จากคอลัมน์ v3_* ในตาราง prompt_backtest_results")
    except Exception as e:
        print(f"Price-context backtest error: {type(e).__name__}: {e}")


def load_processed_links():
    """โหลดลิงก์ที่เคยวิเคราะห์แล้วจาก Neon ตอน bot เริ่มทำงาน"""
    links = set()
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT link_url FROM processed_links;")
                links = {row[0] for row in cur.fetchall()}
    except Exception as e:
        print(f"Load processed_links error: {e}")
    return links


def save_records(rows, skipped_links=None):
    """บันทึกผลวิเคราะห์ + ลิงก์ที่ประมวลผลแล้วลง Neon (แทน CSV เดิม)
    skipped_links: ลิงก์ที่ Groq คัดว่าไม่ใช่ catalyst ต้องบันทึกลง processed_links ด้วย
    เพื่อไม่ต้องคัดกรองซ้ำในรอบถัดไป (ไม่ต้องมีแถวใน market_sentiment)"""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                for r in rows:
                    price_at_alert = get_live_price(r["asset"])
                    cur.execute(
                        """
                        INSERT INTO market_sentiment
                            (link, source, asset, title, sentiment, confidence, action, reason, price_at_alert, lag_minutes)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (link) DO NOTHING;
                        """,
                        (r["link"], r["source"], r["asset"], r["title"],
                         r["sentiment"], r["confidence"], r["action"], r["reason"], price_at_alert,
                         r.get("lag_minutes"))
                    )
                    cur.execute(
                        """
                        INSERT INTO processed_links (link_url)
                        VALUES (%s)
                        ON CONFLICT (link_url) DO NOTHING;
                        """,
                        (r["link"],)
                    )
                for link in (skipped_links or []):
                    cur.execute(
                        """
                        INSERT INTO processed_links (link_url)
                        VALUES (%s)
                        ON CONFLICT (link_url) DO NOTHING;
                        """,
                        (link,)
                    )
            conn.commit()
    except Exception as e:
        print(f"Save to Neon error: {e}")


def fetch_feed_custom(url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        return feedparser.parse(resp.content)
    except Exception:
        return feedparser.parse(url)


def background_bot_worker():
    print("=== Background Bot Active 24/7 (Neon-backed, Groq-screened) ===")
    client = genai.Client(api_key=GEMINI_API_KEY)
    groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
    processed_links = load_processed_links()
    print(f"Loaded {len(processed_links)} previously processed links from Neon.")
    # ลิงก์ที่ Gemini เพิ่งวิเคราะห์ล้มเหลว (โควตาเต็ม/503 ฯลฯ) — เก็บเวลาที่ล้มเหลวไว้ "ชั่วคราว" ในหน่วยความจำ
    # (ไม่ใช่ processed_links ถาวรใน Neon) เพื่อพักไม่ลองซ้ำทันทีในรอบสแกนถัดไป กันไม่ให้ข่าวตัวเดิมที่พัง
    # ค้างอยู่หัวคิวมาบล็อกข่าวใหม่ทุกรอบ 3 นาที แต่ก็ยังกลับมาลองใหม่ได้เองหลังพัก ไม่ทิ้งถาวรเหมือนเดิม
    failed_links = {}
    FAILED_RETRY_COOLDOWN_SECONDS = 1800  # พัก 30 นาทีก่อนลองข่าวเดิมอีกครั้ง
    backfill_historical_prices()  # เติมราคาย้อนหลังให้ข่าว BTC/ETH เก่า (รันครั้งเดียวตอนเริ่ม)
    backfill_baseline_prices()  # เติม baseline sample ย้อนหลัง (รันครั้งเดียว ใช้เทียบกับข่าวได้ทันที ไม่ต้องรอสะสมใหม่)
    # ปิดการรัน backtest อัตโนมัติตอน worker เริ่มทำงานแล้ว (ทั้ง run_prompt_backtest_once และ
    # run_price_context_backtest_once) เพราะมันแย่งโควตา Gemini ที่มีจำกัดมาก (20/วัน) ไปจากข่าวสดจริง
    # โดยเฉพาะ run_price_context_backtest_once ที่ยังไม่เคยสำเร็จสักรายการ (v3_sentiment เป็น NULL อยู่)
    # จะพยายามรันซ้ำทุกครั้งที่ worker restart แย่งโควตาไปเรื่อยๆ — ฟังก์ชันยังอยู่ในไฟล์ เรียกเองได้ทีหลัง
    # ถ้าต้องการเทสข่าวเก่าอีกครั้ง แต่ตอนนี้ให้ความสำคัญกับข่าวสดที่เข้ามาใหม่ก่อนเป็นหลัก

    scan_count = 0
    # สุ่มเก็บ baseline price ทุกๆ BASELINE_SAMPLE_EVERY รอบ (รอบละ INTERVAL_SECONDS วิ)
    # ค่าเริ่มต้น 5 รอบ x 180 วิ = ทุก 15 นาที ให้ความถี่ใกล้เคียงกับ window ที่ใช้วัด impact (15m)
    BASELINE_SAMPLE_EVERY = 5

    while True:
        try:
            scan_count += 1
            if scan_count % BASELINE_SAMPLE_EVERY == 0:
                sample_baseline_price()

            new_records = []
            skipped_links = []
            analyses_this_scan = 0
            skipped_by_groq = 0
            quota_hit = False
            consecutive_failures = 0
            # ถ้า Gemini พังติดกันหลายครั้งในรอบเดียว แปลว่าโควตาน่าจะหมดจริงๆ วันนี้
            # ให้หยุดยิงต่อ (กันเสียเวลา/เสีย call เปล่าๆ) แต่ "ไม่" mark ข่าวที่เหลือว่า processed
            # ต่างจากเดิมตรงที่ตัวข่าวที่ยังไม่ได้ลองเลยในรอบนี้จะไม่ถูกแตะต้องอะไรเลย รอรอบหน้าตามปกติ
            CONSECUTIVE_FAILURE_LIMIT = 3
            now_ts = time.time()
            price_context_cache = {}  # กันยิง CoinGecko ซ้ำถ้ามีข่าว asset เดียวกันหลายชิ้นในรอบเดียว
            for source_name, url in RSS_FEEDS.items():
                if quota_hit:
                    break
                feed = fetch_feed_custom(url)
                for entry in feed.entries:
                    if analyses_this_scan >= MAX_ANALYSES_PER_SCAN:
                        quota_hit = True
                        break

                    link = entry.get("link", "")
                    if not link or link in processed_links:
                        continue

                    # ลิงก์นี้เพิ่งพังไปเมื่อไม่นานมานี้ — พักไว้ก่อน ยังไม่ mark processed
                    # (จะได้ลองใหม่เองหลังพักครบ ไม่ทิ้งถาวรและไม่เปลือง Gemini call ซ้ำๆ)
                    failed_at = failed_links.get(link)
                    if failed_at is not None:
                        if (now_ts - failed_at) < FAILED_RETRY_COOLDOWN_SECONDS:
                            continue
                        failed_links.pop(link, None)  # พักครบแล้ว ล้างออกจาก cooldown ลองใหม่ได้

                    title = entry.get("title", "")
                    summary = entry.get("summary", "")
                    full_text = f"{title} {summary}"

                    if any(_word_match(full_text.upper(), k) for k in KEYWORDS):
                        # ด่านแรก: ให้ Groq คัดว่าเป็น catalyst จริงไหม ก่อนจะเปลือง Gemini quota
                        if groq_client and not screen_with_groq(groq_client, title):
                            processed_links.add(link)  # ไม่ต้องกลับมาดูซ้ำ ไม่ใช่ catalyst
                            skipped_links.append(link)
                            skipped_by_groq += 1
                            continue

                        asset_tag = detect_asset(full_text)
                        lag_minutes = get_news_lag_minutes(entry)

                        # ดึงราคาจริง+เทรนด์ล่าสุด (ถ้า asset นี้มีราคาให้ดึง เช่น BTC/ETH) มาป้อนเป็น
                        # context ให้ Gemini เห็นว่าตอนนี้ตลาดขึ้น/ลง/นิ่งอยู่ ไม่ใช่วิเคราะห์แค่ข้อความข่าวเปล่าๆ
                        if asset_tag not in price_context_cache:
                            price_context_cache[asset_tag] = get_price_context(asset_tag)
                        price_context = price_context_cache[asset_tag]

                        res = analyze_advanced(client, title, summary, asset_tag, price_context)
                        analyses_this_scan += 1
                        time.sleep(GEMINI_CALL_DELAY_SECONDS)  # กันยิงรัวจนชนโควตา

                        if res is None:
                            # Gemini ล้มเหลว (โควตาเต็ม/503 ฯลฯ) — "ไม่" mark ว่าประมวลผลแล้ว
                            # แค่จด timestamp ไว้ใน failed_links เพื่อพัก (cooldown) ไม่ลองซ้ำทันที
                            # แล้ว "continue" ไปข่าวถัดไปในรอบเดียวกันนี้เลย — ไม่ break ทั้ง scan
                            # เหมือนรอบที่แล้ว ซึ่งทำให้ข่าวตัวแรกที่พังบล็อกข่าวใหม่ทุกตัวทุกรอบ
                            failed_links[link] = time.time()
                            consecutive_failures += 1
                            print(f"[{time.strftime('%H:%M:%S')}] Gemini failed for '{title[:60]}' "
                                  f"— cooldown {FAILED_RETRY_COOLDOWN_SECONDS}s, will retry later "
                                  f"(consecutive fails: {consecutive_failures})")
                            if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                                # พังติดกันหลายครั้ง โควตาน่าจะหมดจริงจังวันนี้ — หยุดยิงต่อในรอบนี้
                                # เพื่อไม่เสียเวลา/call เปล่า แต่ข่าวที่เหลือยังไม่ถูกแตะเลย ไม่ใช่ถูกทิ้ง
                                quota_hit = True
                                break
                            continue

                        # สำเร็จแล้ว เอาลิงก์นี้ออกจาก cooldown list ถ้าเคยพังมาก่อน (ไม่ต้องพักอีกต่อไป)
                        failed_links.pop(link, None)
                        consecutive_failures = 0

                        record = {
                            "source": source_name,
                            "asset": asset_tag,
                            "title": title,
                            "sentiment": res.get("sentiment", "Neutral"),
                            "confidence": res.get("confidence", 50),
                            "action": res.get("action", "Wait and See"),
                            "reason": res.get("reason", "-"),
                            "link": link,
                            "lag_minutes": lag_minutes,
                        }
                        new_records.append(record)
                        processed_links.add(link)

                        if record["confidence"] >= ALERT_CONFIDENCE_THRESHOLD and record["sentiment"] in ["Bullish", "Bearish"]:
                            send_telegram_alert(source_name, asset_tag, title, record["sentiment"],
                                                 record["confidence"], record["action"], record["reason"], link,
                                                 lag_minutes=lag_minutes)

            if new_records or skipped_links:
                save_records(new_records, skipped_links=skipped_links)
                print(f"[{time.strftime('%H:%M:%S')}] Saved {len(new_records)} new items to Neon, "
                      f"skipped {skipped_by_groq} non-catalyst items via Groq."
                      + (" (Gemini cap hit, more pending next round)" if quota_hit else ""))
            else:
                print(f"[{time.strftime('%H:%M:%S')}] Scan complete. No new items.")

            update_pending_price_checks()  # เช็คว่ามีข่าวไหนถึงเวลาบันทึกราคาหลังข่าวแล้วบ้าง
            update_pending_baseline_checks()  # เช็ค baseline sample ที่ถึงเวลาบันทึกราคาด้วยเหมือนกัน
        except Exception as e:
            print(f"Worker Error: {e}")

        time.sleep(INTERVAL_SECONDS)


# เริ่ม Background Bot Thread
@st.cache_resource
def start_bot():
    thread = threading.Thread(target=background_bot_worker, daemon=True)
    thread.start()
    return True


start_bot()


# ================= STREAMLIT UI =================
# หน้าตาเว็บทั้งหมดอยู่แยกในไฟล์ dashboard.py แล้ว แก้ดีไซน์/เลย์เอาต์ไปแก้ที่นั่น
# dashboard.py import config.py เท่านั้น (ไม่ import app.py) เพื่อกัน app.py ถูกรันซ้ำ
import dashboard
dashboard.render()
