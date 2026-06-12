FROM nousresearch/hermes-agent:latest

USER root

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

RUN uv pip install --no-cache-dir --python /opt/hermes/.venv/bin/python3 \
    "trafilatura>=1.12,<3.0" \
    "lxml>=4.9" \
    "beautifulsoup4==4.12.2" \
    "sse-starlette==2.1.3"

# NOTE: vLLM streaming tool_call workaround removed — upstream hermes-agent
# run_agent.py has been refactored and the old patch anchor no longer exists.
# If you hit vLLM streaming issues, investigate run_agent.py in the latest image.

RUN mkdir -p /app && chown hermes:hermes /app
COPY --chown=hermes:hermes . /app/

COPY --chown=root:root docker/entrypoint-adapter.sh /entrypoint-adapter.sh
RUN chmod +x /entrypoint-adapter.sh

WORKDIR /app
EXPOSE 8000

ENTRYPOINT ["/entrypoint-adapter.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
