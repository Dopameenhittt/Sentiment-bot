import csv
import os
import time
import json
import feedparser
import requests
from google import genai

# ================= CONFIGURATION =================
GEMINI_API_KEY = "AQ.Ab8RN6LcdK1NXuQFfuGeTBJXVnxz2vVBB__122KbqQQPw1dUtw"

RSS_FEEDS = {
    "Reddit/WSB": "https://www.reddit.com/r/wallstreetbets/new/.rss",
    "Reddit/Crypto": "https://www.reddit.com/r/CryptoCurrency/new/.rss",
    "Reddit/Stocks": "https://www.reddit.com/r/stocks/new/.rss",
    "Yahoo Finance": "https://finance.yahoo.com/news/rssindex",
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/"
}

# กำหนดสินทรัพย์ที่ต้องการแท็ก
TARGET_ASSETS = ["BTC", "ETH", "NVDA", "GOLD", "XAUUSD", "FED", "SPY", "TSLA"]
KEYWORDS = ["BTC", "ETH", "FED", "NVDA", "GOLD", "XAUUSD", "CALLS", "PUTS", "BITCOIN", "RATE CUT", "INFLATION", "SPY", "TSLA"]
CSV_FILE = "market_sentiment.csv"
INTERVAL_SECONDS = 300  # เช็กทุก 5 นาที

TELEGRAM_BOT_TOKEN = "8286819909:AAEgdSYG2-ogY4Ir3P_xJi1nzKapKcle75Q"
TELEGRAM_CHAT_ID = "8926277994"
# =================================================

client = genai.Client(api_key=GEMINI_API_KEY.strip())
processed_links = set()

if os.path.isfile(CSV_FILE):
    try:
        with open(CSV_FILE, mode="r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if len(row) >= 8:
                    processed_links.add(row[7])
    except Exception:
        pass

def detect_asset(text):
    text_upper = text.upper()
    for asset in TARGET_ASSETS:
        if asset in text_upper or (asset == "BTC" and "BITCOIN" in text_upper) or (asset == "GOLD" and "XAU" in text_upper):
            return asset
    return "General Market"

def send_telegram_alert(source, asset, title, sentiment, confidence, action, reason, link):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    emoji = "🟢" if sentiment == "Bullish" else "🔴" if sentiment == "Bearish" else "⚪"
    msg = (
        f"{emoji} *Market Alert [{asset} - {sentiment}]* ({confidence}% Conf)\n\n"
        f"📡 *Source:* {source}\n"
        f"📌 *Title:* {title}\n"
        f"⚡ *Action:* `{action}`\n"
        f"💡 *Reason:* {reason}\n"
        f"🔗 [View Source]({link})"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"Telegram Error: {e}")

def analyze_advanced(title):
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
            model="gemini-2.5-flash",
            contents=prompt,
            config={"response_mime_type": "application/json"}
        )
        return json.loads(response.text.strip())
    except Exception:
        return {
            "sentiment": "Neutral",
            "confidence": 50,
            "action": "Wait and See",
            "reason": "Analysis unavailable"
        }

def save_to_csv(rows):
    file_exists = os.path.isfile(CSV_FILE)
    with open(CSV_FILE, mode="a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["Source", "Asset", "Title", "Sentiment", "Confidence", "Action", "Reason", "Link", "Timestamp"])
        for r in rows:
            writer.writerow([r["source"], r["asset"], r["title"], r["sentiment"], r["confidence"], r["action"], r["reason"], r["link"], r["time"]])

def scan_all():
    print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] กำลังสแกนทุกช่องทาง...")
    new_records = []

    for source_name, url in RSS_FEEDS.items():
        feed = feedparser.parse(url)
        for entry in feed.entries:
            link = entry.get("link", "")
            if link in processed_links:
                continue

            title = entry.get("title", "")
            summary = entry.get("summary", "")
            full_text = f"{title} {summary}"

            if any(k.lower() in full_text.lower() for k in KEYWORDS):
                asset_tag = detect_asset(full_text)
                print(f"[{source_name} | {asset_tag}] ตรวจพบ: {title}")
                res = analyze_advanced(title)

                record = {
                    "source": source_name,
                    "asset": asset_tag,
                    "title": title,
                    "sentiment": res.get("sentiment", "Neutral"),
                    "confidence": res.get("confidence", 50),
                    "action": res.get("action", "Wait and See"),
                    "reason": res.get("reason", "-"),
                    "link": link,
                    "time": time.strftime("%Y-%m-%d %H:%M:%S")
                }
                new_records.append(record)
                processed_links.add(link)

                if record["confidence"] >= 65 and record["sentiment"] in ["Bullish", "Bearish"]:
                    send_telegram_alert(source_name, asset_tag, title, record["sentiment"], record["confidence"], record["action"], record["reason"], link)

    if new_records:
        save_to_csv(new_records)
        print(f"บันทึกข้อมูลใหม่ {len(new_records)} รายการ")
    else:
        print("ยังไม่มีข่าวหรือโพสต์ใหม่")

print("=== Market Sentiment Bot (Asset Tagging + Real-time Alerts) ===")
while True:
    try:
        scan_all()
        time.sleep(INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\nหยุดการทำงานของบอท")
        break
    except Exception as e:
        print(f"System Error: {e}")
        time.sleep(10)