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
# 【修正】バケツ名を小文字にして試してください。もしダメなら大文字に戻します。
# ほとんどのSupabase環境では小文字の "images" が正解です。
BUCKET_NAME = "images" 

@st.cache_resource
def get_supabase_client():
    sb_url = st.secrets.get("SUPABASE_URL")
    sb_key = st.secrets.get("SUPABASE_KEY")
    if not sb_url or not sb_key: return None
    return create_client(sb_url, sb_key)

def handle_sidebar_configs():
    st.sidebar.title("道場の設定")
    user_api_key = st.sidebar.text_input("ご自身のGoogle API Keyを入力", type="password")
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

# ==========================================
# 2. 画像処理エンジン
# ==========================================

def load_and_fix_image(uploaded_file):
    if uploaded_file is None: return None
    img = Image.open(uploaded_file)
    img = ImageOps.exif_transpose(img)
    img.thumbnail((1024, 1024))
    return img

def upload_image_to_supabase(img, filename):
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format='PNG')
    path = f"{filename}.png"
    try:
        # アップロード実行
        supabase.storage.from_(BUCKET_NAME).upload(
            path, img_byte_arr.getvalue(), {"content-type": "image/png", "x-upsert": "true"}
        )
        # URLを「手動」で構築して確実性を高める（ここがポイント）
        # get_public_urlが不安定な場合があるため
        sb_url = st.secrets.get("SUPABASE_URL").replace(".supabase.co", ".supabase.co/storage/v1/object/public")
        return f"{sb_url}/{BUCKET_NAME}/{path}"
    except Exception as e:
        st.error(f"蔵への保存に失敗しました（バケツ名 {BUCKET_NAME}）: {e}")
        return None

def draw_red_pen(base_image, corrections):
    if not corrections: return base_image
    canvas = base_image.copy()
    draw = ImageDraw.Draw(canvas)
    w, h = canvas.size
    
    font = None
    font_size = max(26, int(w / 32))
    current_dir = Path(__file__).parent
    font_candidates = [current_dir / "ipaexm.ttf", current_dir / "NotoSansJP-Regular.ttf", Path("msmincho.ttc")]
    
    for f_path in font_candidates:
        try:
            font = ImageFont.truetype(str(f_path), font_size)
            break
        except Exception: continue
    if not font: font = ImageFont.load_default()

    for i, corr in enumerate(corrections or []):
        try:
            point = corr.get("point")
            if not isinstance(point, (list, tuple)) or len(point) < 2: continue
            py, px = (float(point[0]) / 1000) * h, (float(point[1]) / 1000) * w
            r = max(22, w / 42)
            draw.ellipse([px-r, py-r, px+r, py+r], outline="red", width=max(4, int(w/150)))
            label = corr.get("label", f"点{i+1}")
            txt_bbox = draw.textbbox((px + r + 5, py - r), label, font=font)
            draw.rectangle(txt_bbox, fill="white", outline="red")
            draw.text((px + r + 5, py - r), label, fill="red", font=font)
        except: continue
    return canvas

# ==========================================
# 3. メインUI (Tab 1)
# ==========================================
st.set_page_config(page_title="AI書道師範・清風", layout="wide")
tab1, tab2 = st.tabs(["🖌️ 師範の鑑定", "📈 成長ログ"])

with tab1:
    st.title("🖌️ AI書道師範：清風")
    col1, col2 = st.columns(2)
    with col1: model_file = st.file_uploader("お手本", type=["jpg", "png", "jpeg"], key="m_up")
    with col2:
        practice_file = st.file_uploader("作品", type=["jpg", "png", "jpeg"], key="p_up")
        written_d = st.date_input("書いた日", datetime.now())

    if practice_file:
        p_img_obj = load_and_fix_image(practice_file)
        w_px, h_px = p_img_obj.size
        st.image(p_img_obj, width=300)

        if st.button("清風師範に見ていただく", type="primary"):
            with st.spinner("鑑定中..."):
                try:
                    # 指導内容をGeminiに依頼
                    model = genai.GenerativeModel(
                        model_name='gemini-2.0-flash', # 最新の2.0-flashに更新
                        system_instruction=f"画像サイズ:{w_px}x{h_px} 座標[y,x] 厳格に添削しJSONで回答せよ。判定は必ず〇級/〇段とせよ。",
                        generation_config={"response_mime_type": "application/json"}
                    )
                    content_list = []
                    m_img_obj = load_and_fix_image(model_file)
                    if m_img_obj: content_list.extend(["手本:", m_img_obj])
                    content_list.extend(["作品:", p_img_obj])
                    
                    response = model.generate_content(content_list)
                    data = json.loads(response.text)

                    # クラウド保存
                    eid = str(uuid.uuid4())
                    p_url = upload_image_to_supabase(p_img_obj, f"{eid}_p")
                    m_url = upload_image_to_supabase(m_img_obj, f"{eid}_m") if m_img_obj else None

                    if p_url:
                        supabase.table("calligraphy_history").insert({
                            "id": eid, "written_date": str(written_d),
                            "grade": data.get("grade", "応援しています"), 
                            "comment": data.get("overall_comment", ""),
                            "corrections": data.get("corrections", []),
                            "p_url": p_url, "m_url": m_url
                        }).execute()
                        st.cache_data.clear()
                        st.session_state.last_res, st.session_state.last_img = data, p_img_obj
                        st.success("鑑定完了！蔵に大切に保管しました。")
                except Exception as e:
                    st.error(f"鑑定失敗: {e}")

        if 'last_res' in st.session_state:
            res = st.session_state.last_res
            st.success(f"## 判定: {res['grade']}")
            st.info(res['overall_comment'])
            st.image(draw_red_pen(st.session_state.last_img, res['corrections']), use_container_width=True)

# ==========================================
# 4. メインUI (Tab 2)
# ==========================================
with tab2:
    st.title("📈 成長ログ")
    try:
        # キャッシュされた関数がない場合は直接取得してテスト
        h_data = supabase.table("calligraphy_history").select("*").order("written_date", desc=True).execute().data
        if h_data:
            for h in h_data:
                with st.container():
                    st.divider()
                    c1, c2, c3 = st.columns([2, 1, 1])
                    with c1: st.subheader(f"📅 {h['written_date']} | 判定: {h['grade']}")
                    with c3:
                        if st.checkbox("削除確定", key=f"c_{h['id']}"):
                            if st.button("🗑️ 削除", key=f"d_{h['id']}"):
                                supabase.table("calligraphy_history").delete().eq("id", h['id']).execute()
                                st.cache_data.clear(); st.rerun()

                    show_red = st.toggle("アドバイスを表示", value=True, key=f"t_{h['id']}")
                    col_m, col_p = st.columns(2)
                    with col_m:
                        if h['m_url']:
                            st.image(h['m_url'], caption="お手本", use_container_width=True)
                    with col_p:
                        if h['p_url']:
                            if show_red:
                                try:
                                    resp = requests.get(h['p_url'], timeout=10)
                                    img_h = ImageOps.exif_transpose(Image.open(io.BytesIO(resp.content)))
                                    st.image(draw_red_pen(img_h, h['corrections']), caption="添削", use_container_width=True)
                                except: st.image(h['p_url'], caption="あなたの作品", use_container_width=True)
                            else:
                                st.image(h['p_url'], caption="あなたの作品", use_container_width=True)
                    st.info(h['comment'])
                    for c in (h.get('corrections') or []):
                        with st.expander(f"✨ {c.get('label')}"): st.write(c.get('description'))
    except Exception as e:
        st.error(f"履歴の読み込みに失敗しました。設定を確認してください: {e}")
