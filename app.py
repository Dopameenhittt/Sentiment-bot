import os
import csv
import time
import json
import os
import csv
import time
import json
import threading
import feedparser
import requests
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import streamlit as st
from streamlit_autorefresh import st_autorefresh
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

TARGET_ASSETS = ["BTC", "ETH", "NVDA", "GOLD", "XAUUSD", "FED", "SPY", "TSLA"]
KEYWORDS = ["BTC", "ETH", "FED", "NVDA", "GOLD", "XAUUSD", "CALLS", "PUTS", "BITCOIN", "RATE CUT", "INFLATION", "SPY", "TSLA"]
CSV_FILE = "market_sentiment.csv"
INTERVAL_SECONDS = 180  # ปรับเป็นตรวจเช็กทุกๆ 3 นาที

TELEGRAM_BOT_TOKEN = "8286819909:AAEgdSYG2-ogY4Ir3P_xJi1nzKapKcle75Q"
TELEGRAM_CHAT_ID = "8926277994"
# =================================================

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

def fetch_feed_custom(url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        return feedparser.parse(resp.content)
    except Exception:
        return feedparser.parse(url)

def background_bot_worker():
    print("=== Background Bot Active 24/7 ===")
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

    while True:
        try:
            new_records = []
            for source_name, url in RSS_FEEDS.items():
                feed = fetch_feed_custom(url)
                for entry in feed.entries:
                    link = entry.get("link", "")
                    if link in processed_links:
                        continue

                    title = entry.get("title", "")
                    summary = entry.get("summary", "")
                    full_text = f"{title} {summary}"

                    if any(k.lower() in full_text.lower() for k in KEYWORDS):
                        asset_tag = detect_asset(full_text)
                        res = analyze_advanced(client, title)

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
                print(f"[{time.strftime('%H:%M:%S')}] Saved {len(new_records)} new items.")
            else:
                print(f"[{time.strftime('%H:%M:%S')}] Scan complete. No new items.")
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

# ================= 2. STREAMLIT UI =================
st.set_page_config(page_title="AI Market Sentiment Terminal", layout="wide")

# Auto refresh หน้าเว็บทุก 30 วินาทีอัตโนมัติ
st_autorefresh(interval=30000, key="datarefresh")

st.title("📈 AI Market Sentiment & Fear/Greed Terminal (24/7 Cloud)")

st.sidebar.header("🕹 Control Panel")
if st.sidebar.button("🔄 Force Refresh", use_container_width=True):
    st.rerun()

if not os.path.exists(CSV_FILE):
    st.info("กำลังรอ Background Bot สแกนและสร้างไฟล์ข้อมูลรอบแรก...")
    st.stop()

try:
    df = pd.read_csv(CSV_FILE, on_bad_lines="skip", encoding="utf-8-sig")
except Exception as e:
    st.error(f"Error loading CSV: {e}")
    st.stop()

if df.empty or "Sentiment" not in df.columns:
    st.info("ยังไม่มีข้อมูลเพียงพอสำหรับการวิเคราะห์...")
    st.stop()

if "Asset" in df.columns:
    assets_list = ["ALL"] + sorted(df["Asset"].dropna().unique().tolist())
    selected_asset = st.sidebar.selectbox("🎯 Filter Asset / Ticker:", assets_list)
    if selected_asset != "ALL":
        df = df[df["Asset"] == selected_asset]

total_posts = len(df)
bullish_count = len(df[df["Sentiment"].str.contains("Bullish", case=False, na=False)])
bearish_count = len(df[df["Sentiment"].str.contains("Bearish", case=False, na=False)])
neutral_count = len(df[df["Sentiment"].str.contains("Neutral", case=False, na=False)])

if total_posts > 0:
    net_score = (bullish_count - bearish_count) / total_posts
    fear_greed_val = round(((net_score + 1) / 2) * 100, 1)
else:
    fear_greed_val = 50.0

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Total Analyzed", total_posts)
c2.metric("Bullish 🟢", bullish_count)
c3.metric("Bearish 🔴", bearish_count)
c4.metric("Neutral ⚪", neutral_count)
c5.metric("Index Score", f"{fear_greed_val}/100")

st.markdown("---")

col_gauge, col_pie = st.columns([1, 1])

with col_gauge:
    st.subheader("🧭 Custom Market Fear & Greed Index")
    fig_gauge = go.Figure(go.Indicator(
        mode="gauge+number",
        value=fear_greed_val,
        title={'text': "Market Sentiment Index"},
        gauge={
            'axis': {'range': [0, 100]},
            'bar': {'color': "#ffffff"},
            'steps': [
                {'range': [0, 25], 'color': "#c0392b"},
                {'range': [25, 45], 'color': "#e67e22"},
                {'range': [45, 55], 'color': "#7f8c8d"},
                {'range': [55, 75], 'color': "#27ae60"},
                {'range': [75, 100], 'color': "#2ecc71"}
            ],
            'threshold': {'line': {'color': "black", 'width': 4}, 'thickness': 0.75, 'value': fear_greed_val}
        }
    ))
    fig_gauge.update_layout(height=350, margin=dict(l=20, r=20, t=50, b=20))
    st.plotly_chart(fig_gauge, use_container_width=True)

with col_pie:
    st.subheader("📊 Sentiment Breakdown")
    fig_pie = px.pie(
        df, 
        names="Sentiment", 
        color="Sentiment",
        color_discrete_map={"Bullish": "#2ecc71", "Bearish": "#e74c3c", "Neutral": "#95a5a6"}
    )
    fig_pie.update_layout(height=350, margin=dict(l=20, r=20, t=50, b=20))
    st.plotly_chart(fig_pie, use_container_width=True)

st.subheader("📰 Real-time News & Trade Recommendation")
if "Timestamp" in df.columns:
    st.dataframe(df.sort_values(by="Timestamp", ascending=False), use_container_width=True)
else:
    st.dataframe(df, use_container_width=True)