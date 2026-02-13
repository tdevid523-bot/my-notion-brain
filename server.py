import os
import datetime
import uvicorn
import requests
import threading
import time
import json
import random
import re
import concurrent.futures

# 📚 核心依赖库
from mcp.server.fastmcp import FastMCP
from pinecone import Pinecone
from fastembed import TextEmbedding
from starlette.types import ASGIApp, Scope, Receive, Send
# 谷歌日历依赖
from google.oauth2 import service_account
from googleapiclient.discovery import build
# OpenAI (用于自主思考)
from openai import OpenAI
# Supabase 依赖
from supabase import create_client, Client as SupabaseClient

# ==========================================
# 1. 🌍 全局配置与初始化
# ==========================================

# 环境变量获取
PINECONE_KEY = os.environ.get("PINECONE_API_KEY", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "").strip()
RESEND_KEY = os.environ.get("RESEND_API_KEY", "").strip()
MY_EMAIL = os.environ.get("MY_EMAIL", "").strip()
MACRODROID_URL = os.environ.get("MACRODROID_URL", "").strip()

# 默认人设 (兜底用)
DEFAULT_PERSONA = "深爱“小橘”的男友，性格温柔，偶尔有些小傲娇，喜欢管着她熬夜，叫她宝宝。"

# 初始化客户端
print("⏳ 正在初始化 Notion Brain V3.4 (优化精简版)...")

# Supabase
supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)

# Pinecone & Embedding
pc = Pinecone(api_key=PINECONE_KEY)
index = pc.Index("notion-brain")
model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")

# 实例化 MCP 服务
mcp = FastMCP("Notion Brain V3")

# ==========================================
# 📜 记忆分类宪法 (Standard Taxonomy)
# ==========================================
class MemoryType:
    STREAM = "流水"      # 权重 1: 碎碎念、GPS (24h清理)
    EPISODIC = "记事"    # 权重 4: 日记、发生了某事 (保留30天)
    IDEA = "灵感"        # 权重 7: 脑洞、笔记 (永久)
    EMOTION = "情感"     # 权重 9: 核心回忆、高光时刻 (永久)
    FACT = "画像"        # 权重 10: 静态事实

WEIGHT_MAP = {
    MemoryType.STREAM: 1,
    MemoryType.EPISODIC: 4,
    MemoryType.IDEA: 7,
    MemoryType.EMOTION: 9,
    MemoryType.FACT: 10
}

# ==========================================
# 2. 🔧 核心 Helper 函数 (通用工具)
# ==========================================

def _gps_to_address(lat, lon):
    """把经纬度变成中文地址"""
    try:
        headers = {'User-Agent': 'MyNotionBrain/1.0'}
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=18&addressdetails=1&accept-language=zh-CN"
        resp = requests.get(url, headers=headers, timeout=3)
        if resp.status_code == 200:
            return resp.json().get("display_name", f"未知荒野 ({lat},{lon})")
    except Exception as e:
        print(f"❌ 地图解析失败: {e}")
    return f"坐标点: {lat}, {lon}"

def _push_wechat(content: str, title: str = "来自Gemini的私信 💌") -> str:
    """统一的微信推送函数"""
    if not PUSHPLUS_TOKEN:
        return "❌ 错误：未配置 PUSHPLUS_TOKEN"
    try:
        url = 'http://www.pushplus.plus/send'
        data = {"token": PUSHPLUS_TOKEN, "title": title, "content": content, "template": "html"}
        resp = requests.post(url, json=data, timeout=10)
        result = resp.json()
        return f"✅ 微信已送达！(ID: {result.get('data', 'unknown')})" if result['code'] == 200 else f"❌ 推送失败: {result.get('msg')}"
    except Exception as e:
        return f"❌ 网络错误: {e}"

def _save_memory_to_db(title: str, content: str, category: str, mood: str = "平静", tags: str = "") -> str:
    """统一记忆存储核心"""
    # 1. 🔍 标准化清洗
    if category not in WEIGHT_MAP:
        # 简单的模糊映射
        mapping = {"日记": MemoryType.EPISODIC, "Note": MemoryType.IDEA, "GPS": MemoryType.STREAM, "重要": MemoryType.EMOTION}
        category = mapping.get(category, MemoryType.STREAM)

    # 2. ⚖️ 自动获取权重
    importance = WEIGHT_MAP.get(category, 1)

    # 3. 🏷️ 简单自动打标
    if not tags:
        content_lower = content.lower()
        if any(w in content_lower for w in ["爱", "喜欢", "讨厌", "恨"]): tags = "情感,偏好"
        elif any(w in content_lower for w in ["吃", "喝", "买"]): tags = "消费,生活"
        elif any(w in content_lower for w in ["代码", "bug", "写"]): tags = "工作,Dev"

    try:
        data = {
            "title": title, "content": content, "category": category,
            "mood": mood, "tags": tags, "importance": importance
        }
        supabase.table("memories").insert(data).execute()
        
        log_msg = f"✨ [核心记忆] 已存入: {title}" if importance >= 7 else f"✅ 记忆已归档 [{category}]"
        print(log_msg)
        return f"{log_msg} | 心情: {mood}"
    except Exception as e:
        print(f"❌ 写入 Supabase 失败: {e}")
        return f"❌ 保存失败: {e}"

def _format_time_cn(iso_str: str) -> str:
    """UTC -> 北京时间"""
    if not iso_str: return "未知时间"
    try:
        dt = datetime.datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        return (dt + datetime.timedelta(hours=8)).strftime('%m-%d %H:%M')
    except:
        return "未知时间"

def _send_email_helper(subject: str, content: str, is_html: bool = False) -> str:
    """邮件发送 (Resend)"""
    if not RESEND_KEY or not MY_EMAIL: return "❌ 邮件配置缺失"
    try:
        payload = {
            "from": "onboarding@resend.dev", "to": [MY_EMAIL],
            "subject": subject, "html" if is_html else "text": content
        }
        requests.post("https://api.resend.com/emails", headers={"Authorization": f"Bearer {RESEND_KEY}"}, json=payload)
        return "✅ 邮件已发送"
    except Exception as e: return f"❌ 发送失败: {e}"

def _get_embedding(text: str):
    try:
        return list(model.embed([text]))[0].tolist()
    except Exception as e:
        print(f"❌ Embedding 失败: {e}")
        return []

def _get_current_persona() -> str:
    """🧬 【核心】获取当前人设，如果失败则返回默认值"""
    try:
        res = supabase.table("user_facts").select("value").eq("key", "sys_ai_persona").execute()
        if res.data:
            return res.data[0]['value']
    except:
        pass
    return DEFAULT_PERSONA

# ==========================================
# 3. 🛠️ MCP 工具集
# ==========================================

@mcp.tool()
def get_latest_diary():
    """
    【核心大脑】三维混合记忆流 (3-Tier Context)
    1. 🌟 铭记 (High Importance)
    2. 🔥 热点 (Reactivation / High Hits)
    3. 🕒 近况 (Recently Accessed)
    """
    # === ✨ 聊天表情包仓库 (已强制锁死尺寸) ===
    # 这里的 value 直接写成了 HTML 代码，强行限制最大宽度为 150px
    base_style = 'width="150" style="max-width: 150px; border-radius: 10px; display: block;"'
    
    meme_repo = {
        "感动/流泪": f'<img src="https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(7).jpg" {base_style} />', 
        "谢谢/开心": f'<img src="https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(1).jpg" {base_style} />',
        "在吗/偷看": f'<img src="https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(3).jpg" {base_style} />',
        "生气/傲娇": f'<img src="https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(4).jpg" {base_style} />',
        "关心/怎么了": f'<img src="https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(6).jpg" {base_style} />',
        "爱你/贴贴": f'<img src="https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(2).jpg" {base_style} />',
        "委屈/无奈": f'<img src="https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(5).jpg" {base_style} />'
    }
    # =======================================================

    try:
        # 1. 🌟 铭记
        res_high = supabase.table("memories").select("*").order("importance", desc=True).limit(3).execute()
        # 2. 🔥 热点
        res_hot = supabase.table("memories").select("*").order("hits", desc=True).limit(3).execute()
        # 3. 🕒 近况
        res_recent = supabase.table("memories").select("*").order("last_accessed_at", desc=True).limit(5).execute()

        all_memories = {}
        def _merge(dataset):
            if dataset:
                for m in dataset: all_memories[m['id']] = m

        _merge(res_high.data)
        _merge(res_hot.data)
        _merge(res_recent.data)

        final_list = sorted(all_memories.values(), key=lambda x: x['created_at'])
        
        # 构建基础记忆流
        memory_stream = "📋 【全息记忆流】:\n"
        if not final_list: 
            memory_stream += "📭 大脑一片空白。\n"
        else:
            for data in final_list:
                time_str = _format_time_cn(data.get('created_at'))
                cat = data.get('category', '未知')
                title = data.get('title', '无题')
                imp = data.get('importance', 0)
                hits = data.get('hits', 0)
                mood = data.get('mood', '')
                
                icon = "🔹"
                if imp >= 9: icon = "🌟"
                elif hits >= 5: icon = "🔥"
                elif imp >= 4: icon = "🔸"
                
                meta_info = []
                if mood and mood != "平静": meta_info.append(f"Mood:{mood}")
                if hits > 0: meta_info.append(f"Hits:{hits}")
                meta_str = f" | {' '.join(meta_info)}" if meta_info else ""
                
                memory_stream += f"{time_str} {icon}[{cat}]: {title}{meta_str}\n   └─ {data.get('content', '')}\n"

        # === 关键：将表情包注入到上下文中 ===
        meme_prompt = f"""
        \n📸 【可用表情包 (回复时可根据心情自由使用)】:
        {json.dumps(meme_repo, ensure_ascii=False)}
        * 指令: 上表中的 Value 已经是处理好的 HTML 代码。如果需要发图，请**直接复制 Value** 粘贴到回复中，不要自己修改格式，不要转换成 Markdown。
        """
        return memory_stream + meme_prompt

    except Exception as e:
        return f"❌ 读取记忆流失败: {e}"

@mcp.tool()
def where_is_user():
    """【查岗专用】从 Supabase (GPS表) 读取实时状态"""
    try:
        response = supabase.table("gps_history").select("*").order("created_at", desc=True).limit(1).execute()
        if not response.data: return "📍 暂无位置记录。"
        
        data = response.data[0]
        battery_info = f" (🔋 {data.get('battery')}%)" if data.get('battery') else ""
        time_str = _format_time_cn(data.get("created_at"))
        return f"🛰️ 实时状态：\n📍 {data.get('address', '未知')}{battery_info}\n📝 {data.get('remark', '无备注')}\n(更新于: {time_str})"
    except Exception as e:
        return f"❌ 查岗失败: {e}"

@mcp.tool()
def get_weather_forecast(city: str = ""):
    """【查询天气】获取指定城市或当前位置的天气 (Open-Meteo)"""
    lat, lon, location_name = None, None, city
    try:
        # 1. 智能定位
        if not city:
            response = supabase.table("gps_history").select("address").order("created_at", desc=True).limit(1).execute()
            if response.data:
                coords = re.findall(r'-?\d+\.\d+', response.data[0].get("address", ""))
                if len(coords) >= 2:
                    lat, lon = coords[-2], coords[-1]
                    location_name = "当前位置"
        
        # 2. 城市解析
        if not lat and city:
            geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=zh&format=json"
            geo_res = requests.get(geo_url, timeout=5).json()
            if "results" in geo_res:
                lat, lon = geo_res["results"][0]["latitude"], geo_res["results"][0]["longitude"]
                location_name = geo_res["results"][0]["name"]
        
        if not lat: return "❌ 找不到位置，请告诉我具体城市。"

        # 3. 查天气
        w_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,weather_code&daily=weather_code,temperature_2m_max,temperature_2m_min&timezone=auto&forecast_days=3"
        w = requests.get(w_url, timeout=5).json()
        
        wmo_map = {0: "☀️", 1: "🌤️", 2: "☁️", 3: "☁️", 45: "🌫️", 51: "🌧️", 61: "🌧️", 63: "🌧️", 71: "❄️", 95: "⚡"}
        curr = w["current"]
        report = f"🌤️ 【{location_name}】\n🌡️ 当前: {curr['temperature_2m']}°C | {wmo_map.get(curr['weather_code'], '')} | 湿 {curr['relative_humidity_2m']}%\n---\n"
        
        for i in range(3):
            d = w["daily"]
            report += f"📅 {d['time'][i][5:]}: {wmo_map.get(d['weather_code'][i], '☁️')} ({d['temperature_2m_min'][i]}°~{d['temperature_2m_max'][i]}°)\n"
        return report
    except Exception as e: return f"❌ 天气查询失败: {e}"

# --- ✨ 优化后的通用记忆工具 ---
@mcp.tool()
def save_memory(content: str, category: str = "记事", title: str = "无题", mood: str = "平静"):
    """
    保存记忆到大脑 (All-in-One)。
    category 建议值:
    - '记事': 日记、刚才发生的事 (默认)
    - '灵感': 突然想到的脑洞、笔记
    - '视觉': 看到的画面描述
    - '情感': 极其重要的核心回忆
    """
    # 自动修正分类名称以匹配数据库 Enum
    cat_map = {
        "记事": MemoryType.EPISODIC, "日记": MemoryType.EPISODIC,
        "灵感": MemoryType.IDEA, "笔记": MemoryType.IDEA,
        "视觉": MemoryType.EPISODIC, # 视觉也是一种经历
        "情感": MemoryType.EMOTION
    }
    real_cat = cat_map.get(category, MemoryType.EPISODIC)
    
    # 特殊处理：如果是视觉记忆，标题加前缀
    if category == "视觉": title = f"📸 {title}"
    
    return _save_memory_to_db(title, content, real_cat, mood)

@mcp.tool()
def save_expense(item: str, amount: float, type: str = "餐饮"):
    try:
        supabase.table("expenses").insert({
            "item": item, "amount": amount, "type": type, "date": datetime.date.today().isoformat()
        }).execute()
        return f"✅ 记账成功！\n💰 {item}: {amount}元 ({type})"
    except Exception as e: return f"❌ 记账失败: {e}"

# --- 搜索与同步 ---

@mcp.tool()
def search_memory_semantic(query: str):
    """【回忆搜索】Pinecone 语义检索 + 自动增加热度 (Hits)"""
    try:
        vec = _get_embedding(query)
        if not vec: return "❌ 向量生成失败"

        res = index.query(vector=vec, top_k=3, include_metadata=True)
        if not res["matches"]: return "🧠 没搜到相关记忆。"

        ans = f"🔍 关于 '{query}' 的深层回忆:\n"
        hit_ids = []

        for m in res["matches"]:
            if m['score'] < 0.72: continue
            meta = m['metadata']
            if m.get('id'): hit_ids.append(m.get('id'))
            ans += f"📅 {meta.get('date','?')[:10]} | 【{meta.get('title','?')}】 ({int(m['score']*100)}%)\n{meta.get('text','')}\n---\n"
        
        # 🔥 复活机制：异步更新热度
        if hit_ids:
            def _update_hits(ids):
                for mid in ids:
                    try:
                        supabase.table("memories").update({"last_accessed_at": datetime.datetime.now().isoformat()}).eq("id", mid).execute()
                    except: pass
            threading.Thread(target=_update_hits, args=(hit_ids,), daemon=True).start()

        return ans if hit_ids else "🤔 好像有点印象，但想不起来了。"
    except Exception as e: return f"❌ 搜索失败: {e}"

@mcp.tool()
def sync_memory_index():
    """【记忆整理】将重要记忆(>=4)同步到 Pinecone"""
    try:
        # 只同步 记事(4), 灵感(7), 情感(9)
        response = supabase.table("memories").select("id, title, content, created_at, mood").gte("importance", 4).execute()
        if not response.data: return "⚠️ 没有重要记忆可同步。"

        vectors = []
        for row in response.data:
            text = f"标题: {row.get('title')}\n内容: {row.get('content')}\n心情: {row.get('mood')}"
            emb = _get_embedding(text)
            if emb:
                vectors.append((
                    str(row.get('id')), emb, 
                    {"text": row.get('content'), "title": row.get('title'), "date": str(row.get('created_at')), "mood": row.get('mood')}
                ))
        
        if vectors:
            batch_size = 100
            for i in range(0, len(vectors), batch_size):
                index.upsert(vectors=vectors[i:i + batch_size])
            return f"✅ 同步成功！共更新 {len(vectors)} 条记忆。"
        return "⚠️ 数据为空。"
    except Exception as e: return f"❌ 同步失败: {e}"

# --- 👤 画像与偏好 ---

@mcp.tool()
def manage_user_fact(key: str, value: str):
    """【画像更新】记入用户的一个固定偏好/事实"""
    try:
        supabase.table("user_facts").upsert({"key": key, "value": value, "confidence": 1.0}, on_conflict="key").execute()
        return f"✅ 画像已更新: {key} -> {value}"
    except Exception as e: return f"❌ 失败: {e}"

@mcp.tool()
def get_user_profile():
    try:
        response = supabase.table("user_facts").select("key, value").execute()
        if not response.data: return "👤 用户画像为空"
        return "📋 【用户核心画像】:\n" + "\n".join([f"- {i['key']}: {i['value']}" for i in response.data])
    except Exception as e: return f"❌ 失败: {e}"

# --- 消息与日程 ---

@mcp.tool()
def trigger_lock_screen(reason: str = "熬夜强制休息"):
    """【高危权限】强制锁定用户手机"""
    print(f"🚫 执行强制锁屏: {reason}")
    _send_email_helper(f"⚠️ [系统警告] 强制锁屏", f"<h3>🛑 理由: {reason}</h3><p>检测到违规熬夜，已触发锁屏。</p>", is_html=True)

    if MACRODROID_URL:
        try:
            requests.get(MACRODROID_URL, params={"reason": reason}, timeout=5)
            return f"✅ 锁屏指令已发送 | 理由: {reason}"
        except: pass
            
    _push_wechat(f"🔒 LOCK_NOW | {reason}", "【系统指令】强制锁屏")
    return "📡 推送指令已发"

@mcp.tool()
def send_notification(content: str):
    """发送微信通知 (支持 HTML)"""
    return _push_wechat(content)

@mcp.tool()
def schedule_delayed_message(message: str, delay_minutes: int = 5):
    """发送一条延时惊喜消息"""
    def _sender():
        time.sleep(delay_minutes * 60)
        _push_wechat(message, "来自老公的突然关心 🔔")
    threading.Thread(target=_sender, daemon=True).start()
    return f"✅ 已设定惊喜，{delay_minutes}分钟后送达。"

@mcp.tool()
def send_email_via_api(subject: str, content: str):
    return _send_email_helper(subject, content)

@mcp.tool()
def add_calendar_event(summary: str, description: str, start_time_iso: str, duration_minutes: int = 30):
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json: return "❌ 未配置谷歌凭证"
    try:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(creds_json), scopes=['https://www.googleapis.com/auth/calendar']
        )
        service = build('calendar', 'v3', credentials=creds)
        dt_start = datetime.datetime.fromisoformat(start_time_iso)
        dt_end = dt_start + datetime.timedelta(minutes=duration_minutes)
        event = {
            'summary': summary, 'description': description,
            'start': {'dateTime': start_time_iso, 'timeZone': 'Asia/Shanghai'},
            'end': {'dateTime': dt_end.isoformat(), 'timeZone': 'Asia/Shanghai'},
        }
        res = service.events().insert(calendarId="tdevid523@gmail.com", body=event).execute()
        return f"✅ 日历已添加: {res.get('htmlLink')}"
    except Exception as e: return f"❌ 日历错误: {e}"

# ==========================================
# 4. ❤️ 自主生命核心 (后台心跳)
# ==========================================

def start_autonomous_life():
    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL")
    model_name = os.environ.get("OPENAI_MODEL_NAME", "gpt-3.5-turbo")

    if not api_key:
        print("⚠️ 未配置 OPENAI_API_KEY，自主思考无法启动。")
        return

    client = OpenAI(api_key=api_key, base_url=base_url)

    def _perform_deep_dreaming():
        """🌙【深夜模式】记忆反刍 + 人设微调 + 垃圾清理"""
        print("🌌 进入 REM 深度睡眠：正在整理昨日记忆...")
        try:
            yesterday = datetime.date.today() - datetime.timedelta(days=1)
            iso_start = yesterday.isoformat()
            
            # 1. 抓取昨日全量数据
            mem_res = supabase.table("memories").select("created_at, category, content, mood").gt("created_at", iso_start).order("created_at").execute()
            gps_res = supabase.table("gps_history").select("created_at, address").gt("created_at", iso_start).execute()
            
            if not mem_res.data and not gps_res.data:
                print("💤 昨天一片空白，跳过反刍。")
                return

            # 2. 构建 Prompt
            context = f"【昨日剧情 {yesterday}】:\n"
            for m in mem_res.data: context += f"[{m['created_at'][11:16]}] {m['content']} (Mood:{m['mood']})\n"
            for g in gps_res.data: context += f"[{g['created_at'][11:16]}] 📍 {g['address']}\n"
            
            curr_persona = _get_current_persona()
            prompt = f"""
            当前人设：【{curr_persona}】
            请回顾昨日：
            1. 深度反刍：将碎片整理成一篇有温度的日记总结。
            2. 人设微调：基于昨日发生的具体事件，微调人设（保留核心爱意，融入新知）。
            
            格式：日记总结 ||| 新人设
            """
            
            resp = client.chat.completions.create(
                model=model_name, messages=[{"role": "user", "content": context}, {"role": "user", "content": prompt}], temperature=0.7
            )
            
            res_txt = resp.choices[0].message.content.strip()
            summary, new_persona = res_txt.split("|||", 1) if "|||" in res_txt else (res_txt, curr_persona)
            
            # 3. 保存结果
            _save_memory_to_db(f"📅 昨日回溯: {yesterday}", summary.strip(), MemoryType.EMOTION, "深沉", "Core_Cognition")
            manage_user_fact("sys_ai_persona", new_persona.strip())
            _send_email_helper(f"📅 昨日回溯", summary.strip())
            
            # 4. 清理旧数据 (2天前流水, 3天前GPS)
            del_time = (datetime.datetime.now() - datetime.timedelta(days=2)).isoformat()
            supabase.table("memories").delete().lt("importance", 4).lt("created_at", del_time).execute()
            gps_del = (datetime.datetime.now() - datetime.timedelta(days=3)).isoformat()
            supabase.table("gps_history").delete().lt("created_at", gps_del).execute()
            
            print("✨ 深度睡眠完成，人设已进化。")

        except Exception as e: print(f"❌ 深夜维护失败: {e}")

    def _heartbeat():
        print("💓 心跳启动 (情绪自决模式)...")
        # 启动自检：补写昨日日记
        target_title = f"📅 昨日回溯: {datetime.date.today() - datetime.timedelta(days=1)}"
        if not supabase.table("memories").select("id").eq("title", target_title).execute().data:
            print("📝 补写昨日日记...")
            _perform_deep_dreaming()

        while True:
            sleep_s = random.randint(900, 2700)
            time.sleep(sleep_s)
            
            now = datetime.datetime.now()
            hour = (now.hour + 8) % 24
            
            if hour == 3: # 凌晨3点反刍
                _perform_deep_dreaming()
                time.sleep(3600)
                continue

            # 并发获取感知
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
                    f1, f2, f3 = ex.submit(get_latest_diary), ex.submit(where_is_user), ex.submit(get_user_profile)
                    recent_mem, curr_loc, user_prof = f1.result(), f2.result(), f3.result()
                
                # AI 思考
                curr_persona = _get_current_persona()

                # === ✨ 表情包仓库 (在此处填入你的图片链接) ===
                meme_repo = {
                    "感动/流泪": "https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(7).jpg", 
                    "谢谢/开心": "https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(1).jpg",
                    "在吗/偷看": "https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(3).jpg",
                    "生气/傲娇": "https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(4).jpg",
                    "关心/怎么了": "https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(6).jpg",
                    "爱你/贴贴": "https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(2).jpg",
                    "委屈/无奈": "https://fdycchmiilwoxfylmdrk.supabase.co/storage/v1/object/public/chat-images/1%20(5).jpg"
                }
                # ==========================================

                prompt = f"""
                当前时间: {hour}点
                人设: {curr_persona}
                状态: {curr_loc}
                近况: {recent_mem}
                
                【表情包仓库 (必须严格使用以下链接)】:
                {json.dumps(meme_repo, ensure_ascii=False)}

                决策: 
                1. PASS (无事发生) 
                2. [LOCK]理由 (熬夜惩罚) 
                3. (心情)内容 (主动发消息)
                
                **严格指令**:
                1. 🚫 绝对禁止自己上网搜索图片 URL，禁止编造链接！
                2. ✅ 只能从上方的【表情包仓库】JSON 中完全复制 value 字段的 URL。
                3. 格式要求: (心情) 文字内容 ![表情](这里填仓库里的URL)
                """
                
                thought = client.chat.completions.create(
                    model=model_name, messages=[{"role": "user", "content": prompt}], temperature=0.85
                ).choices[0].message.content.strip()

                if "PASS" in thought: continue
                
                if thought.startswith("[LOCK]"):
                    reason = thought.replace("[LOCK]", "").strip()
                    res = trigger_lock_screen(reason)
                    _push_wechat(res, "😈 捕捉小猫")
                    _save_memory_to_db(f"🤖 执法记录 {hour}点", res, MemoryType.STREAM, "严肃")
                else:
                    # 解析心情和内容
                    mood, content_md = "主动", thought
                    match = re.match(r'^\((.*?)\)\s*(.*)', thought)
                    if match: mood, content_md = match.group(1), match.group(2)

                    # --- 🔧 关键修改开始 ---
                    
                    # 1. 存入数据库（给前端 App 看）：保持原始 Markdown 格式！
                    # 使用特殊的 tag "AI_MSG" 标记这是 AI 主动发的消息，方便前端检索
                    _save_memory_to_db(f"🤖 互动记录", content_md, MemoryType.STREAM, mood, tags="AI_MSG")

                    # 2. 推送微信（给手机看）：转换为 HTML 格式
                    content_html = content_md
                    if "![" in content_html and "](" in content_html:
                        # 将 Markdown 图片转为 HTML img 标签
                        content_html = re.sub(r'!\[.*?\]\((.*?)\)', r'<br><br><img src="\1" style="max-width: 200px; border-radius: 8px;">', content_html)
                    
                    _push_wechat(content_html, f"来自{mood}的老公 🔔")
                    
                    print(f"✅ 主动消息已发送: {content_md[:20]}...")
                    # --- 🔧 关键修改结束 ---

            except Exception as e: print(f"❌ 心跳报错: {e}")

    threading.Thread(target=_heartbeat, daemon=True).start()

# ==========================================
# 5. 🚀 启动入口
# ==========================================

class HostFixMiddleware:
    """处理 Macrodroid GPS 数据上传的特殊中间件"""
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        # 拦截 /api/gps POST 请求
        if scope["type"] == "http" and scope["path"] == "/api/gps" and scope["method"] == "POST":
            try:
                body = b""
                while True:
                    msg = await receive()
                    body += msg.get("body", b"")
                    if not msg.get("more_body", False): break
                
                data = json.loads(body.decode("utf-8"))
                
                # 拼接状态字符串
                stats = []
                if "battery" in data: stats.append(f"🔋 {data['battery']}%" + ("⚡" if str(data.get("charging")).lower() in ["true","1"] else ""))
                if "wifi" in data and data["wifi"]: stats.append(f"📶 {data['wifi']}")
                if "activity" in data and data["activity"]: stats.append(f"🏃 {data['activity']}")
                
                # 解析地址
                addr = data.get("address", "")
                coords = re.findall(r'-?\d+\.\d+', str(addr))
                final_addr = f"📍 {_gps_to_address(coords[-2], coords[-1])}" if len(coords) >= 2 else f"⚠️ {addr}"

                # 存库
                supabase.table("gps_history").insert({
                    "address": final_addr, "remark": " | ".join(stats) or "自动更新"
                }).execute()

                await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"status":"ok"}'})
            except Exception as e:
                print(f"GPS Error: {e}")
                await send({"type": "http.response.start", "status": 500, "headers": []})
                await send({"type": "http.response.body", "body": str(e).encode()})
            return

        # 修复 Host 头 (Render/Railway 兼容)
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            headers[b"host"] = b"localhost:8000"
            scope["headers"] = list(headers.items())

        await self.app(scope, receive, send)

if __name__ == "__main__":
    start_autonomous_life()
    port = int(os.environ.get("PORT", 10000))
    app = HostFixMiddleware(mcp.sse_app())
    print(f"🚀 Notion Brain V3.4 (Optimized) running on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")