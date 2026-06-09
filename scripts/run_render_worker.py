#!/usr/bin/env python3
"""Production entry point for the Larbild render worker (lb-ocx run_worker_loop).

Boots the Flask app (so init_db + config + storage paths are wired) and runs the
BRPOP render loop inside an app context. The worker pulls job ids off the
larbild:render:task_queue Redis list, assembles the SFW txt2img graph (LoRA
resolved + allowlist-validated), enters render-residency on the shared VM100 3090
(signal render-pressure → dispatcher evicts the LLM → hold lock:vm100-3090),
submits to ComfyUI on VM100, downloads the PNG, and auto-sorts it into the
library. Run under systemd (larbild-worker.service).
"""
import logging
import os

logging.basicConfig(
    level=os.environ.get("LARBILD_WORKER_LOGLEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from app import create_app
from app.render_worker import run_worker_loop

def main() -> None:
    app = create_app()
    with app.app_context():
        logging.getLogger("larbild.worker").info("render worker entering loop")
        run_worker_loop()

if __name__ == "__main__":
    main()
