"""
config.py — ค่าตั้งค่าและฟังก์ชันที่ใช้ร่วมกันระหว่าง app.py กับ dashboard.py

สำคัญมาก: ไฟล์นี้ต้อง "ไม่มี" โค้ด Streamlit ระดับบนสุด (top-level) เด็ดขาด
(ห้ามมี st.set_page_config, st.title, การเริ่ม thread ฯลฯ)
เพราะทั้ง app.py และ dashboard.py จะ import ไฟล์นี้พร้อมกัน — ถ้ามีโค้ด Streamlit
อยู่ตรงนี้ มันจะถูกรันซ้ำทุกครั้งที่มีการ import ทำให้พฤติกรรมของแอปผิดเพี้ยน
"""
import os
import psycopg2

# ทุกค่าสำคัญอ่านจาก Environment Variables แทนการฝังในโค้ด
# ต้องตั้งค่าเหล่านี้ใน Render > Environment ก่อนรัน:
#   GEMINI_API_KEY, GROQ_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DATABASE_URL
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "").strip()

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is not set. Add it in Render > Environment.")

# เรียงลำดับความสำคัญ: ข่าวการจริง (catalyst) ก่อน กระแสความคิดเห็นใน Reddit ไว้ท้ายสุด
RSS_FEEDS = {
    "Yahoo Finance": "https://finance.yahoo.com/news/rssindex",
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Reddit/WSB": "https://www.reddit.com/r/wallstreetbets/new/.rss",
    "Reddit/Crypto": "https://www.reddit.com/r/CryptoCurrency/new/.rss",
    "Reddit/Stocks": "https://www.reddit.com/r/stocks/new/.rss",
}

TARGET_ASSETS = ["BTC", "ETH", "NVDA", "GOLD", "XAUUSD", "FED", "SPY", "TSLA"]
KEYWORDS = ["BTC", "ETH", "FED", "NVDA", "GOLD", "XAUUSD", "CALLS", "PUTS", "BITCOIN", "ETHEREUM", "RATE CUT", "INFLATION", "SPY", "TSLA"]
INTERVAL_SECONDS = 180  # ตรวจเช็กทุก 3 นาที
MAX_ANALYSES_PER_SCAN = 8   # จำกัดจำนวนข่าวที่ยิงเข้า Gemini ต่อรอบ กันชนโควตา
GEMINI_CALL_DELAY_SECONDS = 4  # หน่วงเวลาระหว่างการเรียก Gemini แต่ละครั้ง
ALERT_CONFIDENCE_THRESHOLD = 80  # day trade ต้องมั่นใจสูงกว่าการถือยาว ถึงจะแจ้งเตือน


def get_conn():
    """เปิด connection ใหม่ทุกครั้งที่เรียก กัน connection ค้าง/หลุด"""
    return psycopg2.connect(DATABASE_URL)
