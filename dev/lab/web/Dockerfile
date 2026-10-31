FROM python:3.12-slim
RUN pip install --no-cache-dir "django==6.0.9" "django-prometheus==2.5.0" gunicorn
WORKDIR /app
COPY . /app
CMD ["gunicorn", "lab.wsgi", "-b", "0.0.0.0:8000", "-w", "1", "--threads", "4"]
