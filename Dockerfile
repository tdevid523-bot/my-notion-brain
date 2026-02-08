# 使用最轻量的 Python 基础镜像
FROM python:3.9-slim

# 设置工作目录
WORKDIR /app

# 1. 先把清单拷进去
COPY requirements.txt .

# 2. 【关键一步】先手动安装 CPU 版的 PyTorch (只有 100多M，不像完整版有 800M)
# 这样不仅省空间，还能防止内存撑爆
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# 3. 再安装剩下的工具 (sentence-transformers 会自动发现已经有 torch 了，就不会再去下载那个巨大的版本)
RUN pip install --no-cache-dir -r requirements.txt

# 4. 把代码拷进去
COPY . .

# 启动！
CMD ["python", "server.py"]