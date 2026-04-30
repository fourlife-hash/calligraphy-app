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
# 1. 定数・プロンプト定義
# ==========================================
BUCKET_NAME = "IMAGES"

# AIへの役割とタスクの定義（静的）
BASE_SYSTEM_PROMPT = """あなたは温厚で丁寧な書道師範「清風」です。
提供された画像のサイズ情報に基づき、[縦1000, 横1000]の正確な方眼として座標を捉え、筆跡を分析してください。

【任務】
1. 具体的鑑定: 結体や筆致を分析してください。
2. お手本比較: お手本がある場合、骨格の差を優しく教えてください。
3. 判定: 日本の書道教室に基づき「〇級」または「〇段」を決定してください。

必ず以下のJSON形式でのみ回答してください：
{
"grade": "〇級 または 〇段",
"overall_comment": "素晴らしい点と、前向きな総評",
"corrections": [{ "point": [y, x], "label": "修正箇所", "description": "指導内容" }]
}
※座標指定：画像全体を[縦1000, 横1000]とし、修正箇所の真上に正確に[y, x]を指定せよ。"""

# ==========================================
# 2. セキュリティ & 外部連携初期化
# ==========================================

@st.cache_resource
def get_supabase_client():
    """Supabaseクライアントを一度だけ生成"""
    sb_url = st.secrets.get("SUPABASE_URL")
    sb_key = st.secrets.get("SUPABASE_KEY")
    if not sb_url or not sb_key:
        return None
    return create_client(sb_url, sb_key)

def handle_sidebar_configs():
    """サイドバーUI描画とAPIキー解決"""
    st.sidebar.title("道場の設定")
    user_api_key = st.sidebar.text_input("ご自身のGoogle API Keyを入力", type="password")
    
    # 優先順位: ユーザー入力 > secrets
    api_key = user_api_key if user_api_key else st.secrets.get("GOOGLE_API_KEY")
    
    if not api_key:
        st.info("左側のサイドバーに API Key を入力してください。")
        st.stop()
        
    genai.configure(api_key=api_key)

# 初期化実行
handle_sidebar_configs()
supabase: Client = get_supabase_client()

if not supabase:
    st.error("Supabaseの設定が未完了です。")
    st.stop()

# ==========================================
# 3. データフェッチ & 画像処理 (キャッシュ対応)
# ==========================================

@st.cache_data(ttl=300, show_spinner=False)
def fetch_history(_client: Client):
    """DBから履歴を取得（引数にクライアントを渡し、シリアライズ警告を回避）"""
    try:
        response = _client.table("calligraphy_history").select("*").order("written_date", desc=True).execute()
        return response.data
    except Exception as e:
        st.error(f"履歴の取得に失敗しました: {e}")
        return []

@st.cache_data(show_spinner=False)
def fetch_image_content(url):
    """URLから画像をダウンロードし、内容をキャッシュする"""
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        return resp.content
    except Exception:
        return None

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
        return supabase.storage.from_(BUCKET_NAME).get_public_url(path)
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
    current_dir = Path(__file__).parent
    
    font_candidates = [
        current_dir / "ipaexm.ttf",
        current_dir / "IPAexMincho.ttf",
        current_dir / "NotoSansJP-Regular.ttf",
        Path("msmincho.ttc")
    ]
    
    for f_path in font_candidates:
        try:
            font = ImageFont.truetype(str(f_path), font_size)
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
            txt_bbox = draw.textbbox((px + r + 5, py - r), label, font=font)
            draw.rectangle([txt_bbox[0]-5, txt_bbox[1]-2, txt_bbox[2]+5, txt_bbox[3]+2], fill="white", outline="red", width=1)
            draw.text((px + r + 5, py - r), label, fill="red", font=font)
        except Exception: continue
    return canvas

# ==========================================
# 4. メインUI
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
        w_px, h_px = p_img_obj.size
        st.image(p_img_obj, caption="現在の作品", width=300)

        if st.button("清風師範に見ていただく", type="primary"):
            with st.spinner("せいふう師範が鑑定中です..."):
                try:
                    # プロンプトの構築（動的部分と静的部分を物理的に分離）
                    context = f"【画像解析コンテキスト：画像幅{w_px}px, 高さ{h_px}px】\n"
                    full_prompt = context + BASE_SYSTEM_PROMPT
                    
                    model = genai.GenerativeModel(
                        model_name='gemini-2.5-flash',
                        system_instruction=full_prompt,
                        generation_config={"response_mime_type": "application/json"}
                    )
                    
                    # Contentの構築（安全な追加方式）
                    content_list = []
                    m_img_obj = load_and_fix_image(model_file)
                    if m_img_obj:
                        content_list.extend(["これが『お手本』画像です。", m_img_obj])
                    content_list.extend(["こちらが添削する作品です。優しく教えてください。", p_img_obj])
                    
                    response = model.generate_content(content_list)
                    
                    # JSONデコードの堅牢化
                    data = None
                    try:
                        data = json.loads(response.text)
                    except json.JSONDecodeError:
                        st.error("師範の回答が少し乱れました。もう一度だけ鑑定ボタンを押してみてください。")
                    
                    if data:
                        eid = str(uuid.uuid4()) # フルUUIDで衝突回避
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
                            st.success("鑑定完了！蔵（クラウド）に大切に保管しました。")
                        else:
                            st.error("蔵への保存に失敗しました。")

                except google_exceptions.ResourceExhausted:
                    st.error("師範が少しお休み中です（API制限）。1分ほどお待ちください。")
                except Exception as e:
                    st.error(f"鑑定中に何か起きたようです: {e}")

        if 'last_res' in st.session_state:
            res = st.session_state.last_res
            st.success(f"## 今回の判定: {res.get('grade', '応援しています')}")
            st.info(res.get('overall_comment', ''))
            st.image(draw_red_pen(st.session_state.last_img, res.get('corrections', [])), use_container_width=True)
            for c in (res.get('corrections') or []):
                with st.expander(f"✨ {c.get('label')}", expanded=True):
                    st.write(c.get('description'))

with tab2:
    st.title("📈 成長ログ")
    history = fetch_history(supabase) # クライアントを明示的に渡す
    if history:
        for h in history:
            eid = h['id']
            with st.container():
                st.divider()
                c1, c2, c3 = st.columns([2, 1, 1])
                with c1: st.subheader(f"📅 {h['written_date']} | 判定: {h['grade']}")
                with c3:
                    confirm = st.checkbox("削除を確定", key=f"conf_{eid}")
                    if st.button("🗑️ この記録を削除", key=f"del_{eid}", disabled=not confirm):
                        # Storage削除（実在チェックを兼ねた安全なリスト構築）
                        del_paths = [f"{eid}_p.png"]
                        if h.get('m_url'): del_paths.append(f"{eid}_m.png")
                        
                        try: supabase.storage.from_(BUCKET_NAME).remove(del_paths)
                        except Exception: pass
                        
                        supabase.table("calligraphy_history").delete().eq("id", eid).execute()
                        st.cache_data.clear()
                        st.rerun()

                show_red = st.toggle("アドバイスを表示", value=True, key=f"t_{eid}")
                col_m, col_p = st.columns(2)
                with col_m:
                    if h.get('m_url'): st.image(h['m_url'], caption="目指したお手本", use_container_width=True)
                with col_p:
                    if h.get('p_url'):
                        if show_red:
                            img_data = fetch_image_content(h['p_url'])
                            if img_data:
                                try:
                                    img_h = ImageOps.exif_transpose(Image.open(io.BytesIO(img_data)))
                                    st.image(draw_red_pen(img_h, h.get('corrections', [])), caption="添削結果", use_container_width=True)
                                except Exception:
                                    st.image(h['p_url'], caption="作品(直接表示)", use_container_width=True)
                        else:
                            st.image(h['p_url'], caption="あなたの作品", use_container_width=True)
                
                st.info(h['comment'])
                for c in (h.get('corrections') or []):
                    with st.expander(f"✨ {c.get('label')}"):
                        st.write(c.get('description'))
    else:
        st.write("まだ記録がありません。")
