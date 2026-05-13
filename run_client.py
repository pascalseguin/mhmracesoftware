"""Launch the local client. Run: python run_client.py"""
import uvicorn
from client.config import load

cfg = load()
uvicorn.run("client.app:app", host=cfg["host"], port=cfg["port"], reload=False)
