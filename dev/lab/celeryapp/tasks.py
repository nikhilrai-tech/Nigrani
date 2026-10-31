from celery import Celery
app = Celery("tasks", broker="redis://redis:6379/1")
app.conf.worker_send_task_events = True
app.conf.task_send_sent_event = True

@app.task
def add(x, y): return x + y

@app.task
def flaky(n):
    if n % 2: raise ValueError("odd")
    return n
