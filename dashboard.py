"""
dashboard.py — หน้าตาเว็บ (Streamlit UI) ล้วนๆ
แก้ไฟล์นี้เพื่อปรับดีไซน์/เลย์เอาต์ได้เลย โดยไม่กระทบ logic ฝั่ง backend ใน app.py

Import จาก config.py เท่านั้น (ห้าม import app.py) — เพราะ app.py คือสคริปต์หลักที่ถูกรัน
ด้วย `streamlit run app.py` ถ้า dashboard.py ไป import app.py จะทำให้ app.py ถูกรันซ้ำ
เป็นรอบที่สองแบบเงียบๆ (เปิด background thread ซ้ำ, Streamlit ทำงานผิดเพี้ยน จนหน้าเว็บไม่ขึ้น)
"""
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from config import get_conn, ALERT_CONFIDENCE_THRESHOLD


def render():
    """เรียกจาก app.py หลัง start_bot() เพื่อวาดหน้าเว็บทั้งหมด"""
    _render_ui()


# ================= STREAMLIT UI =================
def _render_ui():
    st.set_page_config(page_title="AI Market Sentiment Terminal", page_icon="📈", layout="wide")

    # Auto refresh หน้าเว็บทุก 30 วินาทีอัตโนมัติ
    st_autorefresh(interval=30000, key="datarefresh")

    # ---------- Custom Theme (light minimal look, softer palette) ----------
    st.markdown("""
    <style>
        .stApp { background-color: #f6f5f3; }
        .main .block-container { padding-top: 1.5rem; max-width: 1400px; }

        .terminal-header {
            display: flex; justify-content: space-between; align-items: center;
            padding: 1.2rem 1.6rem; border-radius: 14px; margin-bottom: 1.2rem;
            background: #fdfdfc;
            border: 1px solid #e7e5e1;
            box-shadow: 0 1px 2px rgba(0,0,0,0.03);
        }
        .terminal-header h1 { margin: 0; font-size: 1.55rem; color: #2d2a26; }
        .terminal-header p { margin: 0.2rem 0 0 0; color: #8a8579; font-size: 0.85rem; }
        .live-badge {
            display: inline-flex; align-items: center; gap: 6px;
            background: #eef6f0; color: #3f7d54;
            padding: 5px 12px; border-radius: 20px; font-size: 0.8rem; font-weight: 600;
            border: 1px solid #cfe6d6;
        }
        .live-dot {
            width: 8px; height: 8px; border-radius: 50%; background: #4d9e68;
            animation: pulse 1.8s infinite;
        }
        @keyframes pulse {
            0% { box-shadow: 0 0 0 0 rgba(77,158,104,0.45); }
            70% { box-shadow: 0 0 0 8px rgba(77,158,104,0); }
            100% { box-shadow: 0 0 0 0 rgba(77,158,104,0); }
        }

        div[data-testid="stMetric"] {
            background: #fdfdfc; border: 1px solid #e7e5e1; border-radius: 12px;
            padding: 0.9rem 1rem; box-shadow: 0 1px 2px rgba(0,0,0,0.02);
        }
        div[data-testid="stMetricLabel"] { color: #8a8579; }
        div[data-testid="stMetricValue"] { color: #2d2a26; }

        .sentiment-badge {
            padding: 3px 10px; border-radius: 20px; font-size: 0.78rem; font-weight: 700;
            display: inline-block;
        }
        .badge-bull { background: #eef6f0; color: #3f7d54; }
        .badge-bear { background: #fbeeec; color: #b3483c; }
        .badge-neutral { background: #f1efec; color: #8a8579; }

        section[data-testid="stSidebar"] { background: #fdfdfc; border-right: 1px solid #e7e5e1; }
        h2, h3 { color: #2d2a26; }
        .stTabs [data-baseweb="tab"] { color: #8a8579; }
        .stTabs [aria-selected="true"] { color: #2d2a26; }
    </style>
    """, unsafe_allow_html=True)


    @st.cache_data(ttl=25)
    def load_dashboard_data():
        """ดึงข้อมูลล่าสุดจาก Neon มาแสดงในแดชบอร์ด"""
        try:
            with get_conn() as conn:
                df = pd.read_sql(
                    """
                    SELECT source AS "Source", asset AS "Asset", title AS "Title",
                           sentiment AS "Sentiment", confidence AS "Confidence",
                           action AS "Action", reason AS "Reason", link AS "Link",
                           timestamp AS "Timestamp"
                    FROM market_sentiment
                    ORDER BY timestamp DESC
                    LIMIT 500;
                    """,
                    conn
                )
            return df
        except Exception as e:
            st.error(f"Error loading data from Neon: {e}")
            return pd.DataFrame()


    df = load_dashboard_data()

    # ---------- Header with live status badge ----------
    last_update_text = "รอข้อมูล..."
    if not df.empty and "Timestamp" in df.columns:
        now_utc = pd.Timestamp.now(tz="UTC")
        latest_ts_utc = pd.to_datetime(df["Timestamp"], utc=True).max()
        age_min = round((now_utc - latest_ts_utc).total_seconds() / 60)
        last_update_text = f"ข่าวล่าสุดเมื่อ {age_min} นาทีที่แล้ว"

    st.markdown(f"""
    <div class="terminal-header">
        <div>
            <h1>📈 AI Market Sentiment Terminal</h1>
            <p>News-driven momentum scanner · Neon + Gemini + Groq</p>
        </div>
        <div class="live-badge"><span class="live-dot"></span> LIVE · {last_update_text}</div>
    </div>
    """, unsafe_allow_html=True)

    st.sidebar.header("🕹 Control Panel")
    if st.sidebar.button("🔄 Force Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    st.sidebar.caption(f"Alert threshold: confidence ≥ {ALERT_CONFIDENCE_THRESHOLD}%")

    if df.empty or "Sentiment" not in df.columns:
        st.info("ยังไม่มีข้อมูลเพียงพอสำหรับการวิเคราะห์ กำลังรอ Background Bot สแกนรอบแรก...")
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

    tab_overview, tab_news, tab_about = st.tabs(["📊 Overview", "📰 News Feed", "ℹ️ About"])

    with tab_overview:
        col_gauge, col_pie = st.columns([1, 1])

        with col_gauge:
            st.subheader("🧭 Custom Market Fear & Greed Index")
            fig_gauge = go.Figure(go.Indicator(
                mode="gauge+number",
                value=fear_greed_val,
                title={'text': "Market Sentiment Index"},
                gauge={
                    'axis': {'range': [0, 100]},
                    'bar': {'color': "#4a463f"},
                    'steps': [
                        {'range': [0, 25], 'color': "#d98a80"},
                        {'range': [25, 45], 'color': "#e0b483"},
                        {'range': [45, 55], 'color': "#c9c5bd"},
                        {'range': [55, 75], 'color': "#8fbf9f"},
                        {'range': [75, 100], 'color': "#6fae82"}
                    ],
                    'threshold': {'line': {'color': "black", 'width': 4}, 'thickness': 0.75, 'value': fear_greed_val}
                }
            ))
            fig_gauge.update_layout(
                height=350, margin=dict(l=20, r=20, t=50, b=20),
                paper_bgcolor="rgba(0,0,0,0)", font_color="#4a463f"
            )
            st.plotly_chart(fig_gauge, use_container_width=True)

        with col_pie:
            st.subheader("📊 Sentiment Breakdown")
            fig_pie = px.pie(
                df,
                names="Sentiment",
                color="Sentiment",
                color_discrete_map={"Bullish": "#6fae82", "Bearish": "#c2685c", "Neutral": "#b3aea3"},
                hole=0.45,
            )
            fig_pie.update_layout(
                height=350, margin=dict(l=20, r=20, t=50, b=20),
                paper_bgcolor="rgba(0,0,0,0)", font_color="#4a463f"
            )
            st.plotly_chart(fig_pie, use_container_width=True)

        if "Asset" in df.columns and total_posts > 0:
            st.subheader("🪙 Sentiment by Asset")
            asset_summary = (
                df.groupby("Asset")["Sentiment"]
                .value_counts()
                .unstack(fill_value=0)
                .reindex(columns=["Bullish", "Bearish", "Neutral"], fill_value=0)
            )
            fig_bar = px.bar(
                asset_summary, barmode="stack",
                color_discrete_map={"Bullish": "#6fae82", "Bearish": "#c2685c", "Neutral": "#b3aea3"},
            )
            fig_bar.update_layout(
                height=320, margin=dict(l=20, r=20, t=30, b=20),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font_color="#4a463f",
                legend_title_text=""
            )
            st.plotly_chart(fig_bar, use_container_width=True)

    with tab_news:
        st.subheader("📰 Real-time News & Trade Recommendation")

        high_conf = df[df["Confidence"].astype(float) >= ALERT_CONFIDENCE_THRESHOLD]
        st.caption(f"🔔 {len(high_conf)} รายการผ่านเกณฑ์แจ้งเตือน (confidence ≥ {ALERT_CONFIDENCE_THRESHOLD}%) จากทั้งหมด {total_posts} รายการ")

        display_df = df.sort_values(by="Timestamp", ascending=False).copy() if "Timestamp" in df.columns else df.copy()
        st.dataframe(
            display_df,
            use_container_width=True,
            column_config={
                "Confidence": st.column_config.ProgressColumn(
                    "Confidence", min_value=0, max_value=100, format="%d%%"
                ),
                "Link": st.column_config.LinkColumn("Link", display_text="เปิดข่าว ↗"),
                "Timestamp": st.column_config.DatetimeColumn("Timestamp", format="D MMM, HH:mm"),
            },
            hide_index=True,
        )

    with tab_about:
        st.subheader("ℹ️ หลักการทำงานของระบบ")
        st.markdown(f"""
        1. **คัดกรองข่าว** จาก RSS 5 แหล่ง ด้วย keyword ที่เกี่ยวกับสินทรัพย์เป้าหมาย
        2. **Groq** คัดก่อนว่าเป็น catalyst จริงไหม (ประหยัดโควตา Gemini)
        3. **Gemini** วิเคราะห์ sentiment / confidence / คำแนะนำเชิงทิศทาง
        4. แจ้งเตือนเข้า Telegram เฉพาะรายการที่ **confidence ≥ {ALERT_CONFIDENCE_THRESHOLD}%** พร้อมระยะ lag ของข่าว
        5. **Fear & Greed Index** คำนวณจากสัดส่วน Bullish/Bearish สะสมทั้งหมด

        ⚠️ ระบบนี้มี lag จาก RSS โดยธรรมชาติ (นาทีถึงชั่วโมง) เหมาะสำหรับใช้เป็น**ข้อมูลประกอบการตัดสินใจ**
        ควบคู่กับระบบ technical indicator อื่น ไม่ควรใช้ตัดสินใจเข้า-ออกไม้เพียงลำพัง
        """)

