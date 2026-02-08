# 使用官方 Python
FROM python:3.9

# 创建用户 (Hugging Face 安全要求)
RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:$PATH"

WORKDIR /app

# 复制依赖并安装
COPY --chown=user requirements.txt requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 复制剩下的代码
COPY --chown=user . .

# 暴露端口 7860 (Hugging Face 专用)
EXPOSE 7860

# 启动命令
CMD ["python", "server.py"]