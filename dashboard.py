import os
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px

st.set_page_config(page_title="AI Market Sentiment & Fear/Greed Terminal", layout="wide")
st.title("📈 AI Market Sentiment & Fear/Greed Terminal")

CSV_FILE = "market_sentiment.csv"

# Sidebar Control
st.sidebar.header("🕹 Control Panel")
if st.sidebar.button("🔄 Refresh Data", use_container_width=True):
    st.rerun()

if not os.path.exists(CSV_FILE):
    st.warning("ยังไม่พบไฟล์ข้อมูล กำลังรอบอทบันทึกข้อมูลรอบแรก...")
    st.stop()

try:
    df = pd.read_csv(CSV_FILE, on_bad_lines="skip", encoding="utf-8-sig")
except Exception as e:
    st.error(f"เกิดข้อผิดพลาดในการโหลดข้อมูล: {e}")
    st.stop()

if df.empty or "Sentiment" not in df.columns:
    st.info("ยังไม่มีข้อมูลเพียงพอสำหรับการวิเคราะห์...")
    st.stop()

# Filter Asset Ticker
if "Asset" in df.columns:
    assets_list = ["ALL"] + sorted(df["Asset"].dropna().unique().tolist())
    selected_asset = st.sidebar.selectbox("🎯 Filter Asset / Ticker:", assets_list)
    if selected_asset != "ALL":
        df = df[df["Asset"] == selected_asset]

total_posts = len(df)
bullish_count = len(df[df["Sentiment"].str.contains("Bullish", case=False, na=False)])
bearish_count = len(df[df["Sentiment"].str.contains("Bearish", case=False, na=False)])
neutral_count = len(df[df["Sentiment"].str.contains("Neutral", case=False, na=False)])

# Custom Fear & Greed Index Calculation (0 = Extreme Fear, 100 = Extreme Greed)
if total_posts > 0:
    net_score = (bullish_count - bearish_count) / total_posts
    fear_greed_val = round(((net_score + 1) / 2) * 100, 1)
else:
    fear_greed_val = 50.0

# Metrics Header
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Total Analyzed", total_posts)
c2.metric("Bullish 🟢", bullish_count)
c3.metric("Bearish 🔴", bearish_count)
c4.metric("Neutral ⚪", neutral_count)
c5.metric("Index Score", f"{fear_greed_val}/100")

st.markdown("---")

# Gauge & Charts Row
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
                {'range': [0, 25], 'color': "#c0392b"},    # Extreme Fear
                {'range': [25, 45], 'color': "#e67e22"},   # Fear
                {'range': [45, 55], 'color': "#7f8c8d"},   # Neutral
                {'range': [55, 75], 'color': "#27ae60"},   # Greed
                {'range': [75, 100], 'color': "#2ecc71"}   # Extreme Greed
            ],
            'threshold': {
                'line': {'color': "black", 'width': 4},
                'thickness': 0.75,
                'value': fear_greed_val
            }
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

# Latest Data Grid
st.subheader("📰 Real-time News & Trade Recommendation")
if "Timestamp" in df.columns:
    st.dataframe(df.sort_values(by="Timestamp", ascending=False), use_container_width=True)
else:
    st.dataframe(df, use_container_width=True)