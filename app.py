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

# ==========================================
# 1. セキュリティ & 外部連携設定
# ==========================================
def initialize_app():
    st.sidebar.title("道場の設定")
    user_api_key = st.sidebar.text_input("ご自身のGoogle API Keyを入力してください", type="password")
    
    api_key = user_api_key if user_api_key else st.secrets.get("GOOGLE_API_KEY")
    sb_url = st.secrets.get("SUPABASE_URL")
    sb_key = st.secrets.get("SUPABASE_KEY")
    
    if not api_key or not sb_url or not sb_key:
        st.info("左側のサイドバーに API Key を入力するか、secrets.tomlを設定してください。")
        st.stop()
        
    genai.configure(api_key=api_key)
    return create_client(sb_url, sb_key)

supabase: Client = initialize_app()

SYSTEM_PROMPT = """あなたは温厚で丁寧な書道師範「清風（せいふう）」です。
生徒の書いた字の良いところを見つけ、褒めて伸ばす指導を心がけてください。
【任務】
1. 具体的鑑定: 結体や筆致を分析してください。
2. お手本比較: お手本がある場合、骨格の差を優しく教えてください。
3. 判定: 日本の書道教室に基づき「〇級」または「〇段」を決定してください。

必ず以下のJSON形式でのみ回答してください：
{
"grade": "〇級 または 〇段",
"overall_comment": "素晴らしい点と、前向きな総評",
"corrections": [{"point": [y, x], "label": "もっと良くなるポイント", "description": "優しく具体的なアドバイス"}]
}"""

# ==========================================
# 2. 画像処理 & 通信関数
# ==========================================

def load_and_fix_image(uploaded_file):
    if uploaded_file is None: return None
    img = Image.open(uploaded_file)
    img = ImageOps.exif_transpose(img) # 回転補正
    img.thumbnail((1024, 1024))
    return img

def upload_image_to_supabase(img, filename):
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format='PNG')
    path = f"public/{filename}.png"
    try:
        supabase.storage.from_("images").upload(path, img_byte_arr.getvalue(), {"content-type": "image/png"})
        return supabase.storage.from_("images").get_public_url(path)
    except Exception as e:
        st.error(f"画像の保存に失敗しました: {e}")
        return None

def draw_red_pen(base_image, corrections):
    if not corrections: return base_image
    canvas = base_image.copy()
    draw = ImageDraw.Draw(canvas)
    w, h = canvas.size
    
    font = None
    font_size = max(24, int(w / 30))
    font_candidates = ["msmincho.ttc", "YuMincho.ttc", "/System/Library/Fonts/ヒラギノ明朝 ProN.ttc", "/usr/share/fonts/fonts-japanese-mincho.ttf"]
    for f in font_candidates:
        try:
            font = ImageFont.truetype(f, font_size)
            break
        except Exception: continue
    if not font: font = ImageFont.load_default()

    for i, corr in enumerate(corrections):
        try:
            point = corr.get("point")
            if not isinstance(point, (list, tuple)) or len(point) < 2: continue
            px, py = (point[1] / 1000) * w, (point[0] / 1000) * h
            r = max(20, w / 40)
            draw.ellipse([px-r, py-r, px+r, py+r], outline="red", width=max(3, int(w/150)))
            label = corr.get("label", f"ヒント{i+1}")
            draw.text((px + r + 5, py - r), label, fill="red", font=font)
        except Exception as e:
            st.warning(f"ヒント {i+1} の描画に失敗しました: {e}")
            continue
    return canvas

# ==========================================
# 3. メインUI (Tab 1: 鑑定)
# ==========================================
st.set_page_config(page_title="AI書道師範・清風", layout="wide")
tab1, tab2 = st.tabs(["🖌️ 師範の鑑定", "📈 成長ログ"])

with tab1:
    st.title("🖌️ AI書道師範：清風")
    st.caption("最新モデル Gemini 2.5 Flash / JSONモード / Supabase連携")
    
    col1, col2 = st.columns(2)
    with col1:
        model_file = st.file_uploader("お手本（理想の字）", type=["jpg", "png", "jpeg"])
    with col2:
        practice_file = st.file_uploader("あなたの作品（必須）", type=["jpg", "png", "jpeg"])
        written_d = st.date_input("書いた日", datetime.now())

    if practice_file:
        p_img_obj = load_and_fix_image(practice_file)
        st.image(p_img_obj, caption="鑑定対象", width=300)

        if st.button("清風師範に見ていただく", type="primary"):
            with st.spinner("清風師範がじっくり拝見しております..."):
                try:
                    model = genai.GenerativeModel(
                        model_name='gemini-2.5-flash',
                        system_instruction=SYSTEM_PROMPT,
                        generation_config={"response_mime_type": "application/json"}
                    )
                    
                    content = []
                    m_img_obj = None
                    if model_file:
                        m_img_obj = load_and_fix_image(model_file)
                        content.append("これが『お手本』画像です。")
                        content.append(m_img_obj)
                    
                    content.append("こちらが添削する『生徒の作品』です。優しく教えてください。")
                    content.append(p_img_obj)
                    
                    response = model.generate_content(content)
                    data = json.loads(response.text)

                    # クラウド保存（Storage & DB）
                    eid = str(uuid.uuid4())[:8]
                    p_url = upload_image_to_supabase(p_img_obj, f"{eid}_p")
                    m_url = upload_image_to_supabase(m_img_obj, f"{eid}_m") if m_img_obj else None

                    supabase.table("calligraphy_history").insert({
                        "id": eid, "written_date": str(written_d),
                        "grade": data.get("grade", "応援しています"), 
                        "comment": data.get("overall_comment", ""),
                        "corrections": data.get("corrections", []),
                        "p_url": p_url, "m_url": m_url
                    }).execute()
                    
                    st.session_state.last_res = data
                    st.session_state.last_img = p_img_obj
                    st.success("鑑定完了！蔵（クラウド）に大切に保管しました。")

                except google_exceptions.ResourceExhausted:
                    st.error("師範が少しお休み中です（API制限）。1〜2分待ってみましょう。")
                except Exception as e:
                    st.error(f"鑑定中に何か起きたようです: {e}")

        if 'last_res' in st.session_state:
            res = st.session_state.last_res
            st.success(f"## 判定: {res.get('grade', '応援しています')}")
            st.info(res.get('overall_comment', ''))
            st.image(draw_red_pen(st.session_state.last_img, res.get('corrections', [])), use_container_width=True)
            for c in res.get('corrections', []):
                with st.expander(f"✨ {c.get('label')}", expanded=True):
                    st.write(c.get('description'))

# ==========================================
# 4. メインUI (Tab 2: ログ)
# ==========================================
with tab2:
    st.title("📈 成長ログ")
    try:
        response = supabase.table("calligraphy_history").select("*").order("written_date", desc=True).execute()
        history = response.data
        
        if history:
            for h in history:
                eid = h['id']
                with st.container():
                    st.divider()
                    c1, c2, c3 = st.columns([2, 1, 1])
                    with c1: st.subheader(f"📅 {h['written_date']} | 判定: {h['grade']}")
                    with c3:
                        if st.button("削除", key=f"del_{eid}"):
                            supabase.table("calligraphy_history").delete().eq("id", eid).execute()
                            st.rerun()

                    show_red = st.toggle("アドバイスを表示", value=True, key=f"t_{eid}")
                    col_m, col_p = st.columns(2)
                    
                    with col_m:
                        if h['m_url']:
                            st.image(h['m_url'], caption="目指したお手本", use_container_width=True)
                    
                    with col_p:
                        if h['p_url']:
                            if show_red:
                                # アドバイス表示時のみ画像をダウンロードして加工
                                p_resp = requests.get(h['p_url'])
                                img_h = Image.open(io.BytesIO(p_resp.content))
                                img_h = ImageOps.exif_transpose(img_h)
                                st.image(draw_red_pen(img_h, h['corrections']), caption="添削結果", use_container_width=True)
                            else:
                                # アドバイス非表示時はURLを直接渡して高速表示！
                                st.image(h['p_url'], caption="あなたの作品", use_container_width=True)
                    
                    st.info(h['comment'])
                    for c in h['corrections']:
                        with st.expander(f"✨ {c.get('label')}"):
                            st.write(c.get('description'))
        else:
            st.write("まだ記録がありません。一枚書いてみませんか？")
    except Exception as e:
        st.write(f"履歴の読み込みに失敗しました。設定を確認してください: {e}")
