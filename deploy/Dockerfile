FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY kernel ./kernel
COPY migrations ./migrations
COPY mcp-server ./mcp-server
COPY scripts ./scripts
COPY alembic.ini ./

RUN pip install --no-cache-dir \
    "sqlalchemy>=2.0" \
    "alembic>=1.13" \
    "fastmcp>=2.0" \
    "psycopg[binary]>=3.1"

# 默认以 HTTP MCP 服务启动；init 一次性任务由 compose 覆盖 command
CMD ["python", "deploy/run_http.py"]
