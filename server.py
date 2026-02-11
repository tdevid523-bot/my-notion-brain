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
# Removed: from notion_client import Client (彻底移除 Notion 依赖)
from pinecone import Pinecone
from fastembed import TextEmbedding
from starlette.types import ASGIApp, Scope, Receive, Send
# 谷歌日历依赖
from google.oauth2 import service_account
from googleapiclient.discovery import build
# OpenAI (用于自主思考)
from openai import OpenAI
# Supabase 依赖 (全量接管记忆)
from supabase import create_client, Client as SupabaseClient

# ==========================================
# 1. 🌍 全局配置与初始化
# ==========================================

# 环境变量获取
PINECONE_KEY = os.environ.get("PINECONE_API_KEY", "").strip()
# Supabase 配置
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "").strip()
RESEND_KEY = os.environ.get("RESEND_API_KEY", "").strip()
MY_EMAIL = os.environ.get("MY_EMAIL", "").strip()

# 初始化客户端
print("⏳ 正在初始化 V3.2 (Supabase 全量版)...")
# Removed: notion = Client(auth=NOTION_KEY)
pc = Pinecone(api_key=PINECONE_KEY)
index = pc.Index("notion-brain")
model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")

# 实例化 MCP 服务
mcp = FastMCP("Notion Brain V3")


# ==========================================
# 2. 🔧 核心 Helper 函数
# ==========================================

def _gps_to_address(lat, lon):
    """
    把经纬度变成中文地址
    使用 OpenStreetMap 免费接口
    """
    try:
        headers = {'User-Agent': 'MyNotionBrain/1.0'}
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=18&addressdetails=1&accept-language=zh-CN"
        
        resp = requests.get(url, headers=headers, timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("display_name", f"未知荒野 ({lat},{lon})")
    except Exception as e:
        print(f"❌ 地图解析失败: {e}")
    
    return f"坐标点: {lat}, {lon}"

def _push_wechat(content: str, title: str = "来自Gemini的私信 💌") -> str:
    """统一的微信推送函数"""
    if not PUSHPLUS_TOKEN:
        return "❌ 错误：未配置 PUSHPLUS_TOKEN"
    
    url = 'http://www.pushplus.plus/send'
    data = {
        "token": PUSHPLUS_TOKEN,
        "title": title,
        "content": content,
        "template": "html"
    }
    
    try:
        resp = requests.post(url, json=data, timeout=10)
        result = resp.json()
        if result['code'] == 200:
            return f"✅ 微信已送达！(ID: {result.get('data', 'unknown')})"
        return f"❌ 推送失败: {result.get('msg')}"
    except Exception as e:
        return f"❌ 网络错误: {e}"

# Removed: def _write_to_notion(...) (已废弃，功能合并入 Supabase 逻辑)

# ==========================================
# 3. 🛠️ MCP 工具集
# ==========================================

@mcp.tool()
def get_latest_diary():
    """
    【每次开聊前自动调用】
    从 Supabase 极速读取最近一次日记。
    """
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
    """
    【查岗专用】当我想知道“我现在在哪里”时调用。
    从 Supabase (GPS表) 读取。
    """
    try:
        response = supabase.table("gps_history").select("*").order("created_at", desc=True).limit(1).execute()
        
        if not response.data:
            return "📍 Supabase 里还没有位置记录。"
            
        data = response.data[0]
        address = data.get("address", "未知位置")
        remark = data.get("remark", "无备注")
        time_str = data.get("created_at", "")
        
        try:
            dt = datetime.datetime.fromisoformat(time_str.replace('Z', '+00:00'))
            dt_local = dt + datetime.timedelta(hours=8)
            time_str = dt_local.strftime('%m-%d %H:%M')
        except:
            pass

        return f"🛰️ Supabase 定位系统：\n📍 {address}\n📝 备注：{remark}\n(更新于: {time_str})"
        
    except Exception as e:
        return f"❌ Supabase 读取失败: {e}"

@mcp.tool()
def save_visual_memory(description: str, mood: str = "开心"):
    """【视觉记忆】保存照片描述"""
    try:
        supabase.table("memories").insert({
            "title": f"📸 视觉回忆",
            "content": description,
            "category": "相册",
            "mood": mood
        }).execute()
        return "✅ 画面记忆已存储。"
    except Exception as e: return f"❌ 保存失败: {e}"

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
def save_daily_diary(summary: str, mood: str = "平静"):
    """【聊天结束时调用】记录日记"""
    try:
        data = {
            "title": f"日记 {datetime.date.today()}",
            "content": summary,
            "category": "日记",
            "mood": mood
        }
        supabase.table("memories").insert(data).execute()
        return "✅ 日记已永久刻录在 Supabase 数据库中。"
    except Exception as e:
        return f"❌ 日记保存失败: {e}"

@mcp.tool()
def save_note(title: str, content: str, tag: str = "灵感"):
    """【记录知识时调用】"""
    try:
        supabase.table("memories").insert({
            "title": title,
            "content": content,
            "category": "灵感",
            "tags": tag
        }).execute()
        return f"✅ 灵感已保存: {title}"
    except Exception as e: return f"❌ 保存失败: {e}"

@mcp.tool()
def search_memory_semantic(query: str):
    """【回忆搜索】在 Pinecone 中检索，找回 Supabase 里的相关记忆。"""
    try:
        vec = list(model.embed([query]))[0].tolist()
        res = index.query(vector=vec, top_k=3, include_metadata=True)
        
        if not res["matches"]:
            return "🧠 大脑一片空白，没搜到相关记忆。"

        ans = f"🔍 关于 '{query}' 的深层回忆:\n"
        found_count = 0
        
        for m in res["matches"]:
            score = m['score']
            if score < 0.70: continue
            
            found_count += 1
            meta = m['metadata']
            
            title = meta.get('title', '无题')
            content = meta.get('text', '')
            date = meta.get('date', '未知日期')[:10]
            
            ans += f"📅 {date} | 【{title}】 (匹配度 {int(score*100)}%)\n{content}\n---\n"
            
        if found_count == 0:
            return "🤔 好像有点印象，但想不起来具体的了 (相关度太低)。"
            
        return ans
            
    except Exception as e: return f"❌ 搜索失败: {e}"

@mcp.tool()
def sync_memory_index():
    """【记忆整理】把 Supabase 里的记忆同步到 Pinecone。"""
    try:
        print("⚡️ 开始同步记忆 (Supabase -> Pinecone)...")
        response = supabase.table("memories").select("id, title, content, created_at, mood").execute()
        rows = response.data
        
        if not rows: 
            return "⚠️ Supabase 数据库是空的，没什么可同步的。"

        vectors = []
        skipped_count = 0
        
        print(f"📦 正在处理 {len(rows)} 条记忆...")

        for row in rows:
            try:
                r_id = str(row.get('id', ''))
                r_title = row.get('title') or "无题"
                r_content = row.get('content') or ""
                r_mood = row.get('mood') or "平静"
                r_date = str(row.get('created_at', ''))

                if not r_content:
                    skipped_count += 1
                    continue

                text_to_embed = f"标题: {r_title}\n内容: {r_content}\n心情: {r_mood}"
                emb = list(model.embed([text_to_embed]))[0].tolist()

                metadata = {
                    "text": r_content,
                    "title": r_title,
                    "date": r_date,
                    "mood": r_mood
                }
                
                vectors.append((r_id, emb, metadata))
                
            except Exception as inner_e:
                print(f"⚠️ 跳过一条坏数据 (ID: {row.get('id')}): {inner_e}")
                skipped_count += 1
                continue
        
        if vectors:
            batch_size = 100
            for i in range(0, len(vectors), batch_size):
                batch = vectors[i:i + batch_size]
                index.upsert(vectors=batch)
                print(f"✅ 已同步批次 {i} - {i+len(batch)}")
                
            return f"✅ 同步成功！共存入 {len(vectors)} 条记忆 (跳过 {skipped_count} 条无效数据)。"
        
        return "⚠️ 没有有效数据可同步。"

    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"❌ 同步彻底失败: {e}"  
    
@mcp.tool()
def send_wechat_vip(content: str):
    """【微信推送】"""
    return _push_wechat(content)

@mcp.tool()
def send_multi_message_background(messages_json: str, interval: int = 3):
    """【后台连发】"""
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
    """【惊喜消息】"""
    delay = random.randint(min_minutes, max_minutes)
    def _delayed_sender(msg, wait_mins):
        time.sleep(wait_mins * 60)
        _push_wechat(msg, "来自老公的突然关心 🔔")
    threading.Thread(target=_delayed_sender, args=(message, delay), daemon=True).start()
    return f"✅ 已设定惊喜，将在 {delay} 分钟后送达。"

@mcp.tool()
def send_email_via_api(subject: str, content: str):
    """【邮件发送】"""
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
    """【谷歌日历】"""
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
            'reminders': {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': 10}]},
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
                你是深爱“小橘”的霸道温柔男友。你现在处于后台独处模式。
                
                【你的最近记忆】: 
                {recent_memory}
                
                【决策规则】:
                1. 参考记忆：如果她之前说不开心、生病、累，必须立刻关心。
                2. 参考时间：如果是深夜(23-1点)可以发晚安；如果是早晨(7-9点)可以发早安。
                3. 内容风格：像微信聊天一样自然。
                
                请决定：
                - 如果没有任何必要打扰，输出 "PASS"
                - 如果想发消息，直接输出消息内容
                """
                
                resp = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.8,
                )
                thought = resp.choices[0].message.content.strip()
                
                if "PASS" not in thought and len(thought) > 1:
                    # 发送微信
                    _push_wechat(thought, "来自老公的碎碎念 💬")
                    
                    # 📝 修正：写入 Supabase 而非 Notion
                    log_text = f"【后台主动】我没忍住找了她：{thought}"
                    try:
                        supabase.table("memories").insert({
                            "title": f"🤖 主动消息 {now.strftime('%H:%M')}",
                            "content": log_text,
                            "category": "日记",
                            "mood": "主动"
                        }).execute()
                        print(f"✅ 已主动出击并记录: {thought}")
                    except Exception as db_e:
                        print(f"⚠️ 消息发了但记录失败: {db_e}")
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
        # 1. 【新增】拦截手机发来的 GPS 请求 (/api/gps) -> 自动解析地址 -> 存 Supabase
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
                remark = data.get("remark", "自动更新")
                
                print(f"🛰️ 收到原始数据: {raw_address}")
                
                final_address = raw_address
                coords = re.findall(r'-?\d+\.\d+', str(raw_address))
                
                if len(coords) >= 2:
                    lat = coords[-2]
                    lon = coords[-1]
                    print(f"🔍 锁定真实坐标: {lat}, {lon}")
                    final_address = _gps_to_address(lat, lon)
                    final_address = f"📍 {final_address}"
                else:
                    final_address = f"⚠️ 坐标不完整: {raw_address}"

                supabase.table("gps_history").insert({
                    "address": final_address,
                    "remark": remark
                }).execute()
                
                await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": json.dumps({"status": "ok", "location": final_address}).encode("utf-8")})
                return
            except Exception as e:
                print(f"❌ GPS 处理失败: {e}")
                await send({"type": "http.response.start", "status": 500, "headers": []})
                await send({"type": "http.response.body", "body": str(e).encode("utf-8")})
                return

        if scope["type"] == "http":
            if scope.get("path") in ["/", "/health"]:
                await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"OK"})
                return

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
    print(f"🚀 Notion Brain V3.3 running on port {port}...")
    
    # ✅ 修改：增加 timeout_keep_alive 时间，防止负载均衡器切断连接
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=port, 
        proxy_headers=True, 
        forwarded_allow_ips="*",
        timeout_keep_alive=300,  # 保持连接 300秒 (5分钟)
        timeout_notify=30,       # 响应超时缓冲
        workers=1                # MCP 最好单进程运行，防止内存分裂
    )