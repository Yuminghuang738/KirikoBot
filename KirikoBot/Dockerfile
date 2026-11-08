FROM python:3.11-slim

# 安装系统基础依赖、Node.js 22 LTS 以及 DeepSeek Harness (DSH)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gnupg ca-certificates git libxml2-dev libxslt-dev gcc && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && apt-get install -y --no-install-recommends nodejs && \
    npm install -g pnpm @deepseek-ai/dsh@0.2.2-rc.1 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 创建运行用户，并确保主目录和应用目录权限健全（供 DSH ~/.dsh 与 workspace 使用）
RUN useradd --create-home --user-group appuser && \
    mkdir -p /home/appuser/.dsh /app/shared_files && \
    chown -R appuser:appuser /home/appuser /app

USER appuser

EXPOSE 5000

CMD ["waitress-serve", "--host=0.0.0.0", "--port=5000", "main:app"]
