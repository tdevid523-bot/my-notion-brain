import os
import uvicorn
from mcp.server.fastmcp import FastMCP
from notion_client import Client
from pinecone import Pinecone
from fastembed import TextEmbedding

# 1. 获取配置
notion_key = os.environ.get("NOTION_API_KEY")
database_id = os.environ.get("NOTION_DATABASE_ID")
pinecone_key = os.environ.get("PINECONE_API_KEY")

# 2. 初始化
print("⏳ 正在初始化服务...")
notion = Client(auth=notion_key)
pc = Pinecone(api_key=pinecone_key)
index = pc.Index("notion-brain")

# 3. 加载轻量模型 (省内存，速度快)
print("🚀 加载 FastEmbed...")
model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")

mcp = FastMCP("Notion Brain")

# --- 核心功能 (保持不变) ---
@mcp.tool()
def sync_notion_index():
    try:
        print("⚡️ 开始同步...")
        # 搜索所有页面
        all_pages = notion.search(filter={"value": "page", "property": "object"})["results"]
        vectors = []
        target_id_clean = database_id.replace("-", "")
        count = 0
        
        for p in all_pages:
            # 检查父级数据库ID
            pid = p.get("parent", {}).get("database_id", "")
            if pid and pid.replace("-", "") == target_id_clean:
                # 简单提取标题
                title = "无题"
                if "Title" in p["properties"] and p["properties"]["Title"]["title"]:
                    title = p["properties"]["Title"]["title"][0]["text"]["content"]
                
                txt = f"标题: {title}"
                # 生成向量
                emb = list(model.embed([txt]))[0].tolist()
                
                vectors.append((p["id"], emb, {"text": txt, "title": title}))
                count += 1
        
        if vectors:
            index.upsert(vectors=vectors)
            return f"✅ 成功同步 {count} 条记忆！"
        return "⚠️ 没找到内容"
    except Exception as e: return f"❌ 同步失败: {e}"

@mcp.tool()
def search_memory_semantic(query: str):
    try:
        vec = list(model.embed([query]))[0].tolist()
        res = index.query(vector=vec, top_k=3, include_metadata=True)
        ans = "Found:\n"
        for m in res["matches"]:
            ans += f"- {m['metadata'].get('text','')} (相似度 {m['score']:.2f})\n"
        return ans
    except Exception as e: return f"❌ 搜索失败: {e}"

if __name__ == "__main__":
    # ⚠️ Render 专用：读取环境变量 PORT，如果没有则默认 10000
    port = int(os.environ.get("PORT", 10000))
    print(f"🌍 启动端口: {port}")
    # host 必须是 0.0.0.0
    uvicorn.run(mcp.sse_app(), host="0.0.0.0", port=port)