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
# 1. 空間演出 (天童寺本堂をイメージした明るい和風CSS)
# ==========================================
def apply_temple_style():
    st.markdown("""
    <style>
    /* 全体の背景：明るい畳と自然光をイメージ */
    .stApp {
        background-color: #fcfaf2;
        background-image: linear-gradient(0deg, #f2ead3 1px, transparent 1px);
        background-size: 100% 40px; /* 畳の目を薄く表現 */
    }
    
    /* 五色幕の装飾（画面最上部） */
    .goshiki-banner {
        height: 12px;
        width: 100%;
        background: linear-gradient(to right, 
            #00008b 20%, #ffd700 20% 40%, #ff0000 40% 60%, 
            #ffffff 60% 80%, #800080 80%);
        margin-bottom: 20px;
        border-radius: 2px;
    }

    /* 掲額（タイトル）のデザイン：金色の装飾を意識 */
    .temple-title {
        font-family: "Hiragino Mincho ProN", "Yu Mincho", serif;
        background-color: #fffdf0;
        color: #1a1a1a;
        padding: 20px;
        border: 4px double #d4af37; /* 金色の二重線 */
        border-radius: 8px;
        text-align: center;
        box-shadow: 0 4px 10px rgba(0,0,0,0.1);
        margin-bottom: 30px;
    }

    /* サイドバー：明るい白木の柱をイメージ */
    [data-testid="stSidebar"] {
        background-color: #f7f3e9;
        border-right: 2px solid #e0d5c1;
    }
    [data-testid="stSidebar"] .stMarkdown h3 {
        color: #5d4037;
        border-left: 5px solid #8b4513;
        padding-left: 10px;
    }

    /* 鑑定書：真っさらな和紙の質感 */
    .washi-card {
        background-color: #ffffff;
        border: 1px solid #e0e0e0;
        padding: 30px;
        border-radius: 3px;
        box-shadow: 2px 2px 5px rgba(0,0,0,0.05);
        background-image: url('https://www.transparenttextures.com/patterns/paper-fibers.png');
        margin-top: 20px;
        margin-bottom: 20px;
    }

    /* 師範の言葉：墨の深い黒 */
    .sumi-text {
        font-family: "Hiragino Mincho ProN", "Yu Mincho", serif;
        color: #1a1a1a;
        line-height: 2.0;
        font-size: 1.15rem;
    }

    /* 級・段の強調 */
    .grade-badge {
        color: #b22222;
        font-size: 1.8rem;
        font-weight: bold;
        border-bottom: 2px solid #b22222;
    }
    </style>
    <div class="goshiki-banner"></div>
    """, unsafe_allow_html=True)

# ==========================================
# 2. 初期化 & セキュリティ
# ==========================================
st.set_page_config(page_title="AI書道師範・清風", layout="wide")
apply_temple_style()

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
    st.sidebar.markdown("### 🌿 道場の案内")
    user_api_key = st.sidebar.text_input("ご自身の API Key", type="password")
    api_key = user_api_key if user_api_key else st.secrets.get("GOOGLE_API_KEY")
    if not api_key:
        st.info("サイドバーに API Key を入力してください。")
        st.stop()
    genai.configure(api_key=api_key)

handle_sidebar_and_genai()
supabase: Client = get_supabase_client()

def get_working_model_name():
    try:
        available_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        for version in ["2.5-flash", "2.0-flash", "1.5-flash"]:
            for m in available_models:
                if version in m: return m
        return available_models[0]
    except Exception: return "models/gemini-1.5-flash"

SYSTEM_PROMPT = """あなたは温厚で丁寧な書道師範「清風（せいふう）」です。
画像内の筆跡をミリ単位で分析し、[縦1000, 横1000]の座標系で優しく添削してください。
必ず以下のJSON形式でのみ回答してください：
{
"grade": "〇級 または 〇段",
"overall_comment": "素晴らしい点と、各項目の得点根拠を含めた総評",
"corrections": [{"point": [y, x], "label": "修正箇所", "description": "具体的アドバイス"}]
}"""

# ==========================================
# 3. データ処理
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
        return getattr(res, 'public_url', str(res))
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
            r = max(25, w / 42)
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
st.markdown('<div class="temple-title"><h1>🖌️ AI書道師範「清風」 天童寺道場</h1></div>', unsafe_allow_html=True)

tab1, tab2 = st.tabs(["🙏 師範の鑑定", "📜 成長の歩み"])

with tab1:
    st.markdown('<div class="sumi-text">「よくおいでくださいました。心を静めて、あなたの今日の一筆をお見せください。」</div>', unsafe_allow_html=True)
    
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("#### 📜 手本を置く")
        model_file = st.file_uploader("手本の画像をアップロード", type=["jpg", "png", "jpeg"], key="zen_m")
    with col2:
        st.markdown("#### 🖌️ 作品を置く")
        practice_file = st.file_uploader("作品の画像をアップロード", type=["jpg", "png", "jpeg"], key="zen_p")
        written_d = st.date_input("執筆の日", datetime.now())

    if practice_file:
        p_img_obj = load_and_fix_image(practice_file)
        st.image(p_img_obj, caption="本日の作品", width=400)

        if st.button("清風師範に見ていただく", type="primary"):
            with st.spinner("師範が精神を集中させ、作品と対話しております..."):
                try:
                    active_model = get_working_model_name()
                    model = genai.GenerativeModel(
                        model_name=active_model,
                        system_instruction=f"画像サイズ:{p_img_obj.size} \n" + SYSTEM_PROMPT,
                        generation_config={"response_mime_type": "application/json", "temperature": 0.0}
                    )
                    content_list = []
                    m_img = load_and_fix_image(model_file)
                    if m_img: content_list.extend(["手本:", m_img])
                    content_list.extend(["作品:", p_img_obj])
                    
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
                            st.success("鑑定が整いました。大切に保管いたします。")
                    else:
                        st.error("師範が考え込んでしまったようです。もう一度お願いします。")
                except Exception as e: st.error(f"鑑定失敗: {e}")

        if st.session_state.last_res:
            res = st.session_state.last_res
            st.markdown(f"""
            <div class="washi-card">
                <div class="grade-badge">判定：{res['grade']}</div>
                <div class="sumi-text" style="margin-top:20px;">{res['overall_comment']}</div>
            </div>
            """, unsafe_allow_html=True)
            st.image(draw_red_pen(st.session_state.last_img, res['corrections']), use_container_width=True)
            for c in (res.get('corrections') or []):
                with st.expander(f"✨ {c.get('label')}", expanded=True):
                    st.write(c.get('description'))

# ==========================================
# 5. メインUI (Tab 2: ログ)
# ==========================================
with tab2:
    st.markdown('<div class="sumi-text">「これまでの積み重ねは裏切りません。過去の自分と対話し、さらなる高みを目指しましょう。」</div>', unsafe_allow_html=True)
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
                            try: supabase.storage.from_(BUCKET_NAME).remove([f"{eid}_p.jpg", f"{eid}_m.jpg"])
                            except: pass
                            supabase.table("calligraphy_history").delete().eq("id", eid).execute()
                            st.session_state.history_version = str(uuid.uuid4())
                            st.rerun()

                show_red = st.toggle("アドバイスを重ねる", value=True, key=f"t_{eid}")
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
                        else: st.image(p_url_str, use_container_width=True)
                
                st.markdown(f'<div class="washi-card"><div class="sumi-text">{h["comment"]}</div></div>', unsafe_allow_html=True)
                for c in (h.get('corrections') or []):
                    with st.expander(f"✨ {c.get('label')}"):
                        st.write(c.get('description'))
    else:
        st.write("まだ蔵に作品がありません。")
