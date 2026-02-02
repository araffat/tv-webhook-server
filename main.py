from fastapi import FastAPI, Request
import json

app = FastAPI()

@app.get("/")
def root():
    return {"ok": True, "msg": "webhook server alive"}

@app.post("/tv-webhook")
async def tv_webhook(req: Request):
    raw = await req.body()
    text = raw.decode("utf-8", errors="ignore")
    print("=== Webhook Received ===")
    print(text)
    return {"ok": True}
