FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MPLBACKEND=Agg PA_ROOT=/app
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml LICENSE ./
COPY pa_agent ./pa_agent
COPY pa_server ./pa_server
COPY prompt_engineering ./prompt_engineering
RUN pip install --no-cache-dir .
RUN useradd --create-home --uid 10001 pa && mkdir -p /app/data /app/config /app/experience /app/records /app/logs \
    && chown -R pa:pa /app
USER pa
EXPOSE 8765
CMD ["python", "-m", "pa_server"]
