from fastapi import FastAPI, Request
import json

app = FastAPI()

@app.get("/")
def root():
    return {"ok": True, "msg": "webhook server alive"}

@app.post("/tv-webhook")
async def tv_webhook(req: Request):
    payload = await req.json()
    print("=== TradingView Webhook Received ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return {"ok": True}

