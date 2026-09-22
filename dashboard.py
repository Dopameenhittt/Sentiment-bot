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

    # ---------- Custom Theme (comfortable dark look) ----------
    st.markdown("""
    <style>
        .stApp { background-color: #14181f; }
        .main .block-container { padding-top: 1.5rem; max-width: 1400px; }

        .terminal-header {
            display: flex; justify-content: space-between; align-items: center;
            padding: 1.2rem 1.6rem; border-radius: 14px; margin-bottom: 1.2rem;
            background: #1b212b;
            border: 1px solid #2b3340;
            box-shadow: 0 1px 3px rgba(0,0,0,0.2);
        }
        .terminal-header h1 { margin: 0; font-size: 1.55rem; color: #f1f3f6; }
        .terminal-header p { margin: 0.2rem 0 0 0; color: #8b93a3; font-size: 0.85rem; }
        .live-badge {
            display: inline-flex; align-items: center; gap: 6px;
            background: rgba(52,199,120,0.14); color: #4ade80;
            padding: 5px 12px; border-radius: 20px; font-size: 0.8rem; font-weight: 600;
            border: 1px solid rgba(74,222,128,0.35);
        }
        .live-dot {
            width: 8px; height: 8px; border-radius: 50%; background: #4ade80;
            animation: pulse 1.8s infinite;
        }
        @keyframes pulse {
            0% { box-shadow: 0 0 0 0 rgba(74,222,128,0.5); }
            70% { box-shadow: 0 0 0 8px rgba(74,222,128,0); }
            100% { box-shadow: 0 0 0 0 rgba(74,222,128,0); }
        }

        div[data-testid="stMetric"] {
            background: #1b212b; border: 1px solid #2b3340; border-radius: 12px;
            padding: 0.9rem 1rem;
        }
        div[data-testid="stMetricLabel"] { color: #8b93a3; }
        div[data-testid="stMetricValue"] { color: #f1f3f6; }

        .sentiment-badge {
            padding: 3px 10px; border-radius: 20px; font-size: 0.78rem; font-weight: 700;
            display: inline-block;
        }
        .badge-bull { background: rgba(74,222,128,0.14); color: #4ade80; }
        .badge-bear { background: rgba(248,113,113,0.14); color: #f87171; }
        .badge-neutral { background: rgba(148,163,184,0.14); color: #94a3b8; }

        section[data-testid="stSidebar"] { background: #1b212b; border-right: 1px solid #2b3340; }
        h2, h3 { color: #f1f3f6; }
        .stTabs [data-baseweb="tab"] { color: #8b93a3; }
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
                           timestamp AS "Timestamp", lag_minutes AS "Lag (min)"
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


    @st.cache_data(ttl=60)
    def load_impact_data():
        """ดึงข้อมูลราคาของข่าว (market_sentiment) และ baseline (ไม่อิงข่าว) มาเทียบกัน
        เพื่อตอบ 2 คำถาม: (1) ข่าวทำให้ราคาขยับมากกว่าปกติจริงไหม (2) ทิศทางที่ AI ทำนายถูกไหม"""
        try:
            with get_conn() as conn:
                news_df = pd.read_sql(
                    """
                    SELECT asset AS "Asset", sentiment AS "Sentiment",
                           price_at_alert AS "P0", price_15m AS "P15m",
                           price_1h AS "P1h", price_4h AS "P4h", price_24h AS "P24h"
                    FROM market_sentiment
                    WHERE asset IN ('BTC', 'ETH') AND price_at_alert IS NOT NULL;
                    """,
                    conn
                )
                baseline_df = pd.read_sql(
                    """
                    SELECT asset AS "Asset",
                           price_now AS "P0", price_15m AS "P15m",
                           price_1h AS "P1h", price_4h AS "P4h", price_24h AS "P24h"
                    FROM baseline_prices
                    WHERE price_now IS NOT NULL;
                    """,
                    conn
                )
            return news_df, baseline_df
        except Exception as e:
            st.error(f"Error loading impact data from Neon: {e}")
            return pd.DataFrame(), pd.DataFrame()


    def _compute_impact_stats(news_df, baseline_df):
        """คำนวณ: ขนาดการขยับราคาเฉลี่ย (impact) ของข่าว vs baseline, และ directional accuracy
        ต่อ window เวลา (15m/1h/4h/24h) — ตัด row ที่ราคาเท่าเดิมทุกเป๊ะออก (มักเกิดจาก backfill
        ที่มีความละเอียดหยาบ ไม่ใช่ราคาไม่ขยับจริง) เพื่อไม่ให้ค่าความแม่นยำเพี้ยน"""
        windows = [("15m", "P15m"), ("1h", "P1h"), ("4h", "P4h"), ("24h", "P24h")]
        rows = []
        for label, col in windows:
            # --- Market impact: news vs baseline ---
            n = news_df.dropna(subset=["P0", col]).copy()
            n = n[n["P0"] != n[col]]
            news_move = ((n[col] - n["P0"]).abs() / n["P0"] * 100)
            news_avg_move = round(news_move.mean(), 3) if len(news_move) > 0 else None
            news_n = len(news_move)

            b = baseline_df.dropna(subset=["P0", col]).copy()
            b = b[b["P0"] != b[col]]
            base_move = ((b[col] - b["P0"]).abs() / b["P0"] * 100)
            base_avg_move = round(base_move.mean(), 3) if len(base_move) > 0 else None
            base_n = len(base_move)

            # --- Directional accuracy: ทิศทางที่ AI ทำนาย (Bullish/Bearish) ตรงกับราคาจริงไหม ---
            d = n[n["Sentiment"].isin(["Bullish", "Bearish"])].copy()
            if len(d) > 0:
                actual_up = d[col] > d["P0"]
                predicted_up = d["Sentiment"] == "Bullish"
                correct = (actual_up == predicted_up)
                accuracy = round(correct.mean() * 100, 1)
                acc_n = len(d)
            else:
                accuracy = None
                acc_n = 0

            rows.append({
                "Window": label,
                "News Avg Move %": news_avg_move,
                "News N": news_n,
                "Baseline Avg Move %": base_avg_move,
                "Baseline N": base_n,
                "Impact Ratio": round(news_avg_move / base_avg_move, 2) if news_avg_move and base_avg_move else None,
                "Direction Accuracy %": accuracy,
                "Accuracy N": acc_n,
            })
        return pd.DataFrame(rows)


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

    tab_overview, tab_news, tab_impact, tab_about = st.tabs(
        ["📊 Overview", "📰 News Feed", "📐 Market Impact", "ℹ️ About"]
    )

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
                    'bar': {'color': "#f1f3f6"},
                    'steps': [
                        {'range': [0, 25], 'color': "#b91c1c"},
                        {'range': [25, 45], 'color': "#c2703a"},
                        {'range': [45, 55], 'color': "#475569"},
                        {'range': [55, 75], 'color': "#2f855a"},
                        {'range': [75, 100], 'color': "#22c55e"}
                    ],
                    'threshold': {'line': {'color': "black", 'width': 4}, 'thickness': 0.75, 'value': fear_greed_val}
                }
            ))
            fig_gauge.update_layout(
                height=350, margin=dict(l=20, r=20, t=50, b=20),
                paper_bgcolor="rgba(0,0,0,0)", font_color="#e2e8f0"
            )
            st.plotly_chart(fig_gauge, use_container_width=True)

        with col_pie:
            st.subheader("📊 Sentiment Breakdown")
            fig_pie = px.pie(
                df,
                names="Sentiment",
                color="Sentiment",
                color_discrete_map={"Bullish": "#4ade80", "Bearish": "#f87171", "Neutral": "#94a3b8"},
                hole=0.45,
            )
            fig_pie.update_layout(
                height=350, margin=dict(l=20, r=20, t=50, b=20),
                paper_bgcolor="rgba(0,0,0,0)", font_color="#e2e8f0"
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
                color_discrete_map={"Bullish": "#4ade80", "Bearish": "#f87171", "Neutral": "#94a3b8"},
            )
            fig_bar.update_layout(
                height=320, margin=dict(l=20, r=20, t=30, b=20),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font_color="#e2e8f0",
                legend_title_text=""
            )
            st.plotly_chart(fig_bar, use_container_width=True)

    with tab_news:
        st.subheader("📰 Real-time News & Trade Recommendation")

        high_conf = df[df["Confidence"].astype(float) >= ALERT_CONFIDENCE_THRESHOLD]
        lag_caption = ""
        if "Lag (min)" in df.columns and df["Lag (min)"].notna().any():
            avg_lag = round(df["Lag (min)"].dropna().astype(float).mean(), 1)
            lag_caption = f" · ⏱ lag เฉลี่ย {avg_lag} นาที (นับจากข่าวเผยแพร่ถึงตอนวิเคราะห์เสร็จ)"
        st.caption(f"🔔 {len(high_conf)} รายการผ่านเกณฑ์แจ้งเตือน (confidence ≥ {ALERT_CONFIDENCE_THRESHOLD}%) จากทั้งหมด {total_posts} รายการ{lag_caption}")

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

    with tab_impact:
        st.subheader("📐 ข่าวมีผลต่อตลาดจริงไหม? ทิศทางที่ทำนายถูกไหม?")
        st.caption(
            "เทียบขนาดการขยับราคาหลังข่าว (BTC/ETH) กับช่วงที่ไม่มีข่าว (baseline) "
            "เพื่อดูว่าข่าวทำให้ราคาขยับ 'แรงกว่าปกติ' จริงไหม พร้อมเช็คว่า AI ทำนายทิศทางถูกกี่ %"
        )

        news_df, baseline_df = load_impact_data()

        if news_df.empty:
            st.info("ยังไม่มีข้อมูลราคาข่าว BTC/ETH ที่ backfill/track เสร็จพอจะวิเคราะห์")
        elif baseline_df.empty:
            st.info(
                "ยังไม่มีข้อมูล baseline (ราคาที่เก็บแบบไม่อิงข่าว) เพียงพอ — "
                "ระบบเริ่มเก็บ baseline อัตโนมัติทุก ~15 นาทีแล้ว รอสักพักแล้วกลับมาดูใหม่"
            )
        else:
            stats_df = _compute_impact_stats(news_df, baseline_df)

            col_a, col_b = st.columns([1.3, 1])

            with col_a:
                st.markdown("**ขนาดการขยับราคาเฉลี่ย (Market Impact)**")
                fig_impact = go.Figure()
                fig_impact.add_bar(
                    name="ตอนมีข่าว", x=stats_df["Window"], y=stats_df["News Avg Move %"],
                    marker_color="#4ade80"
                )
                fig_impact.add_bar(
                    name="Baseline (ไม่มีข่าว)", x=stats_df["Window"], y=stats_df["Baseline Avg Move %"],
                    marker_color="#94a3b8"
                )
                fig_impact.update_layout(
                    barmode="group", height=330, margin=dict(l=20, r=20, t=20, b=20),
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font_color="#e2e8f0",
                    yaxis_title="Avg |% price move|", legend_title_text=""
                )
                st.plotly_chart(fig_impact, use_container_width=True)
                st.caption(
                    "Impact Ratio > 1 แปลว่าราคาขยับแรงกว่าช่วงไม่มีข่าว (ยิ่งสูงยิ่งแปลว่าข่าวมีผลจริง) "
                    "ค่าใกล้ 1 แปลว่าข่าวแทบไม่ต่างจากความผันผวนปกติของตลาด"
                )

            with col_b:
                st.markdown("**ทิศทางที่ทำนายถูก (Direction Accuracy)**")
                acc_plot_df = stats_df.dropna(subset=["Direction Accuracy %"])
                if not acc_plot_df.empty:
                    fig_acc = go.Figure()
                    fig_acc.add_bar(
                        x=acc_plot_df["Window"], y=acc_plot_df["Direction Accuracy %"],
                        marker_color="#60a5fa",
                        text=acc_plot_df["Direction Accuracy %"].astype(str) + "%",
                        textposition="outside",
                    )
                    fig_acc.add_hline(y=50, line_dash="dash", line_color="#94a3b8",
                                       annotation_text="สุ่มเดา 50%", annotation_font_color="#94a3b8")
                    fig_acc.update_layout(
                        height=330, margin=dict(l=20, r=20, t=20, b=30),
                        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font_color="#e2e8f0",
                        yaxis_title="% ทายทิศทางถูก", yaxis_range=[0, 100]
                    )
                    st.plotly_chart(fig_acc, use_container_width=True)
                else:
                    st.info("ยังไม่มีข้อมูลพอคำนวณ accuracy")

            st.markdown("**ตารางสรุปทั้งหมด**")
            display_stats = stats_df.rename(columns={
                "News Avg Move %": "News Move %",
                "Baseline Avg Move %": "Baseline Move %",
                "Direction Accuracy %": "Accuracy %",
            })
            st.dataframe(display_stats, use_container_width=True, hide_index=True)
            st.caption(
                "⚠️ ตัวอย่าง (N) ยังน้อยในบาง window — ตัวเลขอาจคลาดเคลื่อนได้มาก โดยเฉพาะ window ที่ N < 30 "
                "ควรรอสะสมข้อมูลเพิ่มก่อนเชื่อผลลัพธ์เต็มที่"
            )

    with tab_about:
        st.subheader("ℹ️ หลักการทำงานของระบบ")
        st.markdown(f"""
        1. **คัดกรองข่าว** จาก RSS 5 แหล่ง ด้วย keyword ที่เกี่ยวกับสินทรัพย์เป้าหมาย
        2. **Groq** คัดก่อนว่าเป็น catalyst จริงไหม (ประหยัดโควตา Gemini)
        3. **Gemini** วิเคราะห์ sentiment / confidence / คำแนะนำเชิงทิศทาง
        4. แจ้งเตือนเข้า Telegram เฉพาะรายการที่ **confidence ≥ {ALERT_CONFIDENCE_THRESHOLD}%** พร้อมระยะ lag ของข่าว
        5. **Fear & Greed Index** คำนวณจากสัดส่วน Bullish/Bearish สะสมทั้งหมด
        6. **Market Impact tab**: เก็บราคา baseline (ไม่อิงข่าว) ทุก ~15 นาที เทียบกับราคาหลังข่าว
           เพื่อวัดว่าข่าวทำให้ราคาขยับแรงกว่าปกติจริงไหม (impact) และ AI ทายทิศทางถูกกี่ % (accuracy)

        ⚠️ ระบบนี้มี lag จาก RSS โดยธรรมชาติ (นาทีถึงชั่วโมง) เหมาะสำหรับใช้เป็น**ข้อมูลประกอบการตัดสินใจ**
        ควบคู่กับระบบ technical indicator อื่น ไม่ควรใช้ตัดสินใจเข้า-ออกไม้เพียงลำพัง
        """)

