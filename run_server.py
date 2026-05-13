"""Launch the public results server. Run: python run_server.py"""
import uvicorn
uvicorn.run("server.app:app", host="0.0.0.0", port=8000, reload=False)
