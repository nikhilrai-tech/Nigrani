FROM python:3.12-slim
RUN pip install --no-cache-dir "celery==5.6.3" redis
WORKDIR /app
COPY tasks.py /app/
CMD ["celery", "-A", "tasks", "worker", "-E", "--loglevel=info"]
