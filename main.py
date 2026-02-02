import os
import json
import datetime
import traceback

import aiosqlite
import httpx
from fastapi import FastAPI, Request
from openai import OpenAI

# =========================
# App / Config
# =========================
APP_VERSION = "gpt-chatcompletions-v2"

app = FastAPI()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Twilio WhatsApp (optional)
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()  # e.g. "whatsapp:+14155238886"
WHATSAPP_TO = os.getenv("WHATSAPP_TO", "").strip()  # e.g. "whatsapp:+90xxxxxxxxxx"

DB_PATH = os.getenv("DB_PATH", "tradelog.db").strip() or "tradelog.db"

print(f"APP VERSION: {APP_VERSION}")
print("OPENAI_API_KEY loaded:", bool(OPENAI_API_KEY))
print("WhatsApp configured:", bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM and WHATSAPP_TO))


# =========================
# Risk instructions (GPT)
# =========================
RISK_INSTRUCTIONS = """
你是BTC永续合约 15m 信号的风控助手（OKX:BTCUSDT.P）。
你只输出严格 JSON，不要输出多余文字，不要 markdown，不要代码块。

目标：过滤低质量信号，尽量避免追涨杀跌。

规则（重点）：
- 15m 噪音大：条件不足 -> action=wait
- 若 signal=LONG 但 payload 提供 htf4h/htf1d 且为 BEAR -> wait
- 若 signal=SHORT 但 payload 提供 htf4h/htf1d 且为 BULL -> wait
- 默认止损 sl_pct 0.7~1.0；止盈 tp_pct 2.0~3.2
- 风险高时：降低置信度、提高 wait 概率
- 输出中文风控话术 1-2 句，不能承诺收益

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
""".strip()


# =========================
# Helpers
# =========================
def safe_json_loads(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None


def default_gpt_fallback(payload: dict, reason: str = "GPT调用失败") -> dict:
    sig = (payload.get("signal") or "").upper()
    direction = "long" if sig == "LONG" else "short" if sig == "SHORT" else "long"
    return {
        "action": "wait",
        "direction": direction,
        "confidence": 0,
        "risk_level": "high",
        "sl_pct": 0.9,
        "tp_pct": 2.2,
        "message_cn": f"{reason}，建议手动确认趋势与关键位后再决定。",
        "checklist": ["确认4H/1D方向", "确认前高前低/关键位", "确认波动与成交量是否异常"],
    }


def format_whatsapp(payload: dict, g: dict) -> str:
    ticker = payload.get("ticker", payload.get("symbol", "BTCUSDT.P"))
    tf = payload.get("interval", payload.get("timeframe", "15m"))
    sig = payload.get("signal", payload.get("raw", "UNKNOWN"))
    price = payload.get("price", payload.get("close", ""))

    return (
        f"信号: {sig}\n"
        f"价: {price}\n"
        f"建议: {g.get('action')} | 风险: {g.get('risk_level')} | 置信度: {g.get('confidence')}\n"
        f"SL: {g.get('sl_pct')}%  TP: {g.get('tp_pct')}%\n"
        f"{g.get('message_cn')}\n"
        f"检查: " + " | ".join((g.get("checklist") or [])[:3])
    )


async def send_whatsapp(text: str):
    # Not configured -> skip silently
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM and WHATSAPP_TO):
        return

    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    async with httpx.AsyncClient(timeout=20) as hc:
        resp = await hc.post(
            url,
            data={"From": TWILIO_WHATSAPP_FROM, "To": WHATSAPP_TO, "Body": text},
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
        )
        resp.raise_for_status()


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS signals(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts_utc TEXT,
              path TEXT,
              raw_text TEXT,
              json_payload TEXT,
              gpt_json TEXT,
              error TEXT
            )
            """
        )
        await db.commit()


# =========================
# GPT call (Chat Completions)
# =========================
async def call_gpt_risk(payload: dict) -> dict:
    # If no key / client -> fallback
    if not client:
        return default_gpt_fallback(payload, "未配置OPENAI_API_KEY")

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": RISK_INSTRUCTIONS},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0.2,
        )

        text = (resp.choices[0].message.content or "").strip()
        g = safe_json_loads(text)

        if not isinstance(g, dict):
            # JSON parse failed
            return default_gpt_fallback(payload, "GPT输出非JSON")

        # Basic sanity defaults
        g.setdefault("action", "wait")
        g.setdefault("direction", "long" if (payload.get("signal") or "").upper() == "LONG" else "short")
        g.setdefault("confidence", 0)
        g.setdefault("risk_level", "mid")
        g.setdefault("sl_pct", 0.9)
        g.setdefault("tp_pct", 2.5)
        g.setdefault("message_cn", "建议谨慎，等待更清晰的结构确认。")
        g.setdefault("checklist", ["确认趋势方向", "确认关键位", "确认波动是否异常"])

        return g

    except Exception as e:
        print("GPT call failed:", repr(e))
        return default_gpt_fallback(payload, "GPT调用异常")


# =========================
# FastAPI routes
# =========================
@app.on_event("startup")
async def startup():
    await init_db()


@app.get("/")
def root():
    return {"ok": True, "msg": "webhook server alive", "version": APP_VERSION}


@app.post("/")
async def webhook_root(req: Request):
    return await handle_webhook(req, path="/")


@app.post("/tv-webhook")
async def tv_webhook(req: Request):
    return await handle_webhook(req, path="/tv-webhook")


async def handle_webhook(req: Request, path: str):
    error_text = ""
    raw = await req.body()
    text = raw.decode("utf-8", errors="ignore").strip()

    print(f"=== Webhook Received at {path} ===")
    print("RAW:", text[:2000])  # limit log size

    # Parse JSON or treat as text
    payload = safe_json_loads(text)
    if payload is None:
        payload = {"raw": text}

    payload.setdefault("recv_ts_utc", datetime.datetime.utcnow().isoformat())

    # GPT risk
    try:
        g = await call_gpt_risk(payload)
    except Exception:
        error_text = traceback.format_exc()
        g = default_gpt_fallback(payload, "风控模块异常")

    # Save to DB
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO signals(ts_utc, path, raw_text, json_payload, gpt_json, error) VALUES(?,?,?,?,?,?)",
                (
                    payload["recv_ts_utc"],
                    path,
                    text,
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(g, ensure_ascii=False),
                    error_text,
                ),
            )
            await db.commit()
    except Exception as e:
        print("DB write failed:", repr(e))

    # WhatsApp send (optional)
    try:
        msg = format_whatsapp(payload, g)
        await send_whatsapp(msg)
    except Exception as e:
        print("WhatsApp send failed:", repr(e))

    return {"ok": True, "gpt": g}
