"""Loaded automatically by the existing Render start command from flask-app."""
worker_class = 'gthread'
threads = 4


def post_worker_init(worker):
    from models import db
    if not db.is_closed():
        db.close()
    from generation_jobs import wake
    wake()


def worker_exit(server, worker):
    from generation_jobs import shutdown
    shutdown()
