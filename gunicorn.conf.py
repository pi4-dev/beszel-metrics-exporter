import os

bind = f"{os.getenv('LISTEN_HOST', '0.0.0.0')}:{os.getenv('LISTEN_PORT', '9105')}"
workers = 1
threads = 4
worker_class = "gthread"
timeout = 30
accesslog = "-"
errorlog = "-"
