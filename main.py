import os, json, datetime
import aiosqlite
import httpx
from fastapi import FastAPI, Request
from openai import OpenAI

app = FastAPI()

# ====== 环境变量 ======
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
client = OpenAI(api_key=OPENAI_API_KEY)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "")  # 例: whatsapp:+14155238886 (sandbox)
WHATSAPP_TO = os.getenv("WHATSAPP_TO", "")  # 例: whatsapp:+90xxxxxxxxxx

DB_PATH = os.getenv("DB_PATH", "tradelog.db")

# ====== GPT 风控提示词（你可以以后再微调）=====
RISK_INSTRUCTIONS = """
你是BTC永续合约 15m 信号的风控助手（OKX:BTCUSDT.P）。
你只输出严格 JSON，不要输出多余文字，不要 markdown。

规则（重点）：
- 15m 噪音大，优先过滤差的信号。条件不足 -> action=wait
- 若 signal=LONG 但 4H/1D 明显看空（如果 payload 提供了 htf4h/htf1d 且为 BEAR） -> wait
- 若 signal=SHORT 但 4H/1D 明显看多 -> wait
- 默认止损 sl_pct 0.7~1.0；止盈 tp_pct 2.0~3.2；风险高则降低 tp、提高 wait 概率
- 不喊单、不承诺收益，用中文输出一句话风控提示

输出格式固定：
{
  "action": "enter" | "wait",
  "direction": "long" | "short",
  "confidence": 0-100,
  "risk_level": "low" | "mid" | "high",
  "sl_pct": number,
  "tp_pct": number,
  "message_cn": "中文风控话术(1-2句)",
  "checklist": ["...","...","..."]
}
"""

# ====== DB 初始化 ======
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS signals(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts_utc TEXT,
          raw_text TEXT,
          json_payload TEXT,
          gpt_json TEXT
        )
        """)
        await db.commit()

@app.on_event("startup")
async def startup():
    await init_db()

@app.get("/")
def root():
    return {"ok": True, "msg": "webhook server alive"}

# 兼容：就算你URL写成根路径，也能接住（可选）
@app.post("/")
async def webhook_root(req: Request):
    return await handle_webhook(req, path="/")

@app.post("/tv-webhook")
async def tv_webhook(req: Request):
    return await handle_webhook(req, path="/tv-webhook")

def safe_json_loads(text: str):
    try:
        return json.loads(text)
    except:
        return None

async def send_whatsapp(text: str):
    # 没配 Twilio 就跳过（不报错）
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM and WHATSAPP_TO):
        return

    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    async with httpx.AsyncClient(timeout=15) as hc:
        resp = await hc.post(
            url,
            data={"From": TWILIO_WHATSAPP_FROM, "To": WHATSAPP_TO, "Body": text},
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
        )
        resp.raise_for_status()

def format_whatsapp(payload: dict, g: dict) -> str:
    ticker = payload.get("ticker", "BTCUSDT.P")
    tf = payload.get("interval", "15m")
    sig = payload.get("signal", "UNKNOWN")
    price = payload.get("price", payload.get("close", ""))

    return (
        f"{sig}\n"
        f"价: {price}\n"
        f"建议: {g.get('action')} | 风险: {g.get('risk_level')} | 置信度: {g.get('confidence')}\n"
        f"SL: {g.get('sl_pct')}%  TP: {g.get('tp_pct')}%\n"
        f"{g.get('message_cn')}\n"
        f"检查: " + " | ".join((g.get("checklist") or [])[:3])
    )

async def call_gpt_risk(payload: dict) -> dict:
    # OpenAI Responses API（官方推荐新项目使用）:contentReference[oaicite:3]{index=3}
    resp = client.responses.create(
        model="gpt-4.1-mini",
        instructions=RISK_INSTRUCTIONS,
        input=json.dumps(payload, ensure_ascii=False),
    )
    text = (resp.output_text or "").strip()
    g = safe_json_loads(text)
    if not isinstance(g, dict):
        # 兜底
        g = {
            "action": "wait",
            "direction": "long" if payload.get("signal") == "LONG" else "short",
            "confidence": 0,
            "risk_level": "high",
            "sl_pct": 0.9,
            "tp_pct": 2.2,
            "message_cn": "风控解析失败，建议手动确认后再决定。",
            "checklist": ["确认趋势方向", "确认关键位", "确认波动是否异常"]
        }
    return g

async def handle_webhook(req: Request, path: str):
    raw = await req.body()
    text = raw.decode("utf-8", errors="ignore").strip()

    print(f"=== Webhook Received at {path} ===")
    print("RAW:", text)

    payload = safe_json_loads(text)
    # TradingView 有时发纯文本（例如 alert("TV_FORCE_TEST")），我们也兼容
    if payload is None:
        payload = {"raw": text}

    # 给 payload 补充时间戳
    payload.setdefault("recv_ts_utc", datetime.datetime.utcnow().isoformat())

    # GPT 风控
    g = await call_gpt_
