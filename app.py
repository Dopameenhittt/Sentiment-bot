import time
import json
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


def detect_asset(text):
    text_upper = text.upper()
    for asset in TARGET_ASSETS:
        if asset in text_upper or (asset == "BTC" and "BITCOIN" in text_upper) or (asset == "GOLD" and "XAU" in text_upper):
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


def analyze_advanced(client, title):
    prompt = f"""
    Analyze the financial market sentiment of this title: "{title}"
    Respond strictly in JSON format with these exact keys:
    {{
        "sentiment": "Bullish" or "Bearish" or "Neutral",
        "confidence": 80,
        "action": "Buy Call/Long" or "Buy Put/Short" or "Wait and See",
        "reason": "1 short concise sentence explaining why"
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
        return {
            "sentiment": "Neutral",
            "confidence": 50,
            "action": "Wait and See",
            "reason": "Analysis unavailable"
        }


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
                            (link, source, asset, title, sentiment, confidence, action, reason, price_at_alert)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (link) DO NOTHING;
                        """,
                        (r["link"], r["source"], r["asset"], r["title"],
                         r["sentiment"], r["confidence"], r["action"], r["reason"], price_at_alert)
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
    backfill_historical_prices()  # เติมราคาย้อนหลังให้ข่าว BTC/ETH เก่า (รันครั้งเดียวตอนเริ่ม)

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

                    title = entry.get("title", "")
                    summary = entry.get("summary", "")
                    full_text = f"{title} {summary}"

                    if any(k.lower() in full_text.lower() for k in KEYWORDS):
                        # ด่านแรก: ให้ Groq คัดว่าเป็น catalyst จริงไหม ก่อนจะเปลือง Gemini quota
                        if groq_client and not screen_with_groq(groq_client, title):
                            processed_links.add(link)  # ไม่ต้องกลับมาดูซ้ำ ไม่ใช่ catalyst
                            skipped_links.append(link)
                            skipped_by_groq += 1
                            continue

                        asset_tag = detect_asset(full_text)
                        lag_minutes = get_news_lag_minutes(entry)
                        res = analyze_advanced(client, title)
                        analyses_this_scan += 1
                        time.sleep(GEMINI_CALL_DELAY_SECONDS)  # กันยิงรัวจนชนโควตา

                        record = {
                            "source": source_name,
                            "asset": asset_tag,
                            "title": title,
                            "sentiment": res.get("sentiment", "Neutral"),
                            "confidence": res.get("confidence", 50),
                            "action": res.get("action", "Wait and See"),
                            "reason": res.get("reason", "-"),
                            "link": link,
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
