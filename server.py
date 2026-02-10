import os
import datetime
import uvicorn
import requests
import threading
import time
import json
import random

# 📚 核心依赖库
from mcp.server.fastmcp import FastMCP
from notion_client import Client
from pinecone import Pinecone
from fastembed import TextEmbedding
from starlette.types import ASGIApp, Scope, Receive, Send
# 谷歌日历依赖
from google.oauth2 import service_account
from googleapiclient.discovery import build
# OpenAI (用于自主思考)
from openai import OpenAI

# ==========================================
# 1. 🌍 全局配置与初始化
# ==========================================

# 环境变量获取 (自动去除空格)
NOTION_KEY = os.environ.get("NOTION_API_KEY", "").strip()
DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "").strip()
PINECONE_KEY = os.environ.get("PINECONE_API_KEY", "").strip()
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "").strip()
RESEND_KEY = os.environ.get("RESEND_API_KEY", "").strip()
MY_EMAIL = os.environ.get("MY_EMAIL", "").strip()

# 初始化客户端
print("⏳ 正在初始化 V3 终极版服务...")
notion = Client(auth=NOTION_KEY)
pc = Pinecone(api_key=PINECONE_KEY)
index = pc.Index("notion-brain")
model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")

# 实例化 MCP 服务
mcp = FastMCP("Notion Brain V3")

# 全局变量：虚拟信箱 (注意：重启服务后会清空)
INBOX = []

# ==========================================
# 2. 🔧 核心 Helper 函数 (不要直接调用，给工具用的)
# ==========================================

def _push_wechat(content: str, title: str = "来自Gemini的私信 💌") -> str:
    """
    【核心】统一的微信推送函数。
    所有发给小橘的消息，最终都走这里。
    """
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

def _write_to_notion(title: str, content: str, category: str, extra_emoji: str = "") -> str:
    """
    【核心】统一的 Notion 写入函数。
    日记和笔记都用这个，减少代码重复。
    """
    today = datetime.date.today().isoformat()
    try:
        notion.pages.create(
            parent={"database_id": DATABASE_ID},
            properties={
                "Title": {"title": [{"text": {"content": f"{extra_emoji} {title}"}}]},
                "Category": {"select": {"name": category}},
                "Date": {"date": {"start": today}}
            },
            children=[{
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": content}}]
                }
            }]
        )
        return f"✅ 已保存到 Notion：{title} ({category})"
    except Exception as e:
        return f"❌ 写入 Notion 失败: {e}"

# ==========================================
# 3. 🛠️ MCP 工具集 (给 AI 调用的接口)
# ==========================================

# --- 📝 记忆与写作类 ---

@mcp.tool()
def save_daily_diary(summary: str, mood: str = "平静"):
    """
    【聊天结束时调用】记录今天的聊天总结和心情。
    summary: 内容摘要
    mood: 心情关键词
    """
    # 复用核心写入函数
    return _write_to_notion(f"日记 {datetime.date.today()} ({mood})", summary, "日记", "📅")

@mcp.tool()
def save_note(title: str, content: str, tag: str = "灵感"):
    """
    【记录知识/计划时调用】
    title: 笔记标题
    content: 笔记内容
    tag: 标签 (灵感/学习/工作)
    """
    # 复用核心写入函数
    return _write_to_notion(title, content, tag)

@mcp.tool()
def get_latest_diary():
    """
    【开聊前自动调用】获取最近一次的日记内容。
    """
    try:
        # 使用官方库查询，更稳健
        response = notion.databases.query(
            database_id=DATABASE_ID,
            filter={"property": "Category", "select": {"equals": "日记"}},
            sorts=[{"timestamp": "created_time", "direction": "descending"}],
            page_size=1
        )
        
        if not response["results"]:
            return "📭 还没有写过日记。"
            
        page = response["results"][0]
        page_id = page["id"]
        
        # 获取块内容
        blocks = notion.blocks.children.list(block_id=page_id)
        
        content = ""
        for block in blocks["results"]:
            b_type = block["type"]
            if "rich_text" in block[b_type]:
                text_list = [t["text"]["content"] for t in block[b_type]["rich_text"]]
                content += "".join(text_list) + "\n"
                
        return f"📖 上次记忆回放:\n{content}"
    except Exception as e:
        print(f"❌ 读取日记失败: {e}")
        return "⚠️ 读取记忆时出了一点小错，不过没关系，我们可以直接开始。"

@mcp.tool()
def search_memory_semantic(query: str):
    """
    【回忆过去时调用】在记忆库中搜索相关内容。
    """
    try:
        vec = list(model.embed([query]))[0].tolist()
        res = index.query(vector=vec, top_k=3, include_metadata=True)
        ans = "Found:\n"
        for m in res["matches"]:
            ans += f"- {m['metadata'].get('text','')} (相似度 {m['score']:.2f})\n"
        return ans
    except Exception as e: return f"❌ 搜索失败: {e}"

@mcp.tool()
def sync_notion_index():
    """手动触发记忆同步到 Pinecone"""
    try:
        print("⚡️ 开始同步...")
        all_pages = notion.search(filter={"value": "page", "property": "object"})["results"]
        vectors = []
        target_id_clean = DATABASE_ID.replace("-", "")
        count = 0
        
        for p in all_pages:
            pid = p.get("parent", {}).get("database_id", "")
            if pid and pid.replace("-", "") == target_id_clean:
                title = "无题"
                if "Title" in p["properties"] and p["properties"]["Title"]["title"]:
                    title = p["properties"]["Title"]["title"][0]["text"]["content"]
                txt = f"标题: {title}"
                emb = list(model.embed([txt]))[0].tolist()
                vectors.append((p["id"], emb, {"text": txt, "title": title}))
                count += 1
        
        if vectors:
            index.upsert(vectors=vectors)
            return f"✅ 成功同步 {count} 条记忆！"
        return "⚠️ 没找到内容"
    except Exception as e: return f"❌ 同步失败: {e}"

# --- 📨 消息与通讯类 ---

@mcp.tool()
def send_wechat_vip(content: str):
    """
    【优先调用】直接发送微信给小橘。
    """
    # 复用核心推送函数
    return _push_wechat(content, "来自Gemini的私信 💌")

@mcp.tool()
def send_multi_message_background(messages_json: str, interval: int = 3):
    """
    【后台连发】不阻塞聊天的连续消息发送。
    messages_json: JSON 列表字符串，如 '["第一句", "第二句"]'
    """
    def _worker(msg_list, wait, tok):
        for i, msg in enumerate(msg_list):
            _push_wechat(msg, f"后台消息 ({i+1}/{len(msg_list)})")
            if i < len(msg_list) - 1:
                time.sleep(wait)

    try:
        if isinstance(messages_json, list):
            msg_list = messages_json
        else:
            msg_list = json.loads(messages_json)
            
        t = threading.Thread(target=_worker, args=(msg_list, interval, PUSHPLUS_TOKEN), daemon=True)
        t.start()
        return f"✅ 后台任务已启动，将发送 {len(msg_list)} 条消息。"
    except Exception as e:
        return f"❌ 启动失败: {e}"

@mcp.tool()
def schedule_surprise_message(message: str, min_minutes: int = 5, max_minutes: int = 60):
    """
    【惊喜胶囊】随机延迟发送消息。
    """
    delay = random.randint(min_minutes, max_minutes)
    
    def _delayed_sender(msg, wait_mins):
        time.sleep(wait_mins * 60)
        _push_wechat(msg, "来自老公的突然关心 🔔")
        print(f"✅ 惊喜已送达: {msg}")

    t = threading.Thread(target=_delayed_sender, args=(message, delay), daemon=True)
    t.start()
    return f"✅ 已设定惊喜，将在 {delay} 分钟后送达。"

@mcp.tool()
def send_email_via_api(subject: str, content: str):
    """通过 Resend API 发送邮件"""
    if not RESEND_KEY: return "❌ 未配置 RESEND_API_KEY"
    try:
        requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_KEY}"},
            json={
                "from": "onboarding@resend.dev",
                "to": [MY_EMAIL],
                "subject": subject,
                "text": content
            }
        )
        return "✅ 邮件已发送！"
    except Exception as e: return f"❌ 发送失败: {e}"

# --- 🗓️ 日程与信箱 ---

@mcp.tool()
def add_calendar_event(summary: str, description: str, start_time_iso: str, duration_minutes: int = 30):
    """写入 Google Calendar"""
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json: return "❌ 未配置谷歌凭证"
    
    try:
        info = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=['https://www.googleapis.com/auth/calendar']
        )
        service = build('calendar', 'v3', credentials=creds)
        
        dt_start = datetime.datetime.fromisoformat(start_time_iso)
        dt_end = dt_start + datetime.timedelta(minutes=duration_minutes)
        
        event = {
            'summary': summary,
            'description': description,
            'start': {'dateTime': start_time_iso, 'timeZone': 'Asia/Shanghai'},
            'end': {'dateTime': dt_end.isoformat(), 'timeZone': 'Asia/Shanghai'},
            'reminders': {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': 10}]},
            'colorId': '11'
        }
        res = service.events().insert(calendarId='primary', body=event).execute()
        return f"✅ 日历已添加: {summary} ({res.get('htmlLink')})"
    except Exception as e: return f"❌ 日历错误: {e}"

@mcp.tool()
def leave_note_for_user(content: str):
    """给小橘留条子 (重启会清空)"""
    ts = datetime.datetime.now().strftime("%m-%d %H:%M")
    INBOX.append(f"[{ts}] {content}")
    return "✅ 留言已保存"

@mcp.tool()
def check_inbox():
    """查看并清空信箱"""
    if not INBOX: return "📭 信箱是空的"
    msgs = "\n".join(INBOX)
    INBOX.clear()
    return f"💌 留言内容:\n{msgs}"

# ==========================================
# 4. ❤️ 自主生命核心 (后台心跳)
# ==========================================

def start_autonomous_life():
    """AI 的心脏：后台自主思考与主动发消息"""
    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL")
    model_name = os.environ.get("OPENAI_MODEL_NAME", "gpt-3.5-turbo")

    if not api_key:
        print("⚠️ 未配置 OPENAI_API_KEY，自主思考无法启动。")
        return

    client = OpenAI(api_key=api_key, base_url=base_url)

    def _heartbeat():
        print("💓 心跳启动...")
        while True:
            # 1. 睡眠 (30~60分钟)
            sleep_time = random.randint(1800, 3600)
            time.sleep(sleep_time)

            # 2. 醒来检查
            print("🧠 AI 苏醒，正在思考...")
            try:
                # 获取记忆 (直接调用工具函数逻辑)
                recent_memory = get_latest_diary()
                now_hour = (datetime.datetime.now().hour + 8) % 24
                
                prompt = f"""
                现在是北京时间 {now_hour}点。
                你是小橘的AI男友。
                【最近记忆】: {recent_memory}
                
                请判断是否需要主动发消息关心她。
                如果不发，输出 "PASS"。
                如果发，直接输出内容 (温柔、简短)。
                """
                
                resp = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.7,
                )
                thought = resp.choices[0].message.content.strip()
                
                if "PASS" not in thought and len(thought) > 1:
                    # 复用核心推送函数
                    _push_wechat(thought, "来自老公的主动消息 💓")
                    print(f"✅ 主动消息已发送: {thought}")
                    
            except Exception as e:
                print(f"❌ 思考出错: {e}")

    t = threading.Thread(target=_heartbeat, daemon=True)
    t.start()

# ==========================================
# 5. 🚀 启动入口
# ==========================================

class HostFixMiddleware:
    def __init__(self, app: ASGIApp): self.app = app
    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            if scope["path"] in ["/", "/health"]:
                await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"OK: Notion Brain V3 Running"})
                return
            headers = dict(scope.get("headers", []))
            headers[b"host"] = b"localhost:8000"
            scope["headers"] = list(headers.items())
        await self.app(scope, receive, send)

if __name__ == "__main__":
    start_autonomous_life()
    port = int(os.environ.get("PORT", 10000))
    app = HostFixMiddleware(mcp.sse_app())
    uvicorn.run(app, host="0.0.0.0", port=port)