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
BUCKET_NAME = "images"

@st.cache_resource
def get_supabase_client():
    sb_url = st.secrets.get("SUPABASE_URL")
    sb_key = st.secrets.get("SUPABASE_KEY")
    if not sb_url or not sb_key: return None
    return create_client(sb_url, sb_key)

def handle_sidebar_and_genai():
    st.sidebar.title("道場の設定")
    user_api_key = st.sidebar.text_input("ご自身のGoogle API Keyを入力", type="password")
    api_key = user_api_key if user_api_key else st.secrets.get("GOOGLE_API_KEY")
    if not api_key:
        st.info("左側のサイドバーに API Key を入力してください。")
        st.stop()
    genai.configure(api_key=api_key)

handle_sidebar_and_genai()
supabase: Client = get_supabase_client()

if not supabase:
    st.error("Supabaseの設定が未完了です。")
    st.stop()

# 404を根絶する「執念のモデル選別」
def get_active_model_name():
    try:
        # 今使える全モデルを取得
        available_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        
        # 2.0系から探し、なければ1.5系、最後はなんでもいいから動くものを
        targets = ['gemini-2.0-flash', 'gemini-1.5-flash', 'gemini-1.5-flash-latest']
        
        for target in targets:
            # 名前の前後一致を確認（models/ が付いている場合とない場合の両方に対応）
            for am in available_models:
                if target in am:
                    return am
        return available_models[0]
    except Exception:
        return 'gemini-1.5-flash' # 最終フォールバック

SYSTEM_PROMPT = """あなたは温厚な書道師範「清風」です。
画像全体を[縦1000, 横1000]の方眼として正確に捉え、修正箇所の真上に[y, x]座標で赤ペンを置いてください。
- y: 上端0 〜 下端1000
- x: 左端0 〜 右端1000
必ず筆跡の真ん中を指してください。

必ず以下のJSON形式でのみ回答してください：
{
"grade": "〇級 または 〇段",
"overall_comment": "素晴らしい点と総評",
"corrections": [{"point": [y, x], "label": "修正点", "description": "指導内容"}]
}"""

# ==========================================
# 2. 画像 & ネットワーク
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
        resp = requests.get(url, timeout=15)
        return resp.content
    except: return None

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
        # URL取得をより堅牢に
        res = supabase.storage.from_(BUCKET_NAME).get_public_url(path)
        # 戻り値がオブジェクトか文字列か判定してURLを抽出
        if isinstance(res, str): return res
        if hasattr(res, 'public_url'): return res.public_url
        return res.get('publicURL')
    except Exception as e:
        st.error(f"保存失敗: {e}")
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
# 3. メイン画面
# ==========================================
st.set_page_config(page_title="AI書道師範・清風", layout="wide")
tab1, tab2 = st.tabs(["🖌️ 師範の鑑定", "📈 成長ログ"])

with tab1:
    st.title("🖌️ AI書道師範：清風")
    
    col1, col2 = st.columns(2)
    with col1: model_file = st.file_uploader("お手本", type=["jpg", "png", "jpeg"], key="m")
    with col2:
        practice_file = st.file_uploader("作品", type=["jpg", "png", "jpeg"], key="p")
        written_d = st.date_input("書いた日", datetime.now())

    if practice_file:
        p_img = load_and_fix_image(practice_file)
        st.image(p_img, width=300)
        
        if st.button("清風師範に見ていただく", type="primary"):
            with st.spinner("現在最も優れたモデルを探して、鑑定中です..."):
                try:
                    # 404を回避するために、いま使える名前をリストから取得
                    active_model = get_active_model_name()
                    
                    model = genai.GenerativeModel(
                        model_name=active_model,
                        system_instruction=f"画像サイズ:{p_img.size} \n" + SYSTEM_PROMPT,
                        generation_config={"response_mime_type": "application/json"}
                    )
                    
                    content = ["添削せよ。手本があれば骨格を比較せよ。", p_img]
                    m_img = load_and_fix_image(model_file)
                    if m_img: content.insert(1, m_img)
                    
                    response = model.generate_content(content)
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
                        st.success(f"鑑定完了！（使用筆: {active_model}）")
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
                with c1: st.subheader(f"📅 {h['written_date']} | {h['grade']}")
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
                    if h.get('m_url'): st.image(h['m_url'], caption="お手本", use_container_width=True)
                with col_p:
                    if h.get('p_url'):
                        if show_red:
                            img_data = fetch_image_content(h['p_url'])
                            if img_data:
                                try:
                                    img_h = ImageOps.exif_transpose(Image.open(io.BytesIO(img_data)))
                                    st.image(draw_red_pen(img_h, h.get('corrections', [])), caption="添削結果", use_container_width=True)
                                except Exception:
                                    st.image(h['p_url'], use_container_width=True)
                        else:
                            st.image(h['p_url'], use_container_width=True)
                for c in (h.get('corrections') or []):
                    with st.expander(f"✨ {c.get('label')}"): st.write(c.get('description'))
    else:
        st.write("記録がありません。")
