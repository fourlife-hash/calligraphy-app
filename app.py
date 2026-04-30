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

@st.cache_resource
def get_supabase_client():
    """Supabaseクライアントを一度だけ生成（Secretsに依存）"""
    sb_url = st.secrets.get("SUPABASE_URL")
    sb_key = st.secrets.get("SUPABASE_KEY")
    if not sb_url or not sb_key:
        return None
    return create_client(sb_url, sb_key)

def handle_sidebar_and_genai():
    """サイドバーのUI描画とGenAIの構成設定"""
    st.sidebar.title("道場の設定")
    user_api_key = st.sidebar.text_input("ご自身のGoogle API Keyを入力してください", type="password")
    
    # 優先順位: 1.ユーザー入力 2.Secrets
    api_key = user_api_key if user_api_key else st.secrets.get("GOOGLE_API_KEY")
    
    if not api_key:
        st.info("左側のサイドバーに API Key を入力してください。")
        st.stop()
    
    # 修正ポイント: キャッシュせず、取得したキーで毎回構成を更新（または上書き）する
    # これによりサイドバーでのキー変更が即座に適用されます
    genai.configure(api_key=api_key)
    
    # キーを更新したことを明示したい場合の補助ボタン（任意）
    if user_api_key and st.sidebar.button("キャッシュをクリアして再起動"):
        st.cache_data.clear()
        st.cache_resource.clear()
        st.rerun()
        
    return api_key

# 初期化実行
active_api_key = handle_sidebar_and_genai()
supabase: Client = get_supabase_client()

if not supabase:
    st.error("Supabaseの設定が未完了です。secrets.tomlを確認してください。")
    st.stop()

# 清風師範の設定
SYSTEM_PROMPT = """あなたは温厚で丁寧な書道師範「清風（せいふう）」です。
画像内の筆跡をミリ単位で分析し、優しく添削してください。
必ず以下のJSON形式でのみ回答してください：
{
"grade": "〇級 または 〇段",
"overall_comment": "素晴らしい点と、前向きな総評",
"corrections": [{"point": [y, x], "label": "もっと良くなるポイント", "description": "優しく具体的なアドバイス"}]
}
※座標指定の鉄則：画像全体を[縦1000, 横1000]とし、筆跡の真上を正確に[y, x]で指定せよ。"""

# ==========================================
# 2. データ取得 & 画像処理 (キャッシュ対応)
# ==========================================

@st.cache_data(ttl=60, show_spinner=False)
def fetch_history():
    """DBから履歴を取得（60秒間キャッシュ）"""
    try:
        response = supabase.table("calligraphy_history").select("*").order("written_date", desc=True).execute()
        return response.data
    except Exception as e:
        st.error(f"履歴の取得に失敗しました: {e}")
        return []

@st.cache_data(show_spinner=False)
def fetch_image_content(url):
    """URLから画像をダウンロードし、内容をキャッシュする"""
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return resp.content
    except Exception:
        return None

def load_and_fix_image(uploaded_file):
    if uploaded_file is None: return None
    img = Image.open(uploaded_file)
    img = ImageOps.exif_transpose(img)
    img.thumbnail((1024, 1024))
    return img

def upload_image_to_supabase(img, filename):
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format='PNG')
    path = f"public/{filename}.png"
    try:
        supabase.storage.from_("images").upload(
            path, img_byte_arr.getvalue(), {"content-type": "image/png", "x-upsert": "true"}
        )
        return supabase.storage.from_("images").get_public_url(path)
    except Exception as e:
        st.error(f"蔵への保存に失敗しました: {e}")
        return None

def draw_red_pen(base_image, corrections):
    if not corrections: return base_image
    canvas = base_image.copy()
    draw = ImageDraw.Draw(canvas)
    w, h = canvas.size
    
    font = None
    font_size = max(26, int(w / 32))
    font_candidates = ["IPAexMincho.ttf", "msmincho.ttc", "YuMincho.ttc", "/System/Library/Fonts/ヒラギノ明朝 ProN.ttc", "/usr/share/fonts/fonts-japanese-mincho.ttf"]
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
            
            y_val, x_val = float(point[0]), float(point[1])
            if not (0 <= y_val <= 1000 and 0 <= x_val <= 1000): continue

            py, px = (y_val / 1000) * h, (x_val / 1000) * w
            r = max(22, w / 42)
            draw.ellipse([px-r, py-r, px+r, py+r], outline="red", width=max(4, int(w/150)))
            draw.ellipse([px-r+7, py-r+7, px+r-7, py+r-7], outline="red", width=1)
            
            label = corr.get("label", f"点{i+1}")
            txt_bbox = draw.textbbox((px + r + 10, py - r), label, font=font)
            draw.rectangle([txt_bbox[0]-5, txt_bbox[1]-2, txt_bbox[2]+5, txt_bbox[3]+2], fill="white", outline="red", width=1)
            draw.text((px + r + 10, py - r), label, fill="red", font=font)
        except Exception: continue
    return canvas

# ==========================================
# 3. メインUI (Tab 1: 鑑定)
# ==========================================
st.set_page_config(page_title="AI書道師範・清風", layout="wide")
tab1, tab2 = st.tabs(["🖌️ 師範の鑑定", "📈 成長ログ"])

with tab1:
    st.title("🖌️ AI書道師範：清風")
    
    col1, col2 = st.columns(2)
    with col1:
        model_file = st.file_uploader("お手本（理想の字）", type=["jpg", "png", "jpeg"], key="up_m")
    with col2:
        practice_file = st.file_uploader("あなたの作品（必須）", type=["jpg", "png", "jpeg"], key="up_p")
        written_d = st.date_input("書いた日", datetime.now())

    if practice_file:
        p_img_obj = load_and_fix_image(practice_file)
        st.image(p_img_obj, caption="現在の作品", width=300)

        if st.button("清風師範に見ていただく", type="primary"):
            with st.spinner("せいふう師範が鑑定中です..."):
                try:
                    model = genai.GenerativeModel(
                        model_name='gemini-2.5-flash',
                        system_instruction=SYSTEM_PROMPT,
                        generation_config={"response_mime_type": "application/json"}
                    )
                    content = []
                    m_img_obj = load_and_fix_image(model_file)
                    if m_img_obj: content.extend(["これが『お手本』画像です。", m_img_obj])
                    content.extend(["こちらが添削する作品です。優しく教えてください。", p_img_obj])
                    
                    response = model.generate_content(content)
                    data = json.loads(response.text)

                    eid = str(uuid.uuid4())[:8]
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
                        # 新しいデータを反映させるためキャッシュクリア
                        st.cache_data.clear()
                        st.session_state.last_res = data
                        st.session_state.last_img = p_img_obj
                        st.success("鑑定完了！蔵（クラウド）に大切に保管しました。")
                except google_exceptions.ResourceExhausted:
                    st.error("師範が少しお休みに入られました（API制限）。1分ほどお待ちください。")
                except Exception as e:
                    st.error(f"鑑定中に何か起きたようです: {e}")

        if 'last_res' in st.session_state:
            res = st.session_state.last_res
            st.success(f"## 今回の判定: {res.get('grade', '応援しています')}")
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
    history = fetch_history()
    
    if history:
        for h in history:
            eid = h['id']
            with st.container():
                st.divider()
                c1, c2, c3 = st.columns([2, 1, 1])
                with c1: st.subheader(f"📅 {h['written_date']} | 判定: {h['grade']}")
                with c3:
                    if st.button("削除", key=f"del_{eid}"):
                        paths_to_remove = [f"public/{eid}_p.png"]
                        if h['m_url']: paths_to_remove.append(f"public/{eid}_m.png")
                        try:
                            supabase.storage.from_("images").remove(paths_to_remove)
                        except Exception: pass
                        supabase.table("calligraphy_history").delete().eq("id", eid).execute()
                        st.cache_data.clear()
                        st.rerun()

                show_red = st.toggle("アドバイスを表示", value=True, key=f"t_{eid}")
                col_m, col_p = st.columns(2)
                
                with col_m:
                    if h['m_url']: st.image(h['m_url'], caption="目指したお手本", use_container_width=True)
                with col_p:
                    if h['p_url']:
                        if show_red:
                            img_data = fetch_image_content(h['p_url'])
                            if img_data:
                                img_h = ImageOps.exif_transpose(Image.open(io.BytesIO(img_data)))
                                st.image(draw_red_pen(img_h, h['corrections']), caption="添削結果", use_container_width=True)
                        else:
                            st.image(h['p_url'], caption="あなたの作品", use_container_width=True)
                
                st.info(h['comment'])
                for c in h['corrections']:
                    with st.expander(f"✨ {c['label']}"):
                        st.write(c['description'])
    else:
        st.write("まだ記録がありません。")
