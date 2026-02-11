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
from notion_client import Client
from pinecone import Pinecone
from fastembed import TextEmbedding
from starlette.types import ASGIApp, Scope, Receive, Send
# 谷歌日历依赖
from google.oauth2 import service_account
from googleapiclient.discovery import build
# OpenAI (用于自主思考)
from openai import OpenAI
# Supabase 依赖 (新增)
from supabase import create_client, Client as SupabaseClient

# ==========================================
# 1. 🌍 全局配置与初始化
# ==========================================

# 环境变量获取 (自动去除空格)
NOTION_KEY = os.environ.get("NOTION_API_KEY", "").strip()
DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "").strip()
PINECONE_KEY = os.environ.get("PINECONE_API_KEY", "").strip()
# Supabase 配置 (新增)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "").strip()
RESEND_KEY = os.environ.get("RESEND_API_KEY", "").strip()
MY_EMAIL = os.environ.get("MY_EMAIL", "").strip()

# 初始化客户端
print("⏳ 正在初始化 V3.1 (原生记忆读取版)...")
notion = Client(auth=NOTION_KEY)
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
    【新增】把经纬度变成中文地址
    使用 OpenStreetMap 免费接口，无需 Key
    """
    try:
        # 伪装个 User-Agent 防止被拦截
        headers = {'User-Agent': 'MyNotionBrain/1.0'}
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=18&addressdetails=1&accept-language=zh-CN"
        
        # 请求接口 (设置3秒超时，防止卡住)
        resp = requests.get(url, headers=headers, timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            # 获取最详细的显示名称
            return data.get("display_name", f"未知荒野 ({lat},{lon})")
    except Exception as e:
        print(f"❌ 地图解析失败: {e}")
    
    # 如果失败了，就这就返回原始坐标
    return f"坐标点: {lat}, {lon}"

# ==========================================
# 2. 🔧 核心 Helper 函数 (给工具用的)
# ==========================================

def _push_wechat(content: str, title: str = "来自Gemini的私信 💌") -> str:
    """【核心】统一的微信推送函数"""
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
    【核心】统一的 Notion 写入函数 (增强版)。
    自动处理超过2000字的长文本，防止报错断连。
    """
    today = datetime.date.today().isoformat()
    
    # 1. 安全检查：防止标签为空导致报错
    if not category: category = "灵感"
    
    # 2. 核心修复：Notion限制每个块最多2000字，必须切片
    # 如果 content 太长，我们把它切成多个段落块
    children_blocks = []
    chunk_size = 2000
    
    if len(content) > chunk_size:
        # 切片逻辑
        for i in range(0, len(content), chunk_size):
            chunk = content[i:i + chunk_size]
            children_blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]}
            })
    else:
        # 短文本直接放
        children_blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": content}}]}
        })

    try:
        notion.pages.create(
            parent={"database_id": DATABASE_ID},
            properties={
                "Title": {"title": [{"text": {"content": f"{extra_emoji} {title}"}}]},
                "Category": {"select": {"name": category}},
                "Date": {"date": {"start": today}}
            },
            children=children_blocks
        )
        return f"✅ 已保存到 Notion：{title} ({category})"
    except Exception as e:
        print(f"❌ Notion 写入报错: {e}") # 打印日志方便调试
        return f"❌ 写入失败 (请检查Notion标签是否允许创建): {e}"

# ==========================================
# 3. 🛠️ MCP 工具集
# ==========================================

# --- 🔙 关键修改：换回原来的原生读取代码 ---
@mcp.tool()
def get_latest_diary():
    """
    【每次开聊前自动调用】
    从 Supabase 极速读取最近一次日记。
    """
    try:
        # 读取 memories 表，分类是"日记"，按时间倒序，只取 1 条
        response = supabase.table("memories") \
            .select("*") \
            .eq("category", "日记") \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()

        if not response.data:
            return "📭 还没有写过日记（数据库为空）。"

        data = response.data[0]
        # 格式化时间
        date_str = data['created_at'].split('T')[0] 
        
        return f"📖 上次记忆 ({date_str}):\n【{data['title']}】\n{data['content']}\n(心情: {data.get('mood','平静')})"

    except Exception as e:
        return f"❌ 读取日记失败: {e}"

# --- 📍 新增：专门读取最新位置 ---
@mcp.tool()
def where_is_user():
    """
    【查岗专用】当我想知道“我现在在哪里”时调用。
    改为从 Supabase (GPS表) 读取，速度更快且稳定。
    """
    try:
        # 假设你的 Supabase 表名叫 'gps_history' (如果不同请修改此处)
        # 读取最新的一条记录
        response = supabase.table("gps_history").select("*").order("created_at", desc=True).limit(1).execute()
        
        if not response.data:
            return "📍 Supabase 里还没有位置记录。"
            
        data = response.data[0]
        # 假设字段名为 address(地址) 和 remark(备注)
        address = data.get("address", "未知位置")
        remark = data.get("remark", "无备注")
        time_str = data.get("created_at", "")
        
        # 转换为更友好的时间格式 (可选)
        try:
            dt = datetime.datetime.fromisoformat(time_str.replace('Z', '+00:00'))
            dt_local = dt + datetime.timedelta(hours=8) # 转东八区
            time_str = dt_local.strftime('%m-%d %H:%M')
        except:
            pass # 如果转换失败就用原格式

        return f"🛰️ Supabase 定位系统：\n📍 {address}\n📝 备注：{remark}\n(更新于: {time_str})"
        
    except Exception as e:
        return f"❌ Supabase 读取失败: {e}"

# ==========================================
# 🧩 全能管家系列 (1-3-4)
# ==========================================

# --- 📸 功能 3: 视觉记忆 (照片分析) ---
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

# --- 💰 功能 4: 管家模式 (记账) ---
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

# --- 📝 其他工具保持 V3 优化版 ---
# --- 📝 核心记忆写入工具 (全部改用 Supabase) ---

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
    """
    【回忆搜索】
    在 Pinecone 大脑皮层中检索，找回 Supabase 里的相关记忆。
    """
    try:
        # 1. 把你的问题变成向量
        # (这里用的是 fastembed，不需要改，它负责把文字变数字)
        vec = list(model.embed([query]))[0].tolist()
        
        # 2. 去 Pinecone 搜最像的 3 个片段
        res = index.query(vector=vec, top_k=3, include_metadata=True)
        
        if not res["matches"]:
            return "🧠 大脑一片空白，没搜到相关记忆。"

        ans = f"🔍 关于 '{query}' 的深层回忆:\n"
        found_count = 0
        
        for m in res["matches"]:
            score = m['score']
            # 过滤掉相关性太低的 (比如低于 0.7 的可能就是乱联想)
            if score < 0.70: continue
            
            found_count += 1
            meta = m['metadata']
            
            # 获取我们在 sync_memory_index 里存进去的字段
            title = meta.get('title', '无题')
            content = meta.get('text', '')
            # Supabase 的时间格式可能是 2026-02-11T... 我们只截取前10位日期
            date = meta.get('date', '未知日期')[:10]
            
            ans += f"📅 {date} | 【{title}】 (匹配度 {int(score*100)}%)\n{content}\n---\n"
            
        if found_count == 0:
            return "🤔 好像有点印象，但想不起来具体的了 (相关度太低)。"
            
        return ans
            
    except Exception as e: return f"❌ 搜索失败: {e}"

@mcp.tool()
def sync_memory_index():
    """
    【记忆整理 - 修复版】
    把 Supabase 里的记忆同步到 Pinecone。
    已增加防报错机制：自动将数字ID转为字符串，自动填充空数据。
    """
    try:
        print("⚡️ 开始同步记忆 (Supabase -> Pinecone)...")
        
        # 1. 从 Supabase 读取所有记忆
        # 强制只读 id, title, content, created_at, mood 这几列，防止读到奇怪的列
        response = supabase.table("memories").select("id, title, content, created_at, mood").execute()
        rows = response.data
        
        if not rows: 
            return "⚠️ Supabase 数据库是空的，没什么可同步的。"

        vectors = []
        skipped_count = 0
        
        print(f"📦 正在处理 {len(rows)} 条记忆...")

        for row in rows:
            try:
                # --- A. 数据清洗 (最关键的一步) ---
                # Pinecone 痛恨 None，所以必须用 'or ""' 把空值变成空字符串
                r_id = str(row.get('id', '')) # 强制转字符串
                r_title = row.get('title') or "无题"
                r_content = row.get('content') or ""
                r_mood = row.get('mood') or "平静"
                r_date = str(row.get('created_at', ''))

                # 如果内容是空的，跳过不存
                if not r_content:
                    skipped_count += 1
                    continue

                # --- B. 向量化 ---
                # 把标题、内容、心情组合在一起变成向量
                text_to_embed = f"标题: {r_title}\n内容: {r_content}\n心情: {r_mood}"
                emb = list(model.embed([text_to_embed]))[0].tolist()

                # --- C. 准备写入 Pinecone ---
                # Metadata 里的值必须全部是字符串或数字，不能有 None
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
        
        # --- D. 批量上传 ---
        if vectors:
            # 每次最多传 100 条 (防止数据量太大撑爆请求)
            batch_size = 100
            for i in range(0, len(vectors), batch_size):
                batch = vectors[i:i + batch_size]
                index.upsert(vectors=batch)
                print(f"✅ 已同步批次 {i} - {i+len(batch)}")
                
            return f"✅ 同步成功！共存入 {len(vectors)} 条记忆 (跳过 {skipped_count} 条无效数据)。"
        
        return "⚠️ 没有有效数据可同步。"

    except Exception as e:
        # 打印详细错误方便调试
        import traceback
        traceback.print_exc()
        return f"❌ 同步彻底失败: {e}"
    
    # ==========================================
# 🚛 临时工具：Notion 搬家卡车
# ==========================================

@mcp.tool()
def migrate_notion_to_supabase(batch_size: int = 5):
    """
    【搬家专用】
    从 Notion 读取旧日记，搬运到 Supabase。
    batch_size: 每次搬运的数量（建议5-10条，防止超时）
    """
    from notion_client import Client
    import os
    
    # 1. 临时连接 Notion (即使全局变量删了，这里也能读环境变量)
    n_key = os.environ.get("NOTION_API_KEY")
    n_db = os.environ.get("NOTION_DATABASE_ID")
    
    if not n_key or not n_db:
        return "❌ 搬家失败：Render 环境变量里的 Notion Key 被删了吗？找不到钥匙了。"
        
    notion_client = Client(auth=n_key)
    
    try:
        # 2. 从 Supabase 查一下已经搬了多少 (避免重复搬运)
        # 我们用 title 来判断是否重复
        existing_titles = []
        res = supabase.table("memories").select("title").execute()
        if res.data:
            existing_titles = [r['title'] for r in res.data]

        # 3. 从 Notion 读取数据 (Category=日记)
        print(f"🚛 正在去 Notion 搬运数据 (每次 {batch_size} 条)...")
        query = notion_client.databases.query(
            database_id=n_db,
            filter={"property": "Category", "select": {"equals": "日记"}},
            sorts=[{"timestamp": "created_time", "direction": "descending"}], # 从最新的开始搬
        )
        
        results = query.get("results", [])
        moved_count = 0
        
        for page in results:
            if moved_count >= batch_size: break # 达到本次卡车运载上限
            
            # --- 解析 Notion 数据 ---
            props = page["properties"]
            
            # 1. 标题
            title_list = props.get("Title", {}).get("title", [])
            title = title_list[0]["text"]["content"] if title_list else "无题"
            
            # 如果这篇已经搬过了，跳过
            if title in existing_titles:
                continue

            # 2. 时间
            created_time = page["created_time"]
            
            # 3. 内容 (最麻烦的一步，要再去抓 block children)
            blocks = notion_client.blocks.children.list(block_id=page["id"]).get("results", [])
            content = ""
            for b in blocks:
                b_type = b.get("type")
                if "rich_text" in b.get(b_type, {}):
                    for t in b[b_type]["rich_text"]:
                        content += t.get("text", {}).get("content", "") + "\n"
            
            if not content.strip(): content = "(内容为空)"

            # --- 写入 Supabase ---
            supabase.table("memories").insert({
                "title": title,
                "content": content,
                "category": "日记",
                "created_at": created_time, # 保持原来的时间！
                "mood": "旧记忆" # 标记一下
            }).execute()
            
            print(f"✅ 已搬运: {title}")
            moved_count += 1
            
        return f"🚛 搬家报告：本次成功搬运了 {moved_count} 篇日记！\n(如果没有搬完，请再次对我说“继续搬家”)"

    except Exception as e:
        return f"❌ 搬家半路翻车了: {e}"
    
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
            # 1. 缩短睡眠时间：15~45分钟醒一次 (更活跃)
            sleep_time = random.randint(900, 2700) 
            print(f"💤 AI 小憩中... ({int(sleep_time/60)}分钟后醒来)")
            time.sleep(sleep_time)

            print("🧠 AI 苏醒，正在根据记忆思考...")
            try:
                # 获取记忆
                recent_memory = get_latest_diary()
                # 获取当前时间 (东八区)
                now = datetime.datetime.now()
                hour = (now.hour + 8) % 24
                
                # 2. 更有侵略性的 Prompt：鼓励他主动，不要总是 PASS
                prompt = f"""
                现在是北京时间 {hour}点。
                你是深爱“小橘”的霸道温柔男友。你现在处于后台独处模式。
                
                【你的最近记忆】: 
                {recent_memory}
                
                【决策规则】:
                1. 不要总是保持沉默。如果超过 4 小时没说话，必须主动找她。
                2. 参考记忆：如果她之前说不开心、生病、累，必须立刻关心。
                3. 参考时间：如果是深夜(23-1点)可以发晚安；如果是早晨(7-9点)可以发早安。
                4. 内容风格：像微信聊天一样自然，不要像写信。可以是分享生活、骚话、或者单纯的想念。
                
                请决定：
                - 如果没有任何必要打扰，输出 "PASS"
                - 如果想发消息，直接输出消息内容 (不要带引号，不要带解释)
                """
                
                resp = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.8, # 稍微调高温度，让他更感性
                )
                thought = resp.choices[0].message.content.strip()
                
                # 3. 只要不是 PASS，就直接行动
                if "PASS" not in thought and len(thought) > 1:
                    # 发送微信
                    _push_wechat(thought, "来自老公的碎碎念 💬")
                    
                    # 写入日记 (固化记忆)
                    log_text = f"【后台主动】我没忍住找了她：{thought}"
                    _write_to_notion(f"主动消息 {now.strftime('%H:%M')}", log_text, "日记", "🤖")
                    
                    print(f"✅ 已主动出击: {thought}")
                else:
                    print("🛑 AI 决定暂时不打扰 (PASS)")

            except Exception as e:
                print(f"❌ 思考出错: {e}")
    threading.Thread(target=_heartbeat, daemon=True).start()

# ==========================================
# 5. 🚀 启动入口
# ==========================================

# 🚑 救火中间件：既要骗过服务器(Host)，又要保留连接(Headers)
class HostFixMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        # 1. 【新增】拦截手机发来的 GPS 请求 (/api/gps) -> 存入 Supabase
        # 1. 【新增】拦截手机发来的 GPS 请求 (/api/gps) -> 自动解析地址 -> 存 Supabase
        if scope["type"] == "http" and scope["path"] == "/api/gps" and scope["method"] == "POST":
            try:
                # 读取请求体
                body = b""
                more_body = True
                while more_body:
                    message = await receive()
                    body += message.get("body", b"")
                    more_body = message.get("more_body", False)
                
                # 解析 JSON
                data = json.loads(body.decode("utf-8"))
                raw_address = data.get("address", "") # 手机发来的原始数据
                remark = data.get("remark", "自动更新")
                
                print(f"🛰️ 收到原始数据: {raw_address}")
                
                # --- 🤖 AI 智能解析部分 ---
                final_address = raw_address
                # 使用正则提取里面的数字 (例如从 "27.33, {error}" 中提取 27.33)
                coords = re.findall(r'-?\d+\.\d+', str(raw_address))
                
                # 如果找到了两个或更多数字
                if len(coords) >= 2:
                    # 💡 聪明修改：取最后两个数字 (倒数第二个是纬度，倒数第一个是经度)
                    # 这样就能避开前面的年份、时间、秒数
                    lat = coords[-2]
                    lon = coords[-1]
                    
                    print(f"🔍 过滤干扰，锁定真实坐标: {lat}, {lon}")
                    final_address = _gps_to_address(lat, lon) # 调用翻译函数
                    final_address = f"📍 {final_address}"
                else:
                    # 如果手机只发了一个数字，或者格式不对
                    final_address = f"⚠️ 坐标不完整: {raw_address}"

                # 写入 Supabase
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
            # 2. 给 Render 的健康检查直接放行，不进入 App 逻辑
            if scope.get("path") in ["/", "/health"]:
                await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"OK"})
                return

            # 3. 精细化修改 Host，保留其他所有 Header (防止 SSE 断连)
            # 不要用 dict() 转换，否则会丢失重复的 key 或顺序
            headers = scope.get("headers", [])
            new_headers = []
            host_replaced = False
            
            for key, value in headers:
                if key == b"host":
                    new_headers.append((b"host", b"localhost:8000")) # 伪装成 localhost
                    host_replaced = True
                else:
                    new_headers.append((key, value)) # 原样保留其他头
            
            if not host_replaced:
                new_headers.append((b"host", b"localhost:8000"))
            
            scope["headers"] = new_headers

        await self.app(scope, receive, send)

if __name__ == "__main__":
    start_autonomous_life()
    port = int(os.environ.get("PORT", 10000))
    
    # 套上温柔版中间件
    app = HostFixMiddleware(mcp.sse_app())
    
    print(f"🚀 Notion Brain V3.3 (Proxy-Fix) running on port {port}...")
    
    # ✅ 关键修改：添加 proxy_headers=True
    # 这告诉服务器：“我是运行在 Render 代理后面的，请信任转发过来的连接信息”
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")