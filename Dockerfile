FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --create-home --uid 10001 bot && mkdir /app/data && chown bot:bot /app/data
COPY badminton /app/badminton
USER bot
CMD ["python", "-m", "badminton"]
