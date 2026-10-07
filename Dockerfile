FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright BIND_HOST=0.0.0.0
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && useradd --create-home --uid 10001 poc
RUN mkdir -p /app/data && chown poc:poc /app/data && chmod 700 /app/data
COPY --chown=poc:poc . .
USER poc
EXPOSE 8000
CMD ["python", "run.py"]
