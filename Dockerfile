FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV ASTRBOTEX_HOST=0.0.0.0
ENV ASTRBOTEX_PORT=8765
ENV ASTRBOTEX_TICK_HZ=5
ENV ASTRBOTEX_DATA_DIR=/app/data

WORKDIR /app

COPY astrbot_ex ./astrbot_ex
COPY dashboard ./dashboard
COPY scripts ./scripts
COPY README.md ./
COPY README_AstrBotEX插件系统规范.md ./

EXPOSE 8765

CMD ["python", "-m", "astrbot_ex.core.api_server", "--host", "0.0.0.0", "--port", "8765", "--tick-hz", "5"]
