import os
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
from sqlalchemy import create_engine, text

# ================= CONFIGURATION (Secure via Env) =================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

RSS_FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Yahoo Finance": "https://finance.yahoo.com/news/rssindex",
    "Reddit/WSB": "https://www.reddit.com/r/wallstreetbets/new/.rss",
    "Reddit/Crypto": "https://www.reddit.com/r/CryptoCurrency/new/.rss",
    "Reddit/Stocks": "https://www.reddit.com/r/stocks/new/.rss"
}

TARGET_ASSETS = ["BTC", "ETH", "NVDA", "GOLD", "XAUUSD", "FED", "SPY", "TSLA"]
KEYWORDS = ["BTC", "ETH", "FED", "NVDA", "GOLD", "XAUUSD", "CALLS", "PUTS", "BITCOIN", "RATE CUT", "INFLATION", "SPY", "TSLA", "CRYPTO"]
INTERVAL_SECONDS = 180
# =================================================================

def get_db_engine():
    if not DATABASE_URL:
        return None
    return create_engine(DATABASE_URL)

def init_db():
    engine = get_db_engine()
    if not engine:
        return
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS market_sentiment (
                id SERIAL PRIMARY KEY,
                source VARCHAR(100),
                asset VARCHAR(50),
                title TEXT,
                sentiment VARCHAR(50),
                confidence INT,
                action VARCHAR(100),
                reason TEXT,
                link TEXT UNIQUE,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """))
        conn.commit()

init_db()

def detect_asset(text_content):
    text_upper = text_content.upper()
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
    if not client:
        return {"sentiment": "Neutral", "confidence": 50, "action": "Wait and See", "reason": "Missing API Key"}

    prompt = f"""
    Analyze the financial market sentiment of this headline: "{title}"
    Respond strictly in JSON format with these exact keys:
    {{
        "sentiment": "Bullish",
        "confidence": 80,
        "action": "Buy Call/Long",
        "reason": "1 short concise sentence explaining why"
    }}
    Allowed sentiment: "Bullish", "Bearish", "Neutral".
    Allowed action: "Buy Call/Long", "Buy Put/Short", "Wait and See".
    """
    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )
        raw_text = response.text.strip()
        if "```" in raw_text:
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        data = json.loads(raw_text.strip())
        return {
            "sentiment": data.get("sentiment", "Neutral"),
            "confidence": int(data.get("confidence", 50)),
            "action": data.get("action", "Wait and See"),
            "reason": data.get("reason", "Analysis complete")
        }
    except Exception as e:
        print(f"[Gemini Exception for '{title[:25]}...']: {e}")
        return {
            "sentiment": "Neutral",
            "confidence": 50,
            "action": "Wait and See",
            "reason": f"Err: {str(e)[:30]}"
        }

def save_to_neon(rows):
    engine = get_db_engine()
    if not engine or not rows:
        return
    with engine.connect() as conn:
        for r in rows:
            conn.execute(text("""
                INSERT INTO market_sentiment (source, asset, title, sentiment, confidence, action, reason, link)
                VALUES (:source, :asset, :title, :sentiment, :confidence, :action, :reason, :link)
                ON CONFLICT (link) DO NOTHING;
            """), r)
        conn.commit()

def fetch_feed_custom(url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        return feedparser.parse(resp.content)
    except Exception:
        return feedparser.parse(url)

def background_bot_worker():
    print("=== Background Bot Active with Neon DB ===")
    key = os.getenv("GEMINI_API_KEY", "").strip()
    client = genai.Client(api_key=key) if key else None

    engine = get_db_engine()
    processed_links = set()

    if engine:
        try:
            with engine.connect() as conn:
                res = conn.execute(text("SELECT link FROM market_sentiment")).fetchall()
                processed_links = {row[0] for row in res}
        except Exception as e:
            print(f"DB Load links error: {e}")

    while True:
        try:
            new_records = []
            for source_name, url in RSS_FEEDS.items():
                feed = fetch_feed_custom(url)
                for entry in feed.entries:
                    link = entry.get("link", "")
                    if not link or link in processed_links:
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
                            "confidence": int(res.get("confidence", 50)),
                            "action": res.get("action", "Wait and See"),
                            "reason": res.get("reason", "-"),
                            "link": link
                        }
                        new_records.append(record)
                        processed_links.add(link)

                        if record["confidence"] >= 65 and record["sentiment"] in ["Bullish", "Bearish"]:
                            send_telegram_alert(source_name, asset_tag, title, record["sentiment"], record["confidence"], record["action"], record["reason"], link)
                        
                        time.sleep(1) # ป้องกัน Rate Limit

            if new_records:
                save_to_neon(new_records)
                print(f"[{time.strftime('%H:%M:%S')}] Saved {len(new_records)} items to Neon Database.")
        except Exception as e:
            print(f"Worker Error: {e}")

        time.sleep(INTERVAL_SECONDS)

@st.cache_resource
def start_bot():
    thread = threading.Thread(target=background_bot_worker, daemon=True)
    thread.start()
    return True

start_bot()

# ================= STREAMLIT UI =================
st.set_page_config(page_title="AI Market Sentiment Terminal", layout="wide")
st_autorefresh(interval=30000, key="datarefresh")

st.title("📈 AI Market Sentiment & Fear/Greed Terminal (Neon DB Persistent)")

st.sidebar.header("🕹 Control Panel")
if st.sidebar.button("🔄 Force Refresh"):
    st.rerun()

engine = get_db_engine()
if not engine:
    st.error("DATABASE_URL is missing! Please configure Environment Variables.")
    st.stop()

try:
    with engine.connect() as conn:
        df = pd.read_sql(text("SELECT source, asset, title, sentiment, confidence, action, reason, link, timestamp FROM market_sentiment ORDER BY timestamp DESC LIMIT 200"), conn)
except Exception as e:
    st.error(f"Error loading from Database: {e}")
    st.stop()

if df.empty or "sentiment" not in df.columns:
    st.info("กำลังรอ Background Bot สแกนและบันทึกข้อมูลเข้า Neon Database...")
    st.stop()

if "asset" in df.columns:
    assets_list = ["ALL"] + sorted(df["asset"].dropna().unique().tolist())
    selected_asset = st.sidebar.selectbox("🎯 Filter Asset / Ticker:", assets_list)
    if selected_asset != "ALL":
        df = df[df["asset"] == selected_asset]

total_posts = len(df)
bullish_count = len(df[df["sentiment"].str.contains("Bullish", case=False, na=False)])
bearish_count = len(df[df["sentiment"].str.contains("Bearish", case=False, na=False)])
neutral_count = len(df[df["sentiment"].str.contains("Neutral", case=False, na=False)])

if total_posts > 0:
    net_score = (bullish_count - bearish_count) / total_posts
    fear_greed_val = round(((net_score + 1) / 2) * 100, 1)
else:
    fear_greed_val = 50.0

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Total in DB", total_posts)
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
    st.plotly_chart(fig_gauge)

with col_pie:
    st.subheader("📊 Sentiment Breakdown")
    fig_pie = px.pie(
        df, 
        names="sentiment", 
        color="sentiment",
        color_discrete_map={"Bullish": "#2ecc71", "Bearish": "#e74c3c", "Neutral": "#95a5a6"}
    )
    fig_pie.update_layout(height=350, margin=dict(l=20, r=20, t=50, b=20))
    st.plotly_chart(fig_pie)

st.subheader("📰 Real-time News & Trade Recommendation (Persistent from Neon DB)")
st.dataframe(df)