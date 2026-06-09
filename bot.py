"""
AXIOM SURGE BOT — WEBSOCKET VERSION
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from threading import Thread, Lock

import websockets
import requests

with open("config.json", "r") as f:
    CONFIG = json.load(f)

WALLET  = CONFIG["wallet_address"]
RPC_URL = CONFIG["solana_rpc_url"]

os.makedirs("logs", exist_ok=True)
log_file = f"logs/bot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler(log_file), logging.StreamHandler()]
)
log = logging.getLogger("AxiomWSBot")

WS_EUCALYPTUS = "wss://eucalyptus.axiom.trade/ws"
WS_HEADERS = {
    "Origin":          "https://axiom.trade",
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8"
}

PHASE1_STOP     = -0.15
PHASE2_TRIGGER  =  0.10
PHASE3_TRIGGER  =  0.25
PHASE3_TRAIL    =  0.10
TIMEOUT_MINUTES =  45
TIMEOUT_BAND    =  0.025
RISK_GUARD_PCT  =  0.50
TRADE_PCT       =  0.10
Y_UPDATE_HOURS  =  1
RESERVE_SOL     =  0.07

state_lock            = Lock()
open_trades           = {}
session_start_balance = None
trade_size_Y          = None
last_Y_update         = None
daily_guard_triggered = False
room_to_token         = {}
token_to_room         = {}
surging_rooms         = set()

def get_wallet_balance_sol():
    try:
        r = requests.post(RPC_URL, json={
            "jsonrpc":"2.0","id":1,
            "method":"getBalance","params":[WALLET]
        }, timeout=10)
        return r.json()["result"]["value"] / 1_000_000_000
    except Exception as e:
        log.error(f"Balance error: {e}")
        return None

def get_token_price_sol(token_address):
    try:
        sol = "So11111111111111111111111111111111111111112"
        r   = requests.get(f"https://price.jup.ag/v6/price?ids={token_address}&vsToken={sol}", timeout=8)
        return float(r.json()["data"][token_address]["price"])
    except Exception:
        return None

def get_token_balance_raw(token_address):
    try:
        r    = requests.post(RPC_URL, json={
            "jsonrpc":"2.0","id":1,
            "method":"getTokenAccountsByOwner",
            "params":[WALLET,{"mint":token_address},{"encoding":"jsonParsed"}]
        }, timeout=10)
        accs = r.json()["result"]["value"]
        if not accs:
            return 0
        return int(accs[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
    except Exception as e:
        log.error(f"Token balance error: {e}")
        return 0

def sign_and_send(raw_tx_b64):
    try:
        import base64
        from solders.keypair import Keypair
        from solders.transaction import VersionedTransaction
        keypair    = Keypair.from_bytes(bytes(CONFIG["private_key_bytes"]))
        tx         = VersionedTransaction.from_bytes(base64.b64decode(raw_tx_b64))
        tx.sign([keypair])
        signed_b64 = base64.b64encode(bytes(tx)).decode()
        r          = requests.post(RPC_URL, json={
            "jsonrpc":"2.0","id":1,"method":"sendTransaction",
            "params":[signed_b64,{"encoding":"base64","skipPreflight":False}]
        }, timeout=15)
        return r.json().get("result")
    except Exception as e:
        log.error(f"sign_and_send error: {e}")
        return None

def update_trade_size():
    global trade_size_Y, last_Y_update
    bal = get_wallet_balance_sol()
    if bal is None:
        return
    trade_size_Y  = round(bal * TRADE_PCT, 4)
    last_Y_update = datetime.now()
    log.info(f"Y = {trade_size_Y} SOL (wallet {bal:.4f} SOL)")

def maybe_update_trade_size():
    if last_Y_update is None:
        update_trade_size()
        return
    if (datetime.now() - last_Y_update).total_seconds() / 3600 >= Y_UPDATE_HOURS:
        update_trade_size()

def check_risk_guard():
    global daily_guard_triggered
    if daily_guard_triggered:
        return True
    bal = get_wallet_balance_sol()
    if bal and session_start_balance and bal <= session_start_balance * (1 - RISK_GUARD_PCT):
        log.warning("RISK GUARD — wallet -50%. No new trades today.")
        daily_guard_triggered = True
        return True
    return False

def available_capital():
    bal    = get_wallet_balance_sol() or 0
    locked = len(open_trades) * (trade_size_Y or 0)
    return max(bal - locked - RESERVE_SOL, 0)

def should_sell(trade, pnl):
    if pnl > trade["peak_pnl"]:
        trade["peak_pnl"] = pnl
    if pnl < PHASE2_TRIGGER:
        trade["phase"] = 1
        if pnl <= PHASE1_STOP:
            return True, f"STOP LOSS -15% pnl={pnl:+.1%}"
    elif pnl < PHASE3_TRIGGER:
        trade["phase"] = 2
        if pnl <= 0:
            return True, f"BREAK-EVEN pnl={pnl:+.1%}"
    else:
        trade["phase"] = 3
        if pnl <= trade["peak_pnl"] - PHASE3_TRAIL:
            return True, f"TRAIL STOP peak={trade['peak_pnl']:+.1%} now={pnl:+.1%}"
    age = (datetime.now() - trade["buy_time"]).total_seconds() / 60
    if age >= TIMEOUT_MINUTES and abs(pnl) <= TIMEOUT_BAND:
        return True, f"TIMEOUT {age:.0f}min pnl={pnl:+.1%}"
    return False, None
def execute_buy(token_address):
    if check_risk_guard() or token_address in open_trades:
        return
    if available_capital() < (trade_size_Y or 0):
        log.warning("Not enough capital")
        return
    sol = "So11111111111111111111111111111111111111112"
    lam = int(trade_size_Y * 1_000_000_000)
    try:
        q = requests.get(
            f"https://quote-api.jup.ag/v6/quote?inputMint={sol}&outputMint={token_address}"
            f"&amount={lam}&slippageBps=1500", timeout=10
        ).json()
        if "error" in q:
            log.error(f"Quote error: {q['error']}")
            return
        s = requests.post("https://quote-api.jup.ag/v6/swap", json={
            "quoteResponse":q,"userPublicKey":WALLET,
            "wrapAndUnwrapSol":True,"prioritizationFeeLamports":1000
        }, timeout=10).json()
        if "swapTransaction" not in s:
            log.error(f"No swapTx: {s}")
            return
        sig       = sign_and_send(s["swapTransaction"])
        buy_price = get_token_price_sol(token_address)
        with state_lock:
            open_trades[token_address] = {
                "address":   token_address,
                "buy_time":  datetime.now(),
                "buy_price": buy_price,
                "sol_spent": trade_size_Y,
                "peak_pnl":  0.0,
                "phase":     1,
                "tx_buy":    sig,
            }
        log.info(f"BUY {token_address[:14]}... {trade_size_Y} SOL price={buy_price}")
    except Exception as e:
        log.error(f"Buy failed {token_address}: {e}")

def execute_sell(token_address, reason):
    trade = open_trades.get(token_address)
    if not trade:
        return
    try:
        raw = get_token_balance_raw(token_address)
        if not raw:
            with state_lock:
                open_trades.pop(token_address, None)
            return
        sol = "So11111111111111111111111111111111111111112"
        q   = requests.get(
            f"https://quote-api.jup.ag/v6/quote?inputMint={token_address}&outputMint={sol}"
            f"&amount={raw}&slippageBps=1500", timeout=10
        ).json()
        if "error" in q:
            return
        s = requests.post("https://quote-api.jup.ag/v6/swap", json={
            "quoteResponse":q,"userPublicKey":WALLET,
            "wrapAndUnwrapSol":True,"prioritizationFeeLamports":1000
        }, timeout=10).json()
        if "swapTransaction" not in s:
            return
        sig        = sign_and_send(s["swapTransaction"])
        exit_price = get_token_price_sol(token_address)
        pnl_pct    = ((exit_price - trade["buy_price"]) / trade["buy_price"]) if (exit_price and trade["buy_price"]) else 0
        pnl_sol    = trade["sol_spent"] * pnl_pct
        emoji      = "🟢" if pnl_sol >= 0 else "🔴"
        log.info(f"{emoji} SELL {token_address[:14]}... | {reason} | PnL {pnl_pct:+.1%} ({pnl_sol:+.4f} SOL)")
        record = {
            "address":trade["address"],"buy_time":trade["buy_time"].isoformat(),
            "sell_time":datetime.now().isoformat(),"buy_price":trade["buy_price"],
            "exit_price":exit_price,"pnl_pct":round(pnl_pct,4),"pnl_sol":round(pnl_sol,6),
            "sol_spent":trade["sol_spent"],"peak_pnl":round(trade["peak_pnl"],4),
            "phase":trade["phase"],"reason":reason,"tx_buy":trade.get("tx_buy"),"tx_sell":sig
        }
        existing = []
        if os.path.exists("trades_log.json"):
            with open("trades_log.json") as f:
                try: existing = json.load(f)
                except: pass
        existing.append(record)
        with open("trades_log.json","w") as f:
            json.dump(existing, f, indent=2)
        with state_lock:
            open_trades.pop(token_address, None)
    except Exception as e:
        log.error(f"Sell failed {token_address}: {e}")

def price_monitor_loop():
    log.info("Price monitor started")
    while True:
        try:
            with state_lock:
                addrs = list(open_trades.keys())
            for addr in addrs:
                trade = open_trades.get(addr)
                if not trade or not trade["buy_price"]:
                    continue
                price = get_token_price_sol(addr)
                if price is None:
                    continue
                pnl = (price - trade["buy_price"]) / trade["buy_price"]
                sell, reason = should_sell(trade, pnl)
                if sell:
                    execute_sell(addr, reason)
        except Exception as e:
            log.error(f"Price monitor error: {e}")
        time.sleep(2)

def status_loop():
    while True:
        time.sleep(60)
        bal = get_wallet_balance_sol()
        log.info(f"Wallet:{bal:.4f} SOL | Open:{len(open_trades)} | Y:{trade_size_Y} SOL | Guard:{'ON' if daily_guard_triggered else 'OFF'}")
        async def resolve_room_to_token(room_id):
    if room_id in room_to_token:
        return room_to_token[room_id]
    try:
        headers = {
            "Origin":     "https://axiom.trade",
            "User-Agent": WS_HEADERS["User-Agent"],
            "Cookie":     CONFIG.get("cookies", ""),
            "Referer":    "https://axiom.trade/",
        }
        r = requests.get("https://api.axiom.trade/surge/surging", headers=headers, timeout=8)
        if r.status_code == 200:
            tokens = r.json()
            if isinstance(tokens, list):
                for t in tokens:
                    a  = t.get("address") or t.get("mint")
                    ri = t.get("room") or t.get("id") or t.get("roomId")
                    if a and ri:
                        room_to_token[ri] = a
                        token_to_room[a]  = ri
                if room_id in room_to_token:
                    return room_to_token[room_id]
        r2 = requests.get(f"https://api.axiom.trade/token/room/{room_id}", headers=headers, timeout=8)
        if r2.status_code == 200:
            data = r2.json()
            addr = data.get("address") or data.get("mint")
            if addr:
                room_to_token[room_id] = addr
                return addr
    except Exception as e:
        log.debug(f"resolve_room error: {e}")
    log.warning(f"Could not resolve room {room_id[:20]}...")
    return None

async def handle_message(msg):
    action = msg.get("action")
    mtype  = msg.get("type")
    if action == "join":
        room = msg.get("room", "")
        if room and room not in surging_rooms:
            surging_rooms.add(room)
            log.info(f"NEW room: {room[:24]}...")
            token = await resolve_room_to_token(room)
            if token:
                log.info(f"Surging coin: {token}")
                maybe_update_trade_size()
                Thread(target=execute_buy, args=(token,), daemon=True).start()
    elif action == "leave":
        surging_rooms.discard(msg.get("room", ""))
    elif mtype in ("surge", "tokens", "surging"):
        tokens = msg.get("tokens") or msg.get("data") or []
        for t in (tokens if isinstance(tokens, list) else []):
            a  = t.get("address") or t.get("mint")
            ri = t.get("room") or t.get("id") or t.get("roomId")
            if a and ri:
                room_to_token[ri] = a
                token_to_room[a]  = ri
    elif mtype == "token":
        a  = msg.get("address") or msg.get("mint")
        ri = msg.get("room") or msg.get("id")
        if a and ri:
            room_to_token[ri] = a
            token_to_room[a]  = ri

async def eucalyptus_feed():
    log.info(f"Connecting {WS_EUCALYPTUS}")
    while True:
        try:
            async with websockets.connect(
                WS_EUCALYPTUS,
                extra_headers=WS_HEADERS,
                ping_interval=25,
                ping_timeout=10,
                close_timeout=5
            ) as ws:
                log.info("WebSocket connected — watching Surge")
                await ws.send(json.dumps({"method":"ping"}))
                await ws.send(json.dumps({"method":"subscribe","channel":"surge"}))
                await ws.send(json.dumps({"method":"subscribe","channel":"surging"}))

                async def keepalive():
                    while True:
                        await asyncio.sleep(25)
                        try:
                            await ws.send(json.dumps({"method":"ping"}))
                        except Exception:
                            break
                asyncio.create_task(keepalive())

                async for raw in ws:
                    if isinstance(raw, bytes):
                        continue
                    try:
                        msg = json.loads(raw)
                        await handle_message(msg)
                    except Exception as e:
                        log.error(f"Message error: {e}")
        except Exception as e:
            log.error(f"WS disconnected: {e} — reconnecting in 5s")
            await asyncio.sleep(5)

async def main_async():
    global session_start_balance
    log.info("=" * 50)
    log.info("  AXIOM SURGE BOT — WebSocket Edition")
    log.info("=" * 50)
    session_start_balance = get_wallet_balance_sol()
    if not session_start_balance:
        log.error("Cannot fetch balance. Check config.json")
        return
    log.info(f"Starting balance: {session_start_balance:.4f} SOL")
    update_trade_size()
    Thread(target=price_monitor_loop, daemon=True).start()
    Thread(target=status_loop,        daemon=True).start()
    await eucalyptus_feed()

def main():
    asyncio.run(main_async())

if __name__ == "__main__":
    main()
    
