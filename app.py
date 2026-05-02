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
# 1. アプリ初期化
# ==========================================
st.set_page_config(page_title="AI書道師範・清風", layout="wide")

if 'history_version' not in st.session_state:
    st.session_state.history_version = str(uuid.uuid4())
if 'last_res' not in st.session_state:
    st.session_state.last_res = None
if 'last_img' not in st.session_state:
    st.session_state.last_img = None

BUCKET_NAME = "images"

# ==========================================
# 2. セキュリティ & 外部連携初期化
# ==========================================
@st.cache_resource
def get_supabase_client():
    sb_url = st.secrets.get("SUPABASE_URL")
    sb_key = st.secrets.get("SUPABASE_KEY")
    if not sb_url or not sb_key: return None
    return create_client(sb_url, sb_key)

def handle_sidebar_and_genai():
    st.sidebar.title("道場の設定")
    user_api_key = st.sidebar.text_input("ご自身のGoogle API Keyを入力してください", type="password")
    api_key = user_api_key if user_api_key else st.secrets.get("GOOGLE_API_KEY")
    if not api_key:
        st.info("左側のサイドバーに API Key を入力してください。")
        st.stop()
    genai.configure(api_key=api_key)

handle_sidebar_and_genai()
supabase: Client = get_supabase_client()

if not supabase:
    st.error("Supabaseの設定が未完了です。secrets.tomlを確認してください。")
    st.stop()

# 【究極の対策】404エラーを物理的に回避する動的モデル選別
def get_working_model_name():
    """いまこの瞬間に、画像解析ができる最新モデルをGoogleから直接聞き出す"""
    try:
        # 1. あなたのAPIキーで今使えるモデルの名簿をGoogleから取得
        models = genai.list_models()
        available_names = [m.name for m in models if 'generateContent' in m.supported_generation_methods]
        
        # 2. 優先キーワード（新しい順）で検索
        # Google側の「名前のブレ」に左右されないよう、部分一致で探します
        priorities = ["2.0-flash", "1.5-pro", "1.5-flash"]
        
        for p in priorities:
            for am in available_names:
                if p in am:
                    return am
        
        # 3. 優先順位に合致するものがなくても、画像解析(generateContent)ができる先頭のモデルを返す
        return available_names[0]
    except Exception as e:
        # 万が一リストすら取得できない場合のみフォールバック
        return "models/gemini-1.5-flash"

SYSTEM_PROMPT = """あなたは温厚で丁寧な書道師範「清風（せいふう）」です。
画像内の筆跡をミリ単位で分析し、優しく添削してください。
【座標ルール】
- 座標[y, x]は、指摘したい筆跡の開始点や先端をピンポイントで指してください。
- 複数の指摘箇所が全く同じ位置に重ならないよう、数ユニットずらして指定してください。
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
        # URLを文字列として確実に取得
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
tab1, tab2 = st.tabs(["🖌️ 師範の鑑定", "📈 成長ログ"])

with tab1:
    st.title("🖌️ AI書道師範：清風")
    
    col1, col2 = st.columns(2)
    with col1: model_file = st.file_uploader("お手本", type=["jpg", "png", "jpeg"], key="mu")
    with col2:
        practice_file = st.file_uploader("作品", type=["jpg", "png", "jpeg"], key="pu")
        written_d = st.date_input("書いた日", datetime.now())

    if practice_file:
        p_img_obj = load_and_fix_image(practice_file)
        st.image(p_img_obj, width=300)
        
        if st.button("清風師範に見ていただく", type="primary"):
            with st.spinner("道場でいま一番輝いている筆（モデル）を選んでいます..."):
                try:
                    # 【重要】404を回避するために、Googleの名簿からその場で最新モデルを特定
                    active_model = get_working_model_name()
                    
                    model = genai.GenerativeModel(
                        model_name=active_model,
                        system_instruction=f"画像サイズ:{p_img_obj.size} \n" + SYSTEM_PROMPT,
                        generation_config={"response_mime_type": "application/json", "temperature": 0.0}
                    )
                    
                    content_list = []
                    m_img_obj = load_and_fix_image(model_file)
                    if m_img_obj: content_list.extend(["お手本画像:", m_img_obj])
                    content_list.extend(["生徒の作品画像:", p_img_obj])
                    
                    response = model.generate_content(content_list)
                    json_match = re.search(r'\{.*\}', response.text, re.S)
                    
                    if not json_match:
                        st.error("師範の回答を読み取れませんでした。もう一度お願いします。")
                    else:
                        data = json.loads(json_match.group())
                        eid = str(uuid.uuid4())
                        p_url = upload_image_to_supabase(p_img_obj, f"{eid}_p")
                        m_url = upload_image_to_supabase(m_img_obj, f"{eid}_m") if m_img_obj else None

                        if p_url:
                            supabase.table("calligraphy_history").insert({
                                "id": eid, "written_date": str(written_d),
                                "grade": data.get("grade",""), "comment": data.get("overall_comment",""),
                                "corrections": data.get("corrections",[]), "p_url": str(p_url), "m_url": m_url
                            }).execute()
                            st.session_state.history_version = str(uuid.uuid4())
                            st.session_state.last_res, st.session_state.last_img = data, p_img_obj
                            st.success(f"鑑定完了！蔵に納めました。（使用筆: {active_model}）")
                        else: st.error("蔵への保存に失敗しました。")
                except Exception as e: st.error(f"鑑定失敗: {e}")

        if st.session_state.last_res:
            res = st.session_state.last_res
            st.success(f"## 判定: {res['grade']}")
            st.info(res['overall_comment'])
            st.image(draw_red_pen(st.session_state.last_img, res['corrections']), use_container_width=True)
            for c in (res.get('corrections') or []):
                with st.expander(f"✨ {c.get('label')}", expanded=True):
                    st.write(c.get('description'))

# ==========================================
# 5. メインUI (Tab 2: 成長ログ)
# ==========================================
with tab2:
    st.title("📈 成長ログ")
    history = fetch_history(supabase, st.session_state.history_version)
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
                            old_new_paths = [f"{eid}_p.jpg", f"{eid}_m.jpg", f"{eid}_p.png", f"{eid}_m.png"]
                            try: supabase.storage.from_(BUCKET_NAME).remove(old_new_paths)
                            except: pass
                            supabase.table("calligraphy_history").delete().eq("id", eid).execute()
                            st.session_state.history_version = str(uuid.uuid4())
                            st.rerun()

                show_red = st.toggle("アドバイスを表示", value=True, key=f"t_{eid}")
                col_m, col_p = st.columns(2)
                with col_m:
                    if h.get('m_url'): st.image(str(h['m_url']), caption="お手本", use_container_width=True)
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
                            st.image(p_url_str, use_container_width=True)
                st.info(h['comment'])
                for c in (h.get('corrections') or []):
                    with st.expander(f"✨ {c.get('label')}"):
                        st.write(c.get('description'))
    else:
        st.write("まだ記録がありません。")
