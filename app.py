import streamlit as st
import google.generativeai as genai
from google.api_core import exceptions as google_exceptions
from PIL import Image, ImageDraw, ImageFont, ImageOps
import json
import os
from datetime import datetime
import uuid
import io
from supabase import create_client, Client
import requests
from pathlib import Path
import re

# ==========================================
# 1. 空間演出 (Custom CSS)
# ==========================================
def apply_zen_style():
    st.markdown("""
    <style>
    /* 全体の背景色：静かな生成り色 */
    .stApp {
        background-color: #f8f1e7;
        background-image: radial-gradient(#e5d8c1 1px, transparent 1px);
        background-size: 20px 20px;
    }
    
    /* タイトル：毛筆を意識したデザイン */
    h1 {
        font-family: "Hiragino Mincho ProN", "Yu Mincho", serif;
        color: #2c2c2c;
        border-bottom: 2px solid #8b4513;
        padding-bottom: 10px;
        text-align: center;
    }

    /* サイドバー：使い込まれた木の机 */
    [data-testid="stSidebar"] {
        background-color: #3e2723;
        color: #ffffff;
    }
    [data-testid="stSidebar"] h1, [data-testid="stSidebar"] p {
        color: #d7ccc8;
    }

    /* 鑑定結果の枠：和紙の質感 */
    .zen-box {
        background-color: #ffffff;
        border: 1px solid #dcdcdc;
        padding: 25px;
        border-radius: 5px;
        box-shadow: 5px 5px 15px rgba(0,0,0,0.05);
        border-left: 10px solid #8b4513;
        margin-bottom: 20px;
    }

    /* 師範の言葉：墨の色 */
    .master-text {
        font-family: "Hiragino Mincho ProN", "Yu Mincho", serif;
        color: #1a1a1a;
        line-height: 1.8;
        font-size: 1.1rem;
    }

    /* タブの見た目 */
    .stTabs [data-baseweb="tab-list"] {
        gap: 20px;
        background-color: rgba(255,255,255,0.5);
        border-radius: 10px;
        padding: 5px;
    }
    </style>
    """, unsafe_allow_html=True)

# ==========================================
# 2. 初期化 & セキュリティ
# ==========================================
st.set_page_config(page_title="AI書道師範・清風", layout="wide")
apply_zen_style()

if 'history_version' not in st.session_state:
    st.session_state.history_version = str(uuid.uuid4())
if 'last_res' not in st.session_state:
    st.session_state.last_res = None
if 'last_img' not in st.session_state:
    st.session_state.last_img = None

BUCKET_NAME = "images"

@st.cache_resource
def get_supabase_client():
    sb_url = st.secrets.get("SUPABASE_URL")
    sb_key = st.secrets.get("SUPABASE_KEY")
    if not sb_url or not sb_key: return None
    return create_client(sb_url, sb_key)

def handle_sidebar_and_genai():
    st.sidebar.markdown("### ⛩️ 道場の設え")
    user_api_key = st.sidebar.text_input("秘伝の鍵 (Google API Key)", type="password")
    api_key = user_api_key if user_api_key else st.secrets.get("GOOGLE_API_KEY")
    if not api_key:
        st.info("左側のサイドバーに API Key を入力し、道場へお入りください。")
        st.stop()
    genai.configure(api_key=api_key)

handle_sidebar_and_genai()
supabase: Client = get_supabase_client()

if not supabase:
    st.error("蔵(Supabase)の設定が整っておりません。")
    st.stop()

def get_working_model_name():
    try:
        available_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        for version in ["2.5-flash", "2.0-flash", "1.5-flash"]:
            for m in available_models:
                if version in m: return m
        return available_models[0]
    except Exception: return "models/gemini-1.5-flash"

SYSTEM_PROMPT = """あなたは温厚で丁寧な書道師範「清風（せいふう）」です。
[縦1000, 横1000]の座標系で筆跡を詳しく分析し、優しく添削してください。
必ず以下のJSON形式でのみ回答してください：
{
"grade": "〇級 または 〇段",
"overall_comment": "素晴らしい点と、各項目の得点根拠を含めた総評",
"corrections": [{"point": [y, x], "label": "修正箇所", "description": "具体的アドバイス"}]
}"""

# ==========================================
# 3. データフェッチ & 画像処理
# ==========================================

@st.cache_data(ttl=600, show_spinner=False)
def fetch_history(_client: Client, update_key: str):
    try:
        response = _client.table("calligraphy_history").select("*").order("written_date", desc=True).execute()
        return response.data
    except Exception: return []

@st.cache_data(show_spinner=False)
def fetch_image_content(url):
    try:
        resp = requests.get(str(url), timeout=15)
        resp.raise_for_status()
        return resp.content
    except Exception: return None

def load_and_fix_image(uploaded_file):
    if uploaded_file is None: return None
    img = Image.open(uploaded_file)
    img = ImageOps.exif_transpose(img) 
    img.thumbnail((1200, 1200))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    return img

def upload_image_to_supabase(img, filename):
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format='JPEG', quality=90)
    path = f"{filename}.jpg"
    try:
        supabase.storage.from_(BUCKET_NAME).upload(
            path, img_byte_arr.getvalue(), {"content-type": "image/jpeg", "x-upsert": "true"}
        )
        res = supabase.storage.from_(BUCKET_NAME).get_public_url(path)
        if isinstance(res, str): return res
        if hasattr(res, 'public_url'): return str(res.public_url)
        return str(res)
    except Exception as e:
        st.error(f"蔵への保存失敗: {e}")
        return None

def draw_red_pen(base_image, corrections):
    if not corrections: return base_image
    canvas = base_image.copy()
    draw = ImageDraw.Draw(canvas)
    w, h = canvas.size
    font = None
    font_size = max(26, int(w / 35))
    current_dir = Path(__file__).parent
    font_candidates = [current_dir / "ipaexm.ttf", current_dir / "NotoSansJP-Regular.ttf", Path("msmincho.ttc")]
    for f_path in font_candidates:
        try:
            font = ImageFont.truetype(str(f_path), font_size)
            break
        except Exception: continue
    if not font: font = ImageFont.load_default()

    drawn_positions = []
    for i, corr in enumerate(corrections or []):
        try:
            p = corr.get("point")
            if not isinstance(p, (list, tuple)) or len(p) < 2: continue
            py, px = (float(p[0]) / 1000) * h, (float(p[1]) / 1000) * w
            
            offset_y = 0
            for prev_y, prev_x in drawn_positions:
                if abs(py + offset_y - prev_y) < font_size and abs(px - prev_x) < 200:
                    offset_y += font_size + 10
            
            drawn_positions.append((py + offset_y, px))
            r = max(22, w / 42)
            draw.ellipse([px-r, py-r, px+r, py+r], outline="red", width=max(4, int(w/150)))
            label = corr.get("label", f"点{i+1}")
            txt_pos = (px + r + 5, py - r + offset_y)
            txt_bbox = draw.textbbox(txt_pos, label, font=font)
            draw.rectangle([txt_bbox[0]-5, txt_bbox[1]-2, txt_bbox[2]+5, txt_bbox[3]+2], fill="white", outline="red", width=1)
            draw.text(txt_pos, label, fill="red", font=font)
        except Exception: continue
    return canvas

# ==========================================
# 4. メインUI (Tab 1: 鑑定)
# ==========================================
st.write("### 🕯️ 静寂の中に、墨の香りを。")
tab1, tab2 = st.tabs(["🖌️ 師範の鑑定", "📈 成長の歩み"])

with tab1:
    st.markdown('<div class="master-text">「さあ、落ち着いて一筆。あなたの心を表す作品をお見せください。」</div>', unsafe_allow_html=True)
    
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("#### 📜 お手本を置く")
        model_file = st.file_uploader("手本の画像をアップロード", type=["jpg", "png", "jpeg"], key="zen_m")
    with col2:
        st.markdown("#### 🖌️ 清書を置く")
        practice_file = st.file_uploader("あなたの作品をアップロード", type=["jpg", "png", "jpeg"], key="zen_p")
        written_d = st.date_input("執筆の日", datetime.now())

    if practice_file:
        p_img_obj = load_and_fix_image(practice_file)
        st.markdown("---")
        st.image(p_img_obj, caption="本日の作品", width=400)

        if st.button("清風師範に見ていただく", type="primary"):
            with st.spinner("心を落ち着かせ、作品と対話しております..."):
                try:
                    active_model = get_working_model_name()
                    model = genai.GenerativeModel(
                        model_name=active_model,
                        system_instruction=f"画像サイズ:{p_img_obj.size} \n" + SYSTEM_PROMPT,
                        generation_config={"response_mime_type": "application/json", "temperature": 0.0}
                    )
                    content_list = []
                    m_img = load_and_fix_image(model_file)
                    if m_img: content_list.extend(["お手本:", m_img])
                    content_list.extend(["生徒の作品:", p_img_obj])
                    
                    response = model.generate_content(content_list)
                    json_match = re.search(r'\{.*\}', response.text, re.S)
                    
                    if json_match:
                        data = json.loads(json_match.group())
                        eid = str(uuid.uuid4())
                        p_url = upload_image_to_supabase(p_img_obj, f"{eid}_p")
                        m_url = upload_image_to_supabase(m_img, f"{eid}_m") if m_img else None

                        if p_url:
                            supabase.table("calligraphy_history").insert({
                                "id": eid, "written_date": str(written_d),
                                "grade": data.get("grade",""), "comment": data.get("overall_comment",""),
                                "corrections": data.get("corrections",[]), "p_url": str(p_url), "m_url": str(m_url) if m_url else None
                            }).execute()
                            st.session_state.history_version = str(uuid.uuid4())
                            st.session_state.last_res, st.session_state.last_img = data, p_img_obj
                            st.success("鑑定が整いました。蔵に大切に納めます。")
                    else:
                        st.error("師範が考え込んでしまったようです。もう一度お願いできますか？")
                except Exception as e: st.error(f"鑑定失敗: {e}")

        if st.session_state.last_res:
            res = st.session_state.last_res
            st.markdown(f'<div class="zen-box"><h3>判定：{res["grade"]}</h3><div class="master-text">{res["overall_comment"]}</div></div>', unsafe_allow_html=True)
            st.image(draw_red_pen(st.session_state.last_img, res['corrections']), use_container_width=True)
            for c in (res.get('corrections') or []):
                with st.expander(f"✨ {c.get('label')}", expanded=True):
                    st.write(c.get('description'))

# ==========================================
# 5. メインUI (Tab 2: ログ)
# ==========================================
with tab2:
    st.markdown('<div class="master-text">「これまでの歩みを振り返りましょう。一筆ごとの積み重ねが、あなたを形作ります。」</div>', unsafe_allow_html=True)
    history = fetch_history(supabase, st.session_state.history_version)
    if history:
        for h in history:
            eid = h['id']
            with st.container():
                st.divider()
                c1, c2, c3 = st.columns([2, 1, 1])
                with c1: st.subheader(f"📅 {h['written_date']} | {h['grade']}")
                with c3:
                    if st.checkbox("削除の覚悟", key=f"c_{eid}"):
                        if st.button("🗑️ 蔵から出す", key=f"d_{eid}"):
                            try: supabase.storage.from_(BUCKET_NAME).remove([f"{eid}_p.jpg", f"{eid}_m.jpg", f"{eid}_p.png", f"{eid}_m.png"])
                            except: pass
                            supabase.table("calligraphy_history").delete().eq("id", eid).execute()
                            st.session_state.history_version = str(uuid.uuid4())
                            st.rerun()

                show_red = st.toggle("師範のアドバイスを重ねる", value=True, key=f"t_{eid}")
                col_m, col_p = st.columns(2)
                with col_m:
                    if h.get('m_url'): st.image(str(h['m_url']), caption="目指した手本", use_container_width=True)
                with col_p:
                    if h.get('p_url'):
                        p_url_str = str(h['p_url'])
                        if show_red:
                            img_data = fetch_image_content(p_url_str)
                            if img_data:
                                try:
                                    img_h = ImageOps.exif_transpose(Image.open(io.BytesIO(img_data)))
                                    st.image(draw_red_pen(img_h, h.get('corrections', [])), caption="添削の跡", use_container_width=True)
                                except Exception: st.image(p_url_str, use_container_width=True)
                        else:
                            st.image(p_url_str, caption="あなたの筆跡", use_container_width=True)
                
                st.markdown(f'<div class="zen-box"><div class="master-text">{h["comment"]}</div></div>', unsafe_allow_html=True)
                for c in (h.get('corrections') or []):
                    with st.expander(f"✨ {c.get('label')}"):
                        st.write(c.get('description'))
    else:
        st.write("まだ蔵に作品がありません。")
