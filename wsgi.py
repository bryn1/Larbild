"""WSGI entry point for Lärbild.

Production (gunicorn)::

    gunicorn -w 2 -b 0.0.0.0:6767 wsgi:app

Dev (Flask)::

    FLASK_APP=app:create_app flask run --port 6767
    # or simply:
    python wsgi.py
"""
from __future__ import annotations

from app import create_app

app = create_app()


if __name__ == "__main__":
    app.run(host=app.config["HOST"], port=app.config["PORT"])
