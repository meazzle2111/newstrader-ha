FROM python:3.13-slim-bookworm

# Node.js nur für die Installation der Claude-Code-Kommandozeile (Nachrichtenanalyse über dein Claude-Abo)
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm ca-certificates tzdata \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && apt-get purge -y npm && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY newstrader ./newstrader

ENV NEWSTRADER_HOME=/data PYTHONUNBUFFERED=1
CMD ["python", "-m", "newstrader", "addon"]
