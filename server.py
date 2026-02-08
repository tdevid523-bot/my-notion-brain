import os
import datetime
from dotenv import load_dotenv
import uvicorn
from mcp.server.fastmcp import FastMCP
from notion_client import Client
from starlette.types import ASGIApp, Scope, Receive, Send
from pinecone import Pinecone # 云端数据库
from sentence_transformers import SentenceTransformer # 向量翻译官

# 1. 加载配置
load_dotenv()
notion_key = os.getenv("NOTION_API_KEY")
database_id = os.getenv("NOTION_DATABASE_ID")
pinecone_key = os.getenv("PINECONE_API_KEY")

# 2. 初始化 Notion
notion = Client(auth=notion_key)

# 3. 初始化 Pinecone (云端记忆库)
print("⏳ 正在连接 Pinecone 云端大脑...")
pc = Pinecone(api_key=pinecone_key)
# ⚠️ 确保你在网页上创建的 Index 名字叫 'notion-brain'，或者改成你自己的名字
index_name = "notion-brain" 
if index_name not in pc.list_indexes().names():
    print(f"❌ 错误：请先在 Pinecone 网页上创建一个叫 '{index_name}' 的 Index！维度(Dimensions)设为 384。")
    exit()
index = pc.Index(index_name)

# 4. 初始化向量模型 (本地翻译官)
# 它会把文字转换成 384 维的数字列表
print("⏳ 正在加载嵌入模型 (第一次可能比较慢)...")
model = SentenceTransformer('all-MiniLM-L6-v2') 

mcp = FastMCP("Notion Pinecone Brain")

# --- 工具 1: 写入 (保持不变) ---
@mcp.tool()
def write_notion_page(title: str, content: str, category: str = "日常", date: str = None):
    if not date: date = datetime.date.today().isoformat()
    print(f"⚡️ [写入] {title}")
    try:
        notion.pages.create(
            parent={"database_id": database_id},
            properties={
                "Title": {"title": [{"text": {"content": title}}]},
                "Category": {"select": {"name": category}},
                "Date": {"date": {"start": date}}
            },
            children=[{
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": content}}]
                }
            }]
        )
        return "✅ 写入成功！(记得运行 sync_notion_index 同步到云端)"
    except Exception as e:
        return f"❌ 写入失败: {e}"

# --- 工具 2: 精确读取 (保持不变) ---
@mcp.tool()
def read_notion_exact(category: str = None, date: str = None):
    print(f"⚡️ [精确查阅]")
    return "请使用语义搜索。"

# --- 工具 3: 同步索引 (升级为 Pinecone 版) ---
@mcp.tool()
def sync_notion_index():
    print("⚡️ [开始同步] 正在从 Notion 下载并上传到 Pinecone...")
    target_id_clean = database_id.replace("-", "")
    
    try:
        all_pages = notion.search(filter={"value": "page", "property": "object"})["results"]
        
        vectors_to_upload = []
        count = 0
        
        for page in all_pages:
            parent = page.get("parent", {})
            p_id = parent.get("database_id") or parent.get("data_source_id")
            
            # ID 匹配逻辑
            if p_id and p_id.replace("-", "") == target_id_clean:
                try:
                    # 1. 提取元数据
                    props = page["properties"]
                    t_obj = props.get("Title", {}).get("title", [])
                    title = t_obj[0]["text"]["content"] if t_obj else "无标题"
                    c_obj = props.get("Category", {}).get("select")
                    category = c_obj["name"] if c_obj else "未分类"
                    d_obj = props.get("Date", {}).get("date")
                    date = d_obj["start"] if d_obj else "未知"
                    
                    # 2. 读取正文
                    page_id = page["id"]
                    blocks = notion.blocks.children.list(block_id=page_id)
                    content = ""
                    for b in blocks["results"]:
                        if "paragraph" in b and b["paragraph"]["rich_text"]:
                            for t in b["paragraph"]["rich_text"]:
                                content += t["text"]["content"]
                    
                    full_text = f"标题:{title}\n分类:{category}\n日期:{date}\n内容:{content}"
                    
                    # 3. 生成向量 (Embedding)
                    # 这里的 embedding 是一个 384 个数字组成的列表
                    embedding = model.encode(full_text).tolist()
                    
                    # 4. 准备 Pinecone 数据包
                    # 格式: (ID, 向量, 元数据)
                    vectors_to_upload.append((
                        page_id, 
                        embedding, 
                        {"text": full_text, "category": category, "date": date, "title": title}
                    ))
                    
                    count += 1
                    print(f"   Prepared: {title}")
                    
                except Exception as e:
                    print(f"   ⚠️ 跳过页面: {e}")
        
        if vectors_to_upload:
            # 批量上传到 Pinecone
            print(f"🚀 正在上传 {len(vectors_to_upload)} 条记忆到云端...")
            index.upsert(vectors=vectors_to_upload)
            msg = f"✅ 同步完成！{count} 条记忆已永久存入 Pinecone。"
        else:
            msg = "⚠️ 没找到需要同步的内容。"
            
        print(msg)
        return msg
    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"❌ 同步失败: {e}"

# --- 工具 4: 语义搜索 (升级为 Pinecone 版) ---
@mcp.tool()
def search_memory_semantic(query: str, n_results: int = 3):
    print(f"⚡️ [云端思考]: {query}")
    try:
        # 1. 把问题也变成向量
        query_embedding = model.encode(query).tolist()
        
        # 2. 去 Pinecone 里搜最相似的向量
        result = index.query(
            vector=query_embedding,
            top_k=n_results,
            include_metadata=True # 这一步很重要，要把原文拿回来
        )
        
        matches = result.get("matches", [])
        if not matches:
            return "🧠 云端大脑里没找到相关记忆。"
            
        answer = "Found:\n"
        for match in matches:
            score = match["score"]
            text = match["metadata"]["text"]
            date = match["metadata"]["date"]
            # 过滤掉相关性太低的 (比如分数小于 0.3)
            if score > 0.3:
                answer += f"---\n[相关度 {score:.2f} | {date}]\n{text}\n"
        
        return answer
    except Exception as e:
        return f"❌ Error: {e}"

# --- 中间件 ---
class HostFixMiddleware:
    def __init__(self, app: ASGIApp): self.app = app
    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            headers[b"host"] = b"localhost:8000"
            scope["headers"] = list(headers.items())
        await self.app(scope, receive, send)

if __name__ == "__main__":
    print("🚀 【Pinecone 永生版】服务器启动中...")
    # 自动适配云端端口
    port = int(os.environ.get("PORT", 8000))
    raw_app = mcp.sse_app()
    final_app = HostFixMiddleware(raw_app)
    uvicorn.run(final_app, host="0.0.0.0", port=port, http="h11")