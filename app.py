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

# ==========================================
# 1. セキュリティ & 外部連携設定
# ==========================================
# Supabaseのバケツ名は、画面で確認した小文字の "images" を使用します
BUCKET_NAME = "images"

@st.cache_resource
def get_supabase_client():
    sb_url = st.secrets.get("SUPABASE_URL")
    sb_key = st.secrets.get("SUPABASE_KEY")
    if not sb_url or not sb_key: return None
    return create_client(sb_url, sb_key)

def handle_sidebar_configs():
    st.sidebar.title("道場の設定")
    user_api_key = st.sidebar.text_input("ご自身のGoogle API Keyを入力してください", type="password")
    api_key = user_api_key if user_api_key else st.secrets.get("GOOGLE_API_KEY")
    if not api_key:
        st.info("左側のサイドバーに API Key を入力してください。")
        st.stop()
    genai.configure(api_key=api_key)

handle_sidebar_configs()
supabase: Client = get_supabase_client()

if not supabase:
    st.error("Supabaseの設定が未完了です。")
    st.stop()

# 404を回避し、将来の2.5-flashも見据えた動的選別
def get_latest_working_model():
    """現在利用可能な最新モデルを自動取得"""
    try:
        available_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        # 優先順位: 2.5(将来) -> 2.0(最新) -> 1.5(安定)
        for version in ["2.5-flash", "2.0-flash", "1.5-flash"]:
            for m in available_models:
                if version in m: return m
        return available_models[0]
    except Exception:
        return "models/gemini-1.5-flash"

SYSTEM_PROMPT = """あなたは温厚で丁寧な書道師範「清風（せいふう）」です。
画像全体のサイズに基づき、[縦1000, 横1000]の座標系で筆跡を分析してください。
【任務】
1. 具体的鑑定: 結体や筆致を分析してください。
2. お手本比較: お手本がある場合、骨格の差を優しく教えてください。
3. 判定: 日本の書道教室に基づき「〇級」または「〇段」を必ず決定してください。

必ず以下のJSON形式でのみ回答：
{
"grade": "〇級 または 〇段",
"overall_comment": "素晴らしい点と、前向きな総評",
"corrections": [{"point": [y, x], "label": "もっと良くなるポイント", "description": "優しく具体的なアドバイス"}]
}"""

# ==========================================
# 2. 画像処理 & 通信
# ==========================================

@st.cache_data(ttl=300, show_spinner=False)
def fetch_history(_client: Client):
    try:
        response = _client.table("calligraphy_history").select("*").order("written_date", desc=True).execute()
        return response.data
    except: return []

@st.cache_data(show_spinner=False)
def fetch_image_content(url):
    try:
        # urlが辞書やオブジェクトとして渡された場合の対策
        clean_url = str(url)
        resp = requests.get(clean_url, timeout=15)
        resp.raise_for_status()
        return resp.content
    except Exception: return None

def load_and_fix_image(uploaded_file):
    if uploaded_file is None: return None
    img = Image.open(uploaded_file)
    img = ImageOps.exif_transpose(img)
    img.thumbnail((1200, 1200))
    return img

def upload_image_to_supabase(img, filename):
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format='PNG')
    path = f"{filename}.png"
    try:
        supabase.storage.from_(BUCKET_NAME).upload(
            path, img_byte_arr.getvalue(), {"content-type": "image/png", "x-upsert": "true"}
        )
        # --- URL取得の徹底修正 ---
        res = supabase.storage.from_(BUCKET_NAME).get_public_url(path)
        # resが文字列（URLそのまま）か、オブジェクト（.public_urlが必要）か判定
        if isinstance(res, str):
            return res
        if hasattr(res, 'public_url'):
            return res.public_url
        return str(res) # 最終手段
    except Exception as e:
        st.error(f"蔵への保存に失敗しました: {e}")
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
        except: continue
    if not font: font = ImageFont.load_default()

    for i, corr in enumerate(corrections or []):
        try:
            p = corr.get("point")
            if not isinstance(p, (list, tuple)) or len(p) < 2: continue
            py, px = (float(p[0]) / 1000) * h, (float(p[1]) / 1000) * w
            r = max(25, w / 45)
            draw.ellipse([px-r, py-r, px+r, py+r], outline="red", width=max(4, int(w/150)))
            label = corr.get("label", f"点{i+1}")
            txt_bbox = draw.textbbox((px + r + 5, py - r), label, font=font)
            draw.rectangle(txt_bbox, fill="white", outline="red")
            draw.text((px + r + 5, py - r), label, fill="red", font=font)
        except: continue
    return canvas

# ==========================================
# 3. メインUI
# ==========================================
st.set_page_config(page_title="AI書道師範・清風", layout="wide")
tab1, tab2 = st.tabs(["🖌️ 師範の鑑定", "📈 成長ログ"])

with tab1:
    st.title("🖌️ AI書道師範：清風")
    
    col1, col2 = st.columns(2)
    with col1: model_file = st.file_uploader("お手本（理想）", type=["jpg", "png", "jpeg"], key="mu")
    with col2:
        practice_file = st.file_uploader("あなたの作品（必須）", type=["jpg", "png", "jpeg"], key="pu")
        written_d = st.date_input("書いた日", datetime.now())

    if practice_file:
        p_img = load_and_fix_image(practice_file)
        st.image(p_img, width=300)
        
        if st.button("清風師範に見ていただく", type="primary"):
            with st.spinner("せいふう師範が鑑定中です..."):
                try:
                    active_model = get_latest_working_model()
                    model = genai.GenerativeModel(
                        model_name=active_model,
                        system_instruction=f"画像サイズ:{p_img.size} \n" + SYSTEM_PROMPT,
                        generation_config={"response_mime_type": "application/json"}
                    )
                    
                    content_list = []
                    m_img = load_and_fix_image(model_file)
                    if m_img: content_list.extend(["お手本:", m_img])
                    content_list.extend(["作品:", p_img])
                    
                    response = model.generate_content(content_list)
                    data = json.loads(response.text)

                    eid = str(uuid.uuid4())
                    p_url = upload_image_to_supabase(p_img, f"{eid}_p")
                    m_url = upload_image_to_supabase(m_img, f"{eid}_m") if m_img else None

                    if p_url:
                        supabase.table("calligraphy_history").insert({
                            "id": eid, "written_date": str(written_d),
                            "grade": data.get("grade",""), "comment": data.get("overall_comment",""),
                            "corrections": data.get("corrections",[]), "p_url": p_url, "m_url": m_url
                        }).execute()
                        st.cache_data.clear()
                        st.session_state.last_res, st.session_state.last_img = data, p_img
                        st.success(f"鑑定完了！蔵に納めました。（使用筆: {active_model}）")
                    else: st.error("蔵への保存に失敗しました。")
                except Exception as e:
                    st.error(f"鑑定失敗: {e}")

        if 'last_res' in st.session_state:
            res = st.session_state.last_res
            st.success(f"## 判定: {res['grade']}")
            st.info(res['overall_comment'])
            st.image(draw_red_pen(st.session_state.last_img, res['corrections']), use_container_width=True)

with tab2:
    st.title("📈 成長ログ")
    history = fetch_history(supabase)
    if history:
        for h in history:
            eid = h['id']
            with st.container():
                st.divider()
                c1, c2, c3 = st.columns([2, 1, 1])
                with c1: st.subheader(f"📅 {h['written_date']} | 判定: {h['grade']}")
                with c3:
                    if st.checkbox("削除確定", key=f"c_{eid}"):
                        if st.button("🗑️ 削除", key=f"d_{eid}"):
                            try: supabase.storage.from_(BUCKET_NAME).remove([f"{eid}_p.png", f"{eid}_m.png"])
                            except: pass
                            supabase.table("calligraphy_history").delete().eq("id", eid).execute()
                            st.cache_data.clear(); st.rerun()

                show_red = st.toggle("アドバイスを表示", value=True, key=f"t_{eid}")
                col_m, col_p = st.columns(2)
                with col_m:
                    if h.get('m_url'):
                        # URLを強制的に文字列として渡し、エラー時は代替表示
                        st.image(str(h['m_url']), caption="お手本", use_container_width=True)
                with col_p:
                    if h.get('p_url'):
                        p_url_str = str(h['p_url'])
                        if show_red:
                            img_data = fetch_image_content(p_url_str)
                            if img_data:
                                try:
                                    img_h = ImageOps.exif_transpose(Image.open(io.BytesIO(img_data)))
                                    st.image(draw_red_pen(img_h, h.get('corrections', [])), caption="添削結果", use_container_width=True)
                                except Exception: st.image(p_url_str, use_container_width=True)
                            else:
                                st.image(p_url_str, caption="作品(直接表示)", use_container_width=True)
                        else:
                            st.image(p_url_str, caption="あなたの作品", use_container_width=True)
                st.info(h['comment'])
                for c in (h.get('corrections') or []):
                    with st.expander(f"✨ {c.get('label')}"): st.write(c.get('description'))
    else:
        st.write("記録がありません。")
