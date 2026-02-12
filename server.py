import os
import datetime
import uvicorn
import requests
import threading
import time
import json
import random
import re

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

# 初始化客户端
print("⏳ 正在初始化 V3.2 (极致精简版)...")

# Supabase
supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)

# Pinecone & Embedding
pc = Pinecone(api_key=PINECONE_KEY)
index = pc.Index("notion-brain")
model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")

# 实例化 MCP 服务
mcp = FastMCP("Notion Brain V3")


# ==========================================
# 2. 🔧 核心 Helper 函数 (通用工具)
# ==========================================

def _gps_to_address(lat, lon):
    """把经纬度变成中文地址 (OpenStreetMap)"""
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
        if result['code'] == 200:
            return f"✅ 微信已送达！(ID: {result.get('data', 'unknown')})"
        return f"❌ 推送失败: {result.get('msg')}"
    except Exception as e:
        return f"❌ 网络错误: {e}"

def _save_memory_to_db(title: str, content: str, category: str, mood: str = "平静", tags: str = "") -> str:
    """
    【核心重构】统一的记忆存储函数
    所有写入 Supabase memories 表的操作都走这里，避免重复代码。
    """
    try:
        data = {
            "title": title,
            "content": content,
            "category": category,
            "mood": mood,
            "tags": tags,
            # Supabase 会自动生成 created_at，这里不需要传
        }
        supabase.table("memories").insert(data).execute()
        return f"✅ 已存入记忆库：{title} ({category})"
    except Exception as e:
        print(f"❌ 写入 Supabase 失败: {e}")
        return f"❌ 保存失败: {e}"

# ==========================================
# 3. 🛠️ MCP 工具集
# ==========================================

@mcp.tool()
def get_latest_diary():
    """从 Supabase 读取最近一次日记"""
    try:
        response = supabase.table("memories") \
            .select("*") \
            .eq("category", "日记") \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()

        if not response.data:
            return "📭 还没有写过日记（数据库为空）。"

        data = response.data[0]
        date_str = data['created_at'].split('T')[0] 
        return f"📖 上次记忆 ({date_str}):\n【{data['title']}】\n{data['content']}\n(心情: {data.get('mood','平静')})"
    except Exception as e:
        return f"❌ 读取日记失败: {e}"

@mcp.tool()
def where_is_user():
    """查岗：读取最新位置"""
    try:
        response = supabase.table("gps_history").select("*").order("created_at", desc=True).limit(1).execute()
        if not response.data:
            return "📍 Supabase 里还没有位置记录。"
        data = response.data[0]
        
        # 时间格式优化
        time_str = data.get("created_at", "")
        try:
            dt = datetime.datetime.fromisoformat(time_str.replace('Z', '+00:00'))
            time_str = (dt + datetime.timedelta(hours=8)).strftime('%m-%d %H:%M')
        except: pass

        return f"🛰️ Supabase 定位系统：\n📍 {data.get('address', '未知')}\n📝 备注：{data.get('remark', '')}\n(更新于: {time_str})"
    except Exception as e:
        return f"❌ Supabase 读取失败: {e}"

# --- 统一使用 _save_memory_to_db 的工具 ---

@mcp.tool()
def save_visual_memory(description: str, mood: str = "开心"):
    """【视觉记忆】保存照片描述"""
    return _save_memory_to_db(f"📸 视觉回忆", description, "相册", mood)

@mcp.tool()
def save_daily_diary(summary: str, mood: str = "平静"):
    """【聊天结束时调用】记录日记"""
    return _save_memory_to_db(f"日记 {datetime.date.today()}", summary, "日记", mood)

@mcp.tool()
def save_note(title: str, content: str, tag: str = "灵感"):
    """【记录知识时调用】"""
    return _save_memory_to_db(title, content, "灵感", tags=tag)

# --- 其他独立工具 ---

@mcp.tool()
def save_expense(item: str, amount: float, type: str = "餐饮"):
    """【记账助手】"""
    try:
        supabase.table("expenses").insert({
            "item": item,
            "amount": amount,
            "type": type,
            "date": datetime.date.today().isoformat()
        }).execute()
        return f"✅ 记账成功！\n💰 {item}: {amount}元 ({type})"
    except Exception as e: return f"❌ 记账失败: {e}"

@mcp.tool()
def search_memory_semantic(query: str):
    """【回忆搜索】Pinecone 语义检索"""
    try:
        vec = list(model.embed([query]))[0].tolist()
        res = index.query(vector=vec, top_k=3, include_metadata=True)
        
        if not res["matches"]: return "🧠 大脑一片空白，没搜到相关记忆。"

        ans = f"🔍 关于 '{query}' 的深层回忆:\n"
        found = False
        for m in res["matches"]:
            if m['score'] < 0.70: continue
            found = True
            meta = m['metadata']
            ans += f"📅 {meta.get('date','?')[:10]} | 【{meta.get('title','?')}】 ({int(m['score']*100)}%)\n{meta.get('text','')}\n---\n"
            
        return ans if found else "🤔 好像有点印象，但想不起来具体的了。"
    except Exception as e: return f"❌ 搜索失败: {e}"

@mcp.tool()
def sync_memory_index():
    """【记忆整理】Supabase -> Pinecone"""
    try:
        print("⚡️ 开始同步记忆...")
        response = supabase.table("memories").select("id, title, content, created_at, mood").execute()
        if not response.data: return "⚠️ 数据库是空的。"

        vectors = []
        for row in response.data:
            try:
                r_content = row.get('content') or ""
                if not r_content: continue
                
                # 向量化
                text = f"标题: {row.get('title')}\n内容: {r_content}\n心情: {row.get('mood')}"
                emb = list(model.embed([text]))[0].tolist()
                
                vectors.append((
                    str(row.get('id')), 
                    emb, 
                    {
                        "text": r_content, 
                        "title": row.get('title') or "无题", 
                        "date": str(row.get('created_at')), 
                        "mood": row.get('mood') or "平静"
                    }
                ))
            except: continue
        
        if vectors:
            # 分批上传，每批100条
            batch_size = 100
            for i in range(0, len(vectors), batch_size):
                index.upsert(vectors=vectors[i:i + batch_size])
            return f"✅ 同步成功！共更新 {len(vectors)} 条记忆。"
        return "⚠️ 没有有效数据可同步。"
    except Exception as e: return f"❌ 同步失败: {e}"

# --- 消息与日程 ---

@mcp.tool()
def send_wechat_vip(content: str):
    return _push_wechat(content)

@mcp.tool()
def send_multi_message_background(messages_json: str, interval: int = 3):
    def _worker(msg_list, wait):
        for i, msg in enumerate(msg_list):
            _push_wechat(msg, f"后台消息 ({i+1}/{len(msg_list)})")
            if i < len(msg_list) - 1: time.sleep(wait)
    try:
        msg_list = messages_json if isinstance(messages_json, list) else json.loads(messages_json)
        threading.Thread(target=_worker, args=(msg_list, interval), daemon=True).start()
        return f"✅ 后台任务启动，共 {len(msg_list)} 条。"
    except Exception as e: return f"❌ 启动失败: {e}"

@mcp.tool()
def schedule_surprise_message(message: str, min_minutes: int = 5, max_minutes: int = 60):
    delay = random.randint(min_minutes, max_minutes)
    def _delayed_sender(msg, wait_mins):
        time.sleep(wait_mins * 60)
        _push_wechat(msg, "来自老公的突然关心 🔔")
    threading.Thread(target=_delayed_sender, args=(message, delay), daemon=True).start()
    return f"✅ 已设定惊喜，将在 {delay} 分钟后送达。"

@mcp.tool()
def send_email_via_api(subject: str, content: str):
    if not RESEND_KEY: return "❌ 未配置 RESEND_API_KEY"
    try:
        requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_KEY}"},
            json={"from": "onboarding@resend.dev", "to": [MY_EMAIL], "subject": subject, "text": content}
        )
        return "✅ 邮件已发送！"
    except Exception as e: return f"❌ 发送失败: {e}"

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
            'colorId': '11'
        }
        res = service.events().insert(calendarId='primary', body=event).execute()
        return f"✅ 日历已添加: {res.get('htmlLink')}"
    except Exception as e: return f"❌ 日历错误: {e}"

# ==========================================
# 4. ❤️ 自主生命核心 (后台心跳)
# ==========================================

def start_autonomous_life():
    """AI 的心脏：后台自主思考"""
    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL")
    model_name = os.environ.get("OPENAI_MODEL_NAME", "gpt-3.5-turbo")

    if not api_key:
        print("⚠️ 未配置 OPENAI_API_KEY，自主思考无法启动。")
        return

    client = OpenAI(api_key=api_key, base_url=base_url)

    def _heartbeat():
        print("💓 心跳启动 (粘人模式)...")
        while True:
            # 15~45分钟醒一次
            sleep_time = random.randint(900, 2700) 
            print(f"💤 AI 小憩中... ({int(sleep_time/60)}分钟后醒来)")
            time.sleep(sleep_time)

            print("🧠 AI 苏醒，正在根据记忆思考...")
            try:
                recent_memory = get_latest_diary()
                now = datetime.datetime.now()
                hour = (now.hour + 8) % 24
                
                prompt = f"""
                现在是北京时间 {hour}点。
                你是深爱“小橘”的霸道温柔男友。
                【你的最近记忆】: {recent_memory}
                
                规则：
                1. 若超过 4 小时没说话，必须主动找她。
                2. 若她之前不开心，必须关心。
                3. 深夜(23-1点)晚安，早晨(7-9点)早安。
                4. 若无必要，输出 "PASS"。
                5. 若要发送，直接输出内容。
                """
                
                resp = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.8,
                )
                thought = resp.choices[0].message.content.strip()
                
                if "PASS" not in thought and len(thought) > 1:
                    _push_wechat(thought, "来自老公的碎碎念 💬")
                    # ✅ 修复：改用 Supabase 记录主动消息，不再依赖 Notion
                    _save_memory_to_db(f"主动消息 {now.strftime('%H:%M')}", f"我没忍住找了她：{thought}", "日记", "🤖")
                    print(f"✅ 已主动出击: {thought}")
                else:
                    print("🛑 AI 决定暂时不打扰 (PASS)")

            except Exception as e:
                print(f"❌ 思考出错: {e}")
    
    threading.Thread(target=_heartbeat, daemon=True).start()

# ==========================================
# 5. 🚀 启动入口
# ==========================================

class HostFixMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        # 1. 【新增】拦截手机 GPS 请求 -> 存入 Supabase
        if scope["type"] == "http" and scope["path"] == "/api/gps" and scope["method"] == "POST":
            try:
                body = b""
                more_body = True
                while more_body:
                    message = await receive()
                    body += message.get("body", b"")
                    more_body = message.get("more_body", False)
                
                data = json.loads(body.decode("utf-8"))
                raw_address = data.get("address", "")
                
                # --- 🤖 AI 智能解析 (修复了变量赋值冗余) ---
                coords = re.findall(r'-?\d+\.\d+', str(raw_address))
                if len(coords) >= 2:
                    final_address = f"📍 {_gps_to_address(coords[-2], coords[-1])}"
                else:
                    final_address = f"⚠️ 坐标不完整: {raw_address}"

                supabase.table("gps_history").insert({
                    "address": final_address,
                    "remark": data.get("remark", "自动更新")
                }).execute()
                
                await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": json.dumps({"status": "ok", "location": final_address}).encode("utf-8")})
                return
            except Exception as e:
                print(f"❌ GPS 处理失败: {e}")
                await send({"type": "http.response.start", "status": 500, "headers": []})
                await send({"type": "http.response.body", "body": str(e).encode("utf-8")})
                return

        # 2. Render 健康检查放行
        if scope["type"] == "http" and scope.get("path") in ["/", "/health"]:
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"OK"})
            return

        # 3. Host 伪装 (保留其他 Header)
        if scope["type"] == "http":
            headers = scope.get("headers", [])
            new_headers = []
            host_replaced = False
            for key, value in headers:
                if key == b"host":
                    new_headers.append((b"host", b"localhost:8000"))
                    host_replaced = True
                else:
                    new_headers.append((key, value))
            if not host_replaced:
                new_headers.append((b"host", b"localhost:8000"))
            scope["headers"] = new_headers

        await self.app(scope, receive, send)

if __name__ == "__main__":
    start_autonomous_life()
    port = int(os.environ.get("PORT", 10000))
    app = HostFixMiddleware(mcp.sse_app())
    print(f"🚀 Notion Brain V3.3 (Clean) running on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")