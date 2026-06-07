"""
AXIOM SURGE BOT — HEADLESS SELENIUM VERSION
Strategy: Surging tab only, 10% wallet per trade,
3-phase trailing stop, 45-min timeout, 50% daily risk guard
"""

import time
import json
import logging
import os
from datetime import datetime
from threading import Thread, Lock

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    NoSuchElementException, TimeoutException, StaleElementReferenceException
)
import requests

# ─────────────────────────────────────────────
# LOAD CONFIG
# ─────────────────────────────────────────────
with open("config.json", "r") as f:
    CONFIG = json.load(f)

EMAIL    = CONFIG["axiom_email"]
PASSWORD = CONFIG["axiom_password"]
RPC_URL  = CONFIG["solana_rpc_url"]
WALLET   = CONFIG["wallet_address"]

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
log_file = f"logs/bot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("AxiomBot")

# ─────────────────────────────────────────────
# GLOBAL STATE
# ─────────────────────────────────────────────
state_lock = Lock()
open_trades = {}
session_start_balance = None
trade_size_Y          = None
last_Y_update         = None
daily_guard_triggered = False

# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────
PHASE1_STOP      = -0.15
PHASE2_TRIGGER   =  0.10
PHASE3_TRIGGER   =  0.25
PHASE3_TRAIL     =  0.10
TIMEOUT_MINUTES  =  45
TIMEOUT_BAND     =  0.025
RISK_GUARD_PCT   =  0.50
TRADE_PCT        =  0.10
Y_UPDATE_HOURS   =  1
RESERVE_SOL      =  0.07

# ─────────────────────────────────────────────
# SOLANA HELPERS
# ─────────────────────────────────────────────
def get_wallet_balance_sol():
    try:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getBalance",
            "params": [WALLET]
        }
        r = requests.post(RPC_URL, json=payload, timeout=10)
        return r.json()["result"]["value"] / 1_000_000_000
    except Exception as e:
        log.error(f"Balance error: {e}")
        return None


def get_token_price(token_address):
    try:
        url = (
            f"https://price.jup.ag/v6/price?ids={token_address}"
            f"&vsToken=So11111111111111111111111111111111111111112"
        )
        r = requests.get(url, timeout=8)
        return float(r.json()["data"][token_address]["price"])
    except:
        return None


def get_token_balance(token_address):
    try:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [WALLET, {"mint": token_address}, {"encoding": "jsonParsed"}]
        }
        r    = requests.post(RPC_URL, json=payload, timeout=10)
        accs = r.json()["result"]["value"]
        if not accs:
            return 0
        return int(accs[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
    except Exception as e:
        log.error(f"Token balance error: {e}")
        return 0


# ─────────────────────────────────────────────
# TRADE SIZE
# ─────────────────────────────────────────────
def update_trade_size():
    global trade_size_Y, last_Y_update
    bal = get_wallet_balance_sol()
    if bal is None:
        return
    trade_size_Y = round(bal * TRADE_PCT, 4)
    last_Y_update = datetime.now()
    log.info(f"Trade size Y = {trade_size_Y} SOL (wallet: {bal:.4f} SOL)")


def maybe_update_trade_size():
    if last_Y_update is None:
        update_trade_size()
        return
    if (datetime.now() - last_Y_update).total_seconds() / 3600 >= Y_UPDATE_HOURS:
        update_trade_size()


# ─────────────────────────────────────────────
# RISK GUARD
# ─────────────────────────────────────────────
def check_risk_guard():
    global daily_guard_triggered
    if daily_guard_triggered:
        return True
    bal = get_wallet_balance_sol()
    if bal and session_start_balance and bal <= session_start_balance * (1 - RISK_GUARD_PCT):
        log.warning("RISK GUARD TRIGGERED — wallet dropped 50%. No new trades today.")
        daily_guard_triggered = True
        return True
    return False


def available_capital():
    bal = get_wallet_balance_sol()
    if bal is None:
        return 0
    return max(bal - len(open_trades) * trade_size_Y - RESERVE_SOL, 0)


# ─────────────────────────────────────────────
# PHASE LOGIC
# ─────────────────────────────────────────────
def should_sell(trade, pnl):
    if pnl > trade["peak_pnl"]:
        trade["peak_pnl"] = pnl

    phase = 1 if pnl < PHASE2_TRIGGER else 2 if pnl < PHASE3_TRIGGER else 3
    trade["phase"] = phase

    if phase == 1 and pnl <= PHASE1_STOP:
        return True, f"STOP LOSS Phase1 at {pnl:.1%}"
    if phase == 2 and pnl <= 0:
        return True, f"BREAK EVEN Phase2 at {pnl:.1%}"
    if phase == 3 and pnl <= trade["peak_pnl"] - PHASE3_TRAIL:
        return True, f"TRAILING STOP peak={trade['peak_pnl']:.1%} now={pnl:.1%}"

    age = (datetime.now() - trade["buy_time"]).total_seconds() / 60
    if age >= TIMEOUT_MINUTES and abs(pnl) <= TIMEOUT_BAND:
        return True, f"TIMEOUT {age:.0f}min at {pnl:.1%}"

    return False, None


# ─────────────────────────────────────────────
# BROWSER
# ─────────────────────────────────────────────
def create_driver():
    options = uc.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    driver = uc.Chrome(options=options)
    driver.implicitly_wait(10)
    log.info("Headless Chrome started")
    return driver


def login(driver):
    log.info("Navigating to Axiom Trade...")
    driver.get("https://axiom.trade")
    time.sleep(5)
    try:
        login_btn = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.XPATH,
                "//button[contains(text(),'Login') or contains(text(),'Sign in') or contains(text(),'Connect')]"
            ))
        )
        login_btn.click()
        time.sleep(2)

        email_field = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH,
                "//input[@type='email' or @name='email' or @placeholder='Email']"
            ))
        )
        email_field.clear()
        email_field.send_keys(EMAIL)

        pass_field = driver.find_element(By.XPATH, "//input[@type='password']")
        pass_field.clear()
        pass_field.send_keys(PASSWORD)

        submit = driver.find_element(By.XPATH,
            "//button[@type='submit' or contains(text(),'Login') or contains(text(),'Sign in')]"
        )
        submit.click()
        time.sleep(4)
        log.info("Logged in successfully")

    except TimeoutException:
        log.error("Login elements not found — Axiom may have changed their page")
        raise


def navigate_to_surging(driver):
    try:
        surge = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.XPATH,
                "//a[contains(text(),'Surge')] | //span[contains(text(),'Surge')]"
            ))
        )
        surge.click()
        time.sleep(2)

        surging_tab = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH,
                "//button[contains(text(),'Surging')] | //a[contains(text(),'Surging')]"
            ))
        )
        surging_tab.click()
        time.sleep(2)
        log.info("On Surge → Surging tab")

    except TimeoutException:
        log.error("Could not navigate to Surging tab")
        raise


# ─────────────────────────────────────────────
# COIN DETECTION
# ─────────────────────────────────────────────
def read_surging_coins(driver):
    coins = {}
    try:
        cards = driver.find_elements(By.XPATH,
            "//*[@data-address] | //*[contains(@class,'token-row')] | //*[contains(@class,'coin-card')]"
        )
        for card in cards:
            try:
                address = (
                    card.get_attribute("data-address") or
                    card.get_attribute("data-token") or
                    card.get_attribute("data-mint")
                )
                if not address or len(address) < 30:
                    continue
                coins[address] = {"name": "", "ticker": ""}
            except StaleElementReferenceException:
                continue
    except Exception as e:
        log.debug(f"read_surging_coins: {e}")
    return coins


# ─────────────────────────────────────────────
# BUY
# ─────────────────────────────────────────────
def execute_buy(token_address, coin_info):
    if check_risk_guard():
        log.warning("Risk guard active — skipping buy")
        return
    if available_capital() < trade_size_Y:
        log.warning("Not enough capital for new trade")
        return

    sol_mint = "So11111111111111111111111111111111111111112"
    amount_lamports = int(trade_size_Y * 1_000_000_000)

    try:
        quote_url = (
            f"https://quote-api.jup.ag/v6/quote"
            f"?inputMint={sol_mint}"
            f"&outputMint={token_address}"
            f"&amount={amount_lamports}"
            f"&slippageBps=1500"
        )
        quote = requests.get(quote_url, timeout=10).json()
        if "error" in quote:
            log.error(f"Quote error: {quote['error']}")
            return

        swap_r = requests.post(
            "https://quote-api.jup.ag/v6/swap",
            json={
                "quoteResponse": quote,
                "userPublicKey": WALLET,
                "wrapAndUnwrapSol": True,
                "prioritizationFeeLamports": 1000,
            },
            timeout=10
        )
        swap_data = swap_r.json()
        if "swapTransaction" not in swap_data:
            log.error(f"No swapTransaction: {swap_data}")
            return

        signed_tx = sign_and_send(swap_data["swapTransaction"])
        if signed_tx:
            buy_price = get_token_price(token_address)
            with state_lock:
                open_trades[token_address] = {
                    "address":   token_address,
                    "name":      coin_info.get("name", "?"),
                    "ticker":    coin_info.get("ticker", "?"),
                    "buy_time":  datetime.now(),
                    "buy_price": buy_price,
                    "sol_spent": trade_size_Y,
                    "peak_pnl":  0.0,
                    "phase":     1,
                    "tx_buy":    signed_tx,
                }
            log.info(f"BUY {token_address[:8]}... — {trade_size_Y} SOL")

    except Exception as e:
        log.error(f"Buy failed: {e}")


def sign_and_send(raw_tx_b64):
    try:
        import base64
        from solders.keypair import Keypair
        from solders.transaction import VersionedTransaction

        keypair  = Keypair.from_bytes(bytes(CONFIG["private_key_bytes"]))
        tx_bytes = base64.b64decode(raw_tx_b64)
        tx       = VersionedTransaction.from_bytes(tx_bytes)
        tx.sign([keypair])
        signed_b64 = base64.b64encode(bytes(tx)).decode()

        r = requests.post(RPC_URL, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "sendTransaction",
            "params": [signed_b64, {"encoding": "base64", "skipPreflight": False}]
        }, timeout=15)
        return r.json().get("result")
    except Exception as e:
        log.error(f"sign_and_send error: {e}")
        return None


# ─────────────────────────────────────────────
# SELL
# ─────────────────────────────────────────────
def execute_sell(token_address, reason):
    trade = open_trades.get(token_address)
    if not trade:
        return
    try:
        token_amount = get_token_balance(token_address)
        if not token_amount:
            with state_lock:
                open_trades.pop(token_address, None)
            return

        sol_mint = "So11111111111111111111111111111111111111112"
        quote = requests.get(
            f"https://quote-api.jup.ag/v6/quote"
            f"?inputMint={token_address}&outputMint={sol_mint}"
            f"&amount={token_amount}&slippageBps=1500",
            timeout=10
        ).json()

        if "error" in quote:
            log.error(f"Sell quote error: {quote['error']}")
            return

        swap_data = requests.post(
            "https://quote-api.jup.ag/v6/swap",
            json={
                "quoteResponse": quote,
                "userPublicKey": WALLET,
                "wrapAndUnwrapSol": True,
                "prioritizationFeeLamports": 1000,
            },
            timeout=10
        ).json()

        if "swapTransaction" not in swap_data:
            return

        signed_tx = sign_and_send(swap_data["swapTransaction"])
        if signed_tx:
            current_price = get_token_price(token_address)
            pnl_pct = 0
            if trade["buy_price"] and current_price:
                pnl_pct = (current_price - trade["buy_price"]) / trade["buy_price"]
            pnl_sol = trade["sol_spent"] * pnl_pct
            emoji = "🟢" if pnl_sol >= 0 else "🔴"
            log.info(
                f"{emoji} SELL {trade['ticker']} | {reason} | "
                f"PnL: {pnl_pct:+.1%} ({pnl_sol:+.4f} SOL) | "
                f"Peak: {trade['peak_pnl']:+.1%}"
            )
            save_trade(trade, current_price, pnl_pct, pnl_sol, reason, signed_tx)
            with state_lock:
                open_trades.pop(token_address, None)

    except Exception as e:
        log.error(f"Sell failed: {e}")


# ─────────────────────────────────────────────
# LOGGING TRADES
# ─────────────────────────────────────────────
def save_trade(trade, exit_price, pnl_pct, pnl_sol, reason, tx_sell):
    record = {
        "token":      trade["address"],
        "ticker":     trade["ticker"],
        "buy_time":   trade["buy_time"].isoformat(),
        "sell_time":  datetime.now().isoformat(),
        "buy_price":  trade["buy_price"],
        "exit_price": exit_price,
        "pnl_pct":    round(pnl_pct, 4),
        "pnl_sol":    round(pnl_sol, 6),
        "sol_spent":  trade["sol_spent"],
        "peak_pnl":   round(trade["peak_pnl"], 4),
        "phase":      trade["phase"],
        "reason":     reason,
        "tx_buy":     trade.get("tx_buy"),
        "tx_sell":    tx_sell,
    }
    path = "trades_log.json"
    existing = []
    if os.path.exists(path):
        with open(path, "r") as f:
            try:
                existing = json.load(f)
            except:
                pass
    existing.append(record)
    with open(path, "w") as f:
        json.dump(existing, f, indent=2)


# ─────────────────────────────────────────────
# MONITOR THREAD
# ─────────────────────────────────────────────
def monitor_open_trades():
    log.info("Price monitor thread started")
    while True:
        try:
            with state_lock:
                addresses = list(open_trades.keys())
            for addr in addresses:
                trade = open_trades.get(addr)
                if not trade:
                    continue
                current_price = get_token_price(addr)
                if current_price is None or trade["buy_price"] is None:
                    continue
                pnl = (current_price - trade["buy_price"]) / trade["buy_price"]
                sell, reason = should_sell(trade, pnl)
                if sell:
                    execute_sell(addr, reason)
        except Exception as e:
            log.error(f"Monitor error: {e}")
        time.sleep(2)


def print_status():
    while True:
        time.sleep(60)
        bal = get_wallet_balance_sol()
        log.info(
            f"STATUS | Wallet: {bal:.4f} SOL | "
            f"Open: {len(open_trades)} trades | "
            f"Y: {trade_size_Y} SOL | "
            f"Guard: {'TRIGGERED' if daily_guard_triggered else 'OK'}"
        )


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    global session_start_balance

    log.info("=" * 50)
    log.info("  AXIOM SURGE BOT — Starting")
    log.info("=" * 50)

    session_start_balance = get_wallet_balance_sol()
    if session_start_balance is None:
        log.error("Cannot fetch wallet balance. Check config.json")
        return
    log.info(f"Starting wallet: {session_start_balance:.4f} SOL")

    update_trade_size()

    Thread(target=monitor_open_trades, daemon=True).start()
    Thread(target=print_status, daemon=True).start()

    driver = create_driver()
    login(driver)
    navigate_to_surging(driver)

    previous_coins = {}
    log.info("Watching Surging tab for new coins...")

    while True:
        try:
            maybe_update_trade_size()
            current_coins = read_surging_coins(driver)
            new_addresses = set(current_coins.keys()) - set(previous_coins.keys())
            for addr in new_addresses:
                if addr not in open_trades:
                    log.info(f"NEW COIN: {addr[:12]}...")
                    execute_buy(addr, current_coins[addr])
            previous_coins = current_coins

        except Exception as e:
            log.error(f"Main loop error: {e}")
            try:
                driver.refresh()
                time.sleep(5)
                navigate_to_surging(driver)
            except:
                log.error("Restarting browser...")
                try:
                    driver.quit()
                except:
                    pass
                time.sleep(10)
                driver = create_driver()
                login(driver)
                navigate_to_surging(driver)

        time.sleep(0.5)


if __name__ == "__main__":
    main()
