
#!/usr/bin/env python3
"""
Dr. Fraudsworth — Laboratory Assistant Bot v6

Changes from v5:
- v0 transaction parsing: loadedAddresses support
- Sell detection with tax-based threshold
- Simplified image slots (one per event category)
- Updated message templates with staker SOL display
- Lower default buy threshold (0.1 SOL)
- Better error logging in async tasks
"""

import asyncio
import base64
import io
import json
import logging
import struct
import time
import traceback
import aiohttp

from collections import OrderedDict
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
from matplotlib.colors import LinearSegmentedColormap

from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
from telegram.error import TelegramError

import db
from config import (
    TELEGRAM_TOKEN, SUPER_ADMIN_ID,
    HELIUS_API_KEYS,
    CRIME_MINT, FRAUD_MINT, PROFIT_MINT,
    CRIME_WSOL_VAULT, FRAUD_WSOL_VAULT,
    CRIME_TOKEN_VAULT, FRAUD_TOKEN_VAULT,
    CRIME_POOL_STATE, FRAUD_POOL_STATE,
    CARNAGE_VAULT, VAULT_CRIME, VAULT_FRAUD,
    WSOL_INTERMEDIARY, TREASURY,
    STAKING_ESCROW,
    VAULT_PROFIT, STAKE_POOL, STAKING_PROFIT_VAULT,
    EPOCH_STATE, STAKING_PROGRAM, TAX_PROGRAM,
    TOTAL_SUPPLY, PROFIT_SUPPLY,
    EPOCH_OFFSET_CURRENT_EPOCH, EPOCH_OFFSET_CHEAP_SIDE,
    EPOCH_OFFSET_CRIME_BUY_TAX, EPOCH_OFFSET_CRIME_SELL_TAX,
    EPOCH_OFFSET_FRAUD_BUY_TAX, EPOCH_OFFSET_FRAUD_SELL_TAX,
    EPOCH_OFFSET_CARNAGE_PENDING, EPOCH_OFFSET_CARNAGE_TARGET,
    POOL_OFFSET_RESERVE_A, POOL_OFFSET_RESERVE_B,
    POOLS, BASE_DECIMALS, ARB_OPERATOR,
)

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('lab_assistant.log'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ============================================================
# GLOBALS
# ============================================================
sol_price = 89.0
hype_price = 0.0   # HYPE/USD, refreshed alongside SOL price for HYPE-pool valuation
pending_image_updates = {}
processed_tax_sigs = OrderedDict()
processed_stake_sigs = OrderedDict()

# ============================================================
# HELIUS RPC FAILOVER
# ============================================================
_rpc_key_index = 0
_rpc_consecutive_failures = 0
_rpc_key_switched_at = 0.0

def get_rpc_url() -> str:
    """Return the current Helius RPC HTTP URL."""
    return f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEYS[_rpc_key_index]}"

def rotate_rpc_key(reason: str = ""):
    """Switch to the next Helius API key."""
    global _rpc_key_index, _rpc_consecutive_failures, _rpc_key_switched_at
    old_idx = _rpc_key_index
    _rpc_key_index = (_rpc_key_index + 1) % len(HELIUS_API_KEYS)
    _rpc_consecutive_failures = 0
    _rpc_key_switched_at = time.time()
    old_key = HELIUS_API_KEYS[old_idx][-8:]
    new_key = HELIUS_API_KEYS[_rpc_key_index][-8:]
    log.warning(f"RPC key rotated: ...{old_key} → ...{new_key} ({reason})")

def rpc_check_http(status: int) -> bool:
    """Check HTTP status before parsing JSON. Returns True (= should retry) on 429/500/503."""
    global _rpc_consecutive_failures
    if status in (429, 500, 503):
        _rpc_consecutive_failures += 1
        if _rpc_consecutive_failures >= 2 and len(HELIUS_API_KEYS) > 1:
            rotate_rpc_key(f"HTTP {status}")
        return True
    return False

def rpc_check_error(resp_json: dict) -> bool:
    """Check RPC response for rate-limit errors. Returns True if error detected and key rotated."""
    global _rpc_consecutive_failures
    err = resp_json.get("error")
    if not err:
        _rpc_consecutive_failures = 0
        return False
    code = err.get("code", 0)
    msg = err.get("message", "")
    if code == -32429 or code == 429 or "max usage" in msg.lower() or "too many" in msg.lower():
        _rpc_consecutive_failures += 1
        log.warning(f"RPC rate-limited (code={code}): {msg} (failures={_rpc_consecutive_failures})")
        if _rpc_consecutive_failures >= 2 and len(HELIUS_API_KEYS) > 1:
            rotate_rpc_key(f"rate-limited: {msg}")
        return True
    if code != 0:
        log.warning(f"RPC error (code={code}): {msg}")
    return False

# Cached epoch state (updated by monitor_epochs every 60s)
cached_epoch_state = {}

# Live valuations, recomputed every 60s from all-pool on-chain prices.
# cached_fdv holds *market caps* (name kept for backwards compatibility);
# cached_fdv_full holds fully-diluted valuations; cached_prices the unit prices.
cached_fdv = {"crime": 0.0, "fraud": 0.0, "profit": 0.0}
cached_fdv_full = {"crime": 0.0, "fraud": 0.0, "profit": 0.0}
cached_prices = {"crime_usd": 0.0, "fraud_usd": 0.0, "profit_usd": 0.0}

# Activity tracking for /health
activity = {
    "tax_txns_this_epoch":    0,
    "staking_txns_this_epoch": 0,
    "last_tax_activity":      None,
    "last_staking_activity":  None,
    "last_epoch_update":      None,
    "last_sol_price_update":  None,
    "current_epoch":          None,
    "last_cheap_side":        None,
    "last_escrow_balance":    None,
}

# ============================================================
# HELPERS
# ============================================================
CHAT_ID = -1003765301360

def is_dm(update: Update) -> bool:
    return update.effective_chat.type == "private"

def solscan_tx(sig: str) -> str:
    if not sig or sig == "PREVIEW":
        return "https://solscan.io"
    return f"https://solscan.io/tx/{sig}"

FYI_API = "https://www.fraudsworth.fyi/api"

async def fyi_get(path: str) -> dict | None:
    """Fetch JSON from fraudsworth.fyi API."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{FYI_API}{path}", timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return await r.json()
    except Exception as e:
        log.warning(f"fyi_get({path}) failed: {e}")
    return None

def fmt_sol(amount: float) -> str:
    return f"{amount:.3f} SOL"

def fmt_usd(amount: float) -> str:
    if amount >= 1_000_000:
        return f"${amount/1_000_000:.2f}M"
    if amount >= 1_000:
        return f"${amount/1_000:.1f}K"
    return f"${amount:.0f}"

def bps_to_pct(bps: int) -> str:
    return f"{bps/100:.0f}%"

EPOCH_DISPLAY_OFFSET = 428

def display_epoch(raw_epoch) -> str:
    """Convert raw on-chain epoch number to display number (first epoch was 429 on-chain → 1 display)."""
    try:
        return str(int(raw_epoch) - EPOCH_DISPLAY_OFFSET)
    except (TypeError, ValueError):
        return "?"

def get_buy_tier(sol: float) -> str:
    if sol >= db.get_setting("mega_whale_sol"):  return "mega"
    if sol >= db.get_setting("whale_buy_sol"):   return "whale"
    if sol >= db.get_setting("medium_buy_sol"):  return "medium"
    return "small"

def time_ago(ts) -> str:
    if ts is None:
        return "never"
    diff = time.time() - ts
    if diff < 60:
        return f"{int(diff)}s ago"
    if diff < 3600:
        return f"{int(diff/60)}m ago"
    return f"{int(diff/3600)}h ago"

# ============================================================
# RPC HELPERS
# ============================================================
async def rpc_get_account(pubkey: str, encoding: str = "base64"):
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getAccountInfo",
        "params": [pubkey, {"encoding": encoding}]
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(get_rpc_url(), json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if rpc_check_http(r.status):
                    return None
                resp = await r.json()
                if rpc_check_error(resp):
                    return None
                return resp.get("result", {}).get("value")
    except Exception as e:
        log.error(f"getAccountInfo {pubkey[:8]}: {e}")
        return None

async def rpc_get_token_balance(pubkey: str) -> float:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenAccountBalance", "params": [pubkey]}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(get_rpc_url(), json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if rpc_check_http(r.status):
                    return 0.0
                resp = await r.json()
                if rpc_check_error(resp):
                    return 0.0
                val = resp.get("result", {}).get("value", {})
                return float(val.get("uiAmount") or 0)
    except Exception:
        return 0.0

async def rpc_get_multiple_accounts(pubkeys: list) -> list:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
               "params": [pubkeys, {"encoding": "base64"}]}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(get_rpc_url(), json=payload, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if rpc_check_http(r.status):
                    return []
                resp = await r.json()
                if rpc_check_error(resp):
                    return []
                return resp.get("result", {}).get("value", []) or []
    except Exception as e:
        log.warning(f"getMultipleAccounts error: {e}")
        return []

async def rpc_get_token_supply(mint: str) -> float:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [mint]}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(get_rpc_url(), json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if rpc_check_http(r.status):
                    return 0.0
                resp = await r.json()
                if rpc_check_error(resp):
                    return 0.0
                return float(resp.get("result", {}).get("value", {}).get("uiAmount") or 0)
    except Exception:
        return 0.0

async def rpc_get_transaction(sig: str):
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTransaction",
        "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                         "commitment": "confirmed"}]
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(get_rpc_url(), json=payload, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if rpc_check_http(r.status):
                    return None
                resp = await r.json()
                if rpc_check_error(resp):
                    return None
                result = resp.get("result")
                if result is None:
                    log.warning(f"getTransaction returned null for {sig[:20]}")
                return result
    except Exception as e:
        log.warning(f"getTransaction failed for {sig[:20]}: {e}")
        return None

# ============================================================
# EPOCH STATE PARSER
# ============================================================
def parse_epoch_state(data: bytes) -> dict:
    try:
        d = data[8:]  # Skip 8-byte discriminator
        epoch_num     = struct.unpack_from("<I", d, EPOCH_OFFSET_CURRENT_EPOCH)[0]
        cheap_side    = struct.unpack_from("B",  d, EPOCH_OFFSET_CHEAP_SIDE)[0]
        crime_buy     = struct.unpack_from("<H", d, EPOCH_OFFSET_CRIME_BUY_TAX)[0]
        crime_sell    = struct.unpack_from("<H", d, EPOCH_OFFSET_CRIME_SELL_TAX)[0]
        fraud_buy     = struct.unpack_from("<H", d, EPOCH_OFFSET_FRAUD_BUY_TAX)[0]
        fraud_sell    = struct.unpack_from("<H", d, EPOCH_OFFSET_FRAUD_SELL_TAX)[0]
        carnage_pend  = struct.unpack_from("B",  d, EPOCH_OFFSET_CARNAGE_PENDING)[0]
        carnage_tgt   = struct.unpack_from("B",  d, EPOCH_OFFSET_CARNAGE_TARGET)[0]

        return {
            "epoch":          epoch_num,
            "cheap_side":     "CRIME" if cheap_side == 0 else "FRAUD",
            "crime_buy_bps":  crime_buy,
            "crime_sell_bps": crime_sell,
            "fraud_buy_bps":  fraud_buy,
            "fraud_sell_bps": fraud_sell,
            "carnage_pending": carnage_pend == 1,
            "carnage_target":  "CRIME" if carnage_tgt == 0 else "FRAUD",
        }
    except Exception as e:
        log.error(f"parse_epoch_state error: {e}")
        return {}

async def fetch_epoch_state() -> dict:
    acct = await rpc_get_account(EPOCH_STATE, "base64")
    if not acct:
        return {}
    raw = base64.b64decode(acct["data"][0])
    return parse_epoch_state(raw)

# ============================================================
# POOL RESERVE READER
# ============================================================
async def fetch_pool_reserves(pool_state_pubkey: str) -> tuple:
    acct = await rpc_get_account(pool_state_pubkey, "base64")
    if not acct:
        return (0, 0)
    raw = base64.b64decode(acct["data"][0])
    d = raw[8:]
    try:
        reserve_a = struct.unpack_from("<Q", d, POOL_OFFSET_RESERVE_A)[0]
        reserve_b = struct.unpack_from("<Q", d, POOL_OFFSET_RESERVE_B)[0]
        return (reserve_a, reserve_b)
    except Exception as e:
        log.error(f"fetch_pool_reserves error: {e}")
        return (0, 0)

async def get_fdv_from_pool(pool_state_pubkey: str, token_decimals: int = 6) -> float:
    reserve_sol_lam, reserve_tok_raw = await fetch_pool_reserves(pool_state_pubkey)
    if reserve_sol_lam == 0 or reserve_tok_raw == 0:
        return 0.0
    reserve_sol = reserve_sol_lam / 1e9
    reserve_tok = reserve_tok_raw / (10 ** token_decimals)
    price_per_token = (reserve_sol * sol_price) / reserve_tok
    return price_per_token * TOTAL_SUPPLY

async def estimate_tokens_from_sol(sol_amount: float, pool_state_pubkey: str, token_decimals: int = 6) -> float:
    reserve_sol_lam, reserve_tok_raw = await fetch_pool_reserves(pool_state_pubkey)
    if reserve_sol_lam == 0 or reserve_tok_raw == 0:
        return 0.0
    reserve_sol = reserve_sol_lam / 1e9
    reserve_tok = reserve_tok_raw / (10 ** token_decimals)
    tokens_out = reserve_tok * sol_amount / (reserve_sol + sol_amount)
    return tokens_out

# ============================================================
# SOL / HYPE PRICE UPDATER
# ============================================================
async def update_sol_price():
    global sol_price, hype_price
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://api.coingecko.com/api/v3/simple/price?ids=solana,hyperliquid&vs_currencies=usd",
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as r:
                    data = await r.json()
                    sol_price = data["solana"]["usd"]
                    hype_price = data.get("hyperliquid", {}).get("usd", hype_price)
                    activity["last_sol_price_update"] = time.time()
                    log.info(f"SOL: ${sol_price} · HYPE: ${hype_price}")
        except Exception as e:
            log.warning(f"SOL price update failed: {e}")
        await asyncio.sleep(300)

def quote_to_sol(quote: str, amount: float) -> float:
    """Convert a native quote-asset amount into a SOL-equivalent value.
    USDC is treated as USD; HYPE uses the live CoinGecko price."""
    if quote == "SOL":
        return amount
    if sol_price <= 0:
        return 0.0
    if quote == "USDC":
        return amount / sol_price
    if quote == "HYPE":
        return amount * hype_price / sol_price
    return 0.0

def fmt_trade_size(quote: str, quote_amount: float, sol_equiv: float) -> str:
    """Header display for a trade. SOL pools read exactly as before;
    USDC/HYPE pools show the native amount plus the SOL-equivalent."""
    if quote == "SOL":
        return fmt_sol(sol_equiv)
    return f"{quote_amount:,.2f} {quote} (≈{sol_equiv:.3f} SOL)"

# Each PROFIT represents (CRIME_supply / PROFIT_supply) CRIME + the same in
# FRAUD, so PROFIT price = ratio × (CRIME price + FRAUD price). Verified exact
# against the site's own figures. Ratio = 1B / 20M = 50.
PROFIT_RATIO = TOTAL_SUPPLY / PROFIT_SUPPLY
SPL_AMOUNT_OFFSET = 64  # u64 token amount inside an SPL token account

def _quote_usd(quote: str) -> float:
    return {"SOL": sol_price, "USDC": 1.0, "HYPE": hype_price}.get(quote, 0.0)

async def compute_pool_valuations() -> dict:
    """Liquidity-weighted CRIME/FRAUD prices across ALL pools (SOL, USDC, HYPE),
    read straight from on-chain vault balances, plus the derived PROFIT
    valuation. This replaces the single-pool API price that made mcap look off
    once the factory went multi-pool. Returns {} if the on-chain read fails."""
    vaults = []
    for p in POOLS:
        vaults += [p["base_vault"], p["quote_vault"]]
    accts = await rpc_get_multiple_accounts(vaults)
    if len(accts) != len(vaults):
        return {}
    amts = {}
    for v, a in zip(vaults, accts):
        try:
            amts[v] = struct.unpack_from("<Q", base64.b64decode(a["data"][0]), SPL_AMOUNT_OFFSET)[0]
        except Exception:
            amts[v] = 0

    num = {"CRIME": 0.0, "FRAUD": 0.0}
    den = {"CRIME": 0.0, "FRAUD": 0.0}
    for p in POOLS:
        bal_b = amts.get(p["base_vault"], 0) / (10 ** BASE_DECIMALS)
        bal_q = amts.get(p["quote_vault"], 0) / (10 ** p["quote_decimals"])
        qusd = _quote_usd(p["quote"])
        if bal_b <= 0 or bal_q <= 0 or qusd <= 0:
            continue
        price = (bal_q * qusd) / bal_b          # base price in USD, this pool
        liq = 2 * bal_q * qusd                   # pool liquidity in USD (both sides)
        num[p["base"]] += price * liq
        den[p["base"]] += liq

    if den["CRIME"] <= 0 or den["FRAUD"] <= 0:
        return {}
    crime_usd = num["CRIME"] / den["CRIME"]
    fraud_usd = num["FRAUD"] / den["FRAUD"]
    profit_usd = PROFIT_RATIO * (crime_usd + fraud_usd)

    # CRIME/FRAUD market cap uses live mint supply (genesis − burned), read
    # on-chain; FDV uses the fixed genesis supply.
    crime_circ = await rpc_get_token_supply(CRIME_MINT) or TOTAL_SUPPLY
    fraud_circ = await rpc_get_token_supply(FRAUD_MINT) or TOTAL_SUPPLY

    # PROFIT circulating / max depend on off-chain vault + foundation balances,
    # so the supply API remains the source for those (the *price* was the weak
    # link, and that's now local). Falls back to the fixed supply if unavailable.
    profit_circ = profit_max = PROFIT_SUPPLY
    sup = await fyi_get("/supply/current")
    if sup and sup.get("profit"):
        profit_circ = sup["profit"].get("circulating", PROFIT_SUPPLY)
        profit_max = sup["profit"].get("maxCirculating", PROFIT_SUPPLY)

    return {
        "prices": {"crime_usd": crime_usd, "fraud_usd": fraud_usd, "profit_usd": profit_usd},
        "mcap": {"crime": crime_circ * crime_usd,
                 "fraud": fraud_circ * fraud_usd,
                 "profit": profit_circ * profit_usd},
        "fdv":  {"crime": TOTAL_SUPPLY * crime_usd,
                 "fraud": TOTAL_SUPPLY * fraud_usd,
                 "profit": profit_max * profit_usd},
    }

async def compute_pool_liquidity() -> dict:
    """Per-pool reserves and USD TVL across ALL pools, grouped by base token.
    Read straight from on-chain vaults so USDC and HYPE pools are included
    (the fraudsworth.fyi /liquidity endpoint only reports the SOL pools)."""
    vaults = []
    for p in POOLS:
        vaults += [p["base_vault"], p["quote_vault"]]
    accts = await rpc_get_multiple_accounts(vaults)
    if len(accts) != len(vaults):
        return {}
    amts = {}
    for v, a in zip(vaults, accts):
        try:
            amts[v] = struct.unpack_from("<Q", base64.b64decode(a["data"][0]), SPL_AMOUNT_OFFSET)[0]
        except Exception:
            amts[v] = 0

    out = {"CRIME": {"pools": [], "tvl": 0.0}, "FRAUD": {"pools": [], "tvl": 0.0}}
    for p in POOLS:
        bal_b = amts.get(p["base_vault"], 0) / (10 ** BASE_DECIMALS)
        bal_q = amts.get(p["quote_vault"], 0) / (10 ** p["quote_decimals"])
        tvl = 2 * bal_q * _quote_usd(p["quote"])
        out[p["base"]]["pools"].append(
            {"quote": p["quote"], "quote_bal": bal_q, "base_bal": bal_b, "tvl": tvl})
        out[p["base"]]["tvl"] += tvl
    return out

async def update_fdv_cache():
    """Recompute CRIME/FRAUD/PROFIT market cap & FDV every 60s from live,
    all-pool on-chain prices. Falls back to the fraudsworth.fyi API only if
    the on-chain read fails."""
    global cached_fdv, cached_fdv_full, cached_prices
    # Wait for the first real SOL/HYPE price before valuing anything, otherwise
    # the first cycle would price the SOL/HYPE pools off the default placeholder.
    for _ in range(30):
        if activity.get("last_sol_price_update"):
            break
        await asyncio.sleep(0.5)
    while True:
        try:
            v = await compute_pool_valuations()
            if v:
                cached_fdv = v["mcap"]
                cached_fdv_full = v["fdv"]
                cached_prices = v["prices"]
                log.info(f"Valuation: CRIME mc={cached_fdv['crime']:.0f}/fdv={cached_fdv_full['crime']:.0f} "
                         f"FRAUD mc={cached_fdv['fraud']:.0f} PROFIT mc={cached_fdv['profit']:.0f}")
            else:
                data = await fyi_get("/solana/pool-state")
                if data:
                    cached_fdv = {
                        "crime": data.get("crime", {}).get("marketCapUsd", 0.0),
                        "fraud": data.get("fraud", {}).get("marketCapUsd", 0.0),
                        "profit": data.get("profit", {}).get("marketCapUsd", 0.0),
                    }
                    cached_fdv_full = {
                        "crime": cached_fdv["crime"],
                        "fraud": cached_fdv["fraud"],
                        "profit": data.get("profit", {}).get("fdvUsd", cached_fdv["profit"]),
                    }
                    log.warning("Valuation: on-chain read failed — used API fallback")
        except Exception as e:
            log.warning(f"FDV cache update failed: {e}")
        await asyncio.sleep(60)

# ============================================================
# TELEGRAM SENDER
# ============================================================
async def send_alert(bot: Bot, message: str, event_type: str = None,
                     sol_amount: float = 0, chat_id: int = None):
    target_chat = chat_id or CHAT_ID
    image = db.get_image(event_type) if event_type else None

    try:
        sent_msg = None
        if image:
            file_id, file_type = image
            if file_type == "animation":
                sent_msg = await bot.send_animation(
                    chat_id=target_chat,
                    animation=file_id,
                    caption=message,
                    parse_mode=ParseMode.HTML
                )
            else:
                sent_msg = await bot.send_photo(
                    chat_id=target_chat,
                    photo=file_id,
                    caption=message,
                    parse_mode=ParseMode.HTML
                )
        else:
            sent_msg = await bot.send_message(
                chat_id=target_chat,
                text=message,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )

        if event_type:
            db.increment_stat(event_type, sol_amount)

        log.info(f"Alert [{event_type}]: {message[:80]}...")
        return sent_msg

    except TelegramError as e:
        log.error(f"Telegram error [{event_type}]: {e}")
    except Exception as e:
        log.error(f"Send error [{event_type}]: {e}")
    return None

# ============================================================
# ALERT BUILDERS — v6 templates
# ============================================================
async def fire_crime_buy(bot: Bot, sol: float, tokens: float, mcap: float, sig: str,
                         quote: str = "SOL", quote_amount: float = 0.0, fdv: float = 0.0):
    if not db.get_setting("alerts_crime_buys"):
        return

    tier = get_buy_tier(sol)
    tx_url = solscan_tx(sig)
    size_disp = fmt_trade_size(quote, quote_amount, sol)
    pool_line = "" if quote == "SOL" else f"  <i>via {quote} pool</i>\n"

    # Read fresh epoch state for accurate tax rate
    epoch = await fetch_epoch_state() or cached_epoch_state
    buy_tax_bps = epoch.get("crime_buy_bps", 0)
    tax_pct = bps_to_pct(buy_tax_bps)
    stakers_sol = sol * (buy_tax_bps / 10000) * 0.71

    if tier == "mega":
        msg = (
            f"💥 <b>THE LABORATORY SHAKES</b>\n\n"
            f"🏦🏦🏦🏦🏦\n"
            f"<b>CRIME — {size_disp}</b>\n"
            f"  <code>{tokens:,.0f} CRIME</code> ABSORBED\n"
            f"{pool_line}"
            f"  MC <code>{fmt_usd(mcap)}</code> · FDV <code>{fmt_usd(fdv)}</code>\n"
            f"  Tax <code>{tax_pct}</code> · <code>{stakers_sol:.4f} SOL</code> EXTRACTED\n\n"
            f"<i>SOMEBODY SEDATE THE DOCTOR.</i>\n"
            f"<i>HE HAS TAKEN LEAVE OF HIS SENSES.</i>\n\n"
            f"<a href='{tx_url}'>📋 Txn</a> · <a href='https://fraudsworth.fun'>Buy CRIME</a>"
        )
    elif tier == "whale":
        msg = (
            f"⚡ <b>SIGNIFICANT EXPERIMENT DETECTED</b>\n\n"
            f"🏦🏦🏦\n"
            f"<b>CRIME — {size_disp}</b>\n"
            f"  <code>{tokens:,.0f} CRIME</code> SEIZED\n"
            f"{pool_line}"
            f"  MC <code>{fmt_usd(mcap)}</code> · FDV <code>{fmt_usd(fdv)}</code>\n"
            f"  Tax <code>{tax_pct}</code> · <code>{stakers_sol:.4f} SOL</code> extracted for stakers\n\n"
            f"<i>THE DOCTOR CANNOT CONTAIN HIMSELF.</i>\n\n"
            f"<a href='{tx_url}'>📋 Txn</a> · <a href='https://fraudsworth.fun'>Buy CRIME</a>"
        )
    elif tier == "medium":
        msg = (
            f"🧪 <b>The laboratory stirs...</b>\n\n"
            f"💰💰💰\n"
            f"<b>CRIME</b> — <code>{size_disp}</code>\n"
            f"  <code>{tokens:,.0f} CRIME</code> consumed\n"
            f"{pool_line}"
            f"  MC <code>{fmt_usd(mcap)}</code> · FDV <code>{fmt_usd(fdv)}</code>\n"
            f"  Tax <code>{tax_pct}</code> · <code>{stakers_sol:.4f} SOL</code> to stakers\n\n"
            f"<i>The Doctor cannot contain himself.</i>\n\n"
            f"<a href='{tx_url}'>📋 Txn</a> · <a href='https://fraudsworth.fun'>Buy CRIME</a>"
        )
    else:
        msg = (
            f"⚗️ <b>A new experiment begins...</b>\n\n"
            f"💵💵💵\n"
            f"<b>CRIME</b> — <code>{size_disp}</code>\n"
            f"  <code>{tokens:,.0f} CRIME</code> devoured\n"
            f"{pool_line}"
            f"  MC <code>{fmt_usd(mcap)}</code> · FDV <code>{fmt_usd(fdv)}</code>\n"
            f"  Tax <code>{tax_pct}</code> · <code>{stakers_sol:.4f} SOL</code> to stakers\n\n"
            f"<i>Another soul enters the laboratory.</i>\n\n"
            f"<a href='{tx_url}'>📋 Txn</a> · <a href='https://fraudsworth.fun'>Buy CRIME</a>"
        )

    await send_alert(bot, msg, "crime_buy", sol)

async def fire_fraud_buy(bot: Bot, sol: float, tokens: float, mcap: float, sig: str,
                         quote: str = "SOL", quote_amount: float = 0.0, fdv: float = 0.0):
    if not db.get_setting("alerts_fraud_buys"):
        return

    tier = get_buy_tier(sol)
    tx_url = solscan_tx(sig)
    size_disp = fmt_trade_size(quote, quote_amount, sol)
    pool_line = "" if quote == "SOL" else f"  <i>via {quote} pool</i>\n"

    # Read fresh epoch state for accurate tax rate
    epoch = await fetch_epoch_state() or cached_epoch_state
    buy_tax_bps = epoch.get("fraud_buy_bps", 0)
    tax_pct = bps_to_pct(buy_tax_bps)
    stakers_sol = sol * (buy_tax_bps / 10000) * 0.71

    if tier == "mega":
        msg = (
            f"💥 <b>THE LABORATORY IS ON FIRE</b>\n\n"
            f"🏦🏦🏦🏦🏦\n"
            f"<b>FRAUD — {size_disp}</b>\n"
            f"  <code>{tokens:,.0f} FRAUD</code> ABSORBED\n"
            f"{pool_line}"
            f"  MC <code>{fmt_usd(mcap)}</code> · FDV <code>{fmt_usd(fdv)}</code>\n"
            f"  Tax <code>{tax_pct}</code> · <code>{stakers_sol:.4f} SOL</code> EXTRACTED\n\n"
            f"<i>THE DOCTOR REQUIRES SMELLING SALTS.</i>\n"
            f"<i>FRAUD POOL WILL NEVER BE THE SAME.</i>\n\n"
            f"<a href='{tx_url}'>📋 Txn</a> · <a href='https://fraudsworth.fun'>Buy FRAUD</a>"
        )
    elif tier == "whale":
        msg = (
            f"⚡ <b>THE OTHER SIDE AWAKENS</b>\n\n"
            f"🏦🏦🏦\n"
            f"<b>FRAUD — {size_disp}</b>\n"
            f"  <code>{tokens:,.0f} FRAUD</code> SEIZED\n"
            f"{pool_line}"
            f"  MC <code>{fmt_usd(mcap)}</code> · FDV <code>{fmt_usd(fdv)}</code>\n"
            f"  Tax <code>{tax_pct}</code> · <code>{stakers_sol:.4f} SOL</code> extracted for stakers\n\n"
            f"<i>THE DOCTOR RECALIBRATES FRANTICALLY.</i>\n\n"
            f"<a href='{tx_url}'>📋 Txn</a> · <a href='https://fraudsworth.fun'>Buy FRAUD</a>"
        )
    elif tier == "medium":
        msg = (
            f"🧪 <b>The other side stirs...</b>\n\n"
            f"💰💰💰\n"
            f"<b>FRAUD</b> — <code>{size_disp}</code>\n"
            f"  <code>{tokens:,.0f} FRAUD</code> consumed\n"
            f"{pool_line}"
            f"  MC <code>{fmt_usd(mcap)}</code> · FDV <code>{fmt_usd(fdv)}</code>\n"
            f"  Tax <code>{tax_pct}</code> · <code>{stakers_sol:.4f} SOL</code> to stakers\n\n"
            f"<i>The Doctor cross-references his notes.</i>\n\n"
            f"<a href='{tx_url}'>📋 Txn</a> · <a href='https://fraudsworth.fun'>Buy FRAUD</a>"
        )
    else:
        msg = (
            f"⚗️ <b>The coin flips...</b>\n\n"
            f"💵💵💵\n"
            f"<b>FRAUD</b> — <code>{size_disp}</code>\n"
            f"  <code>{tokens:,.0f} FRAUD</code> absorbed\n"
            f"{pool_line}"
            f"  MC <code>{fmt_usd(mcap)}</code> · FDV <code>{fmt_usd(fdv)}</code>\n"
            f"  Tax <code>{tax_pct}</code> · <code>{stakers_sol:.4f} SOL</code> to stakers\n\n"
            f"<i>The other side of the coin turns.</i>\n\n"
            f"<a href='{tx_url}'>📋 Txn</a> · <a href='https://fraudsworth.fun'>Buy FRAUD</a>"
        )

    await send_alert(bot, msg, "fraud_buy", sol)

async def fire_big_sell(bot: Bot, sol: float, tokens: float, token_name: str, tax_sol: float, sig: str,
                        quote: str = "SOL", quote_amount: float = 0.0):
    if not db.get_setting("alerts_sells"):
        return

    tx_url = solscan_tx(sig)
    exit_disp = fmt_trade_size(quote, quote_amount, sol)
    pool_line = "" if quote == "SOL" else f"  <i>via {quote} pool</i>\n"

    msg = (
        f"💀 <b>SELL DETECTED</b>\n\n"
        f"  <code>{tokens:,.0f} {token_name}</code> abandoned\n"
        f"  <code>{exit_disp}</code> exits the building\n"
        f"{pool_line}"
        f"  <b><code>{tax_sol:.4f} SOL</code> TAXED. GONE. OURS.</b>\n\n"
        f"<i>71% to stakers · 24% to Carnage · 5% treasury</i>\n"
        f"<i>THANKS FOR THE TAXES. 🖕🖕</i>\n\n"
        f"<a href='{tx_url}'>📋 Txn</a>"
    )

    await send_alert(bot, msg, "big_sell", sol)

async def fire_profit_stake(bot: Bot, tokens: float, total_staked: float, sig: str):
    if not db.get_setting("alerts_profit_stakes"):
        return

    # Use max circulating supply for a more meaningful staked %
    supply_data = await fyi_get("/supply/current")
    max_circ = PROFIT_SUPPLY  # fallback
    if supply_data and supply_data.get("profit"):
        max_circ = supply_data["profit"].get("maxCirculating", PROFIT_SUPPLY)
    pct = (total_staked / max_circ) * 100 if max_circ > 0 else 0
    tx_url = solscan_tx(sig)

    msg = (
        f"🟢 <b>The Rewards Vat swells</b>\n\n"
        f"  <code>{tokens:,.0f} PROFIT</code> locked in\n"
        f"  Pool <code>{total_staked:,.0f}</code> staked · <code>{pct:.1f}%</code> of supply\n"
        f"  SOL dripping in every epoch\n\n"
        f"<i>The Doctor approves of your commitment.</i>\n\n"
        f"<a href='{tx_url}'>📋 Txn</a> · <a href='https://fraudsworth.fun'>Stake PROFIT</a>"
    )

    await send_alert(bot, msg, "profit_stake", tokens)

async def fire_carnage(bot: Bot, sol: float, target: str, sig: str):
    if not db.get_setting("alerts_carnage"):
        return

    tx_url = solscan_tx(sig)

    msg = (
        f"☠️ <b>CARNAGE DETONATES</b>\n\n"
        f"  Target <b>{target}</b>\n"
        f"  <code>{fmt_sol(sol)}</code> deployed · BUY. BURN. GONE.\n"
        f"  Supply shrinking permanently\n\n"
        f"<i>THE INCINERATOR IS FEASTING.</i>\n"
        f"<i>The Doctor watches. Delighted.</i>\n\n"
        f"<a href='{tx_url}'>📋 Txn</a>"
    )

    sent_msg = await send_alert(bot, msg, "carnage", sol)

    # Pin the carnage alert and notify all members
    if sent_msg:
        try:
            await sent_msg.pin(disable_notification=False)
            log.info("Carnage alert pinned successfully")
        except TelegramError as e:
            log.error(f"Failed to pin carnage alert: {e}")

async def fire_epoch_flip(bot: Bot, epoch_state: dict, sol_distributed: float):
    if not db.get_setting("alerts_epoch_flips"):
        return

    epoch_num  = epoch_state.get("epoch", "?")
    crime_buy  = epoch_state.get("crime_buy_bps", 0)
    crime_sell = epoch_state.get("crime_sell_bps", 0)
    fraud_buy  = epoch_state.get("fraud_buy_bps", 0)
    fraud_sell = epoch_state.get("fraud_sell_bps", 0)

    yield_line = f"  💰 <code>{sol_distributed:.4f} SOL</code> extracted from traders this epoch\n" if sol_distributed > 0.001 else ""

    msg = (
        f"🔄 <b>Epoch {display_epoch(epoch_num)} — THE DICE HAVE SPOKEN</b>\n\n"
        f"  🔴 CRIME  Buy <code>{bps_to_pct(crime_buy)}</code> · Sell <code>{bps_to_pct(crime_sell)}</code>\n"
        f"  🔵 FRAUD  Buy <code>{bps_to_pct(fraud_buy)}</code> · Sell <code>{bps_to_pct(fraud_sell)}</code>\n"
        f"{yield_line}\n"
        f"<i>New parameters. New victims. New opportunities.</i>"
    )

    await send_alert(bot, msg, "epoch_flip", 0)

# ============================================================
# TRANSACTION PARSER — v0 loadedAddresses support
# ============================================================
def build_account_keys(tx: dict) -> list:
    """Build full account keys list including v0 loadedAddresses"""
    keys = []
    for k in tx.get("transaction", {}).get("message", {}).get("accountKeys", []):
        keys.append(k if isinstance(k, str) else k.get("pubkey", ""))
    # v0 transactions: append loadedAddresses
    loaded = tx.get("meta", {}).get("loadedAddresses", {})
    keys.extend(loaded.get("writable", []))
    keys.extend(loaded.get("readonly", []))
    return keys

async def parse_and_dispatch(bot: Bot, sig: str, logs: list):
    """Parse a Tax program transaction and dispatch to correct handler"""
    global activity, cached_epoch_state

    try:
        if sig in processed_tax_sigs:
            return
        processed_tax_sigs[sig] = True

        while len(processed_tax_sigs) > 5000:
            processed_tax_sigs.popitem(last=False)

        activity["tax_txns_this_epoch"] += 1
        activity["last_tax_activity"] = time.time()

        # No keyword pre-filter — rely on account balance diffs for detection.
        # Keyword filters dropped arb bot txns (e.g. ArBhWNMcGfew...) whose
        # WS log payloads were truncated before the swap instruction names.

        # Refresh epoch state for accurate tax rates before processing
        fresh_state = await fetch_epoch_state()
        if fresh_state:
            cached_epoch_state = fresh_state

        tx = await rpc_get_transaction(sig)
        if not tx or tx.get("meta", {}).get("err"):
            return

        meta = tx.get("meta", {})
        account_keys = build_account_keys(tx)

        pre_balances  = meta.get("preBalances", [])
        post_balances = meta.get("postBalances", [])
        pre_token     = meta.get("preTokenBalances", [])
        post_token    = meta.get("postTokenBalances", [])

        # CARNAGE detection — only when CARNAGE_VAULT is involved AND has SOL leaving it.
        # Carnage = the vault spends SOL to buy & burn tokens on the AMM.
        # Check FIRST because carnage txns look like buys on the AMM.
        fee_payer = account_keys[0] if account_keys else ""

        if CARNAGE_VAULT in account_keys:
            cv_idx = account_keys.index(CARNAGE_VAULT)
            carnage_sol_left = 0
            if cv_idx < len(pre_balances) and cv_idx < len(post_balances):
                carnage_sol_left = (pre_balances[cv_idx] - post_balances[cv_idx]) / 1e9

            if carnage_sol_left >= 0.01:
                # SOL actually left the carnage vault — this is a real carnage trigger
                sol_spent = carnage_sol_left
                target = "CRIME or FRAUD"

                # Determine target from wSOL vault inflows
                for vault, label in [
                    (CRIME_WSOL_VAULT, "CRIME"),
                    (FRAUD_WSOL_VAULT, "FRAUD"),
                ]:
                    if vault in account_keys:
                        idx = account_keys.index(vault)
                        if idx < len(pre_balances) and idx < len(post_balances):
                            diff = (post_balances[idx] - pre_balances[idx]) / 1e9
                            if diff > 0.01:
                                target = label

                # Fallback: determine target from token balance changes
                if target == "CRIME or FRAUD":
                    for b in post_token:
                        if b.get("mint") == CRIME_MINT:
                            pre_v = float(next((p["uiTokenAmount"]["uiAmount"] for p in pre_token
                                if p.get("accountIndex") == b["accountIndex"]), 0) or 0)
                            if float(b["uiTokenAmount"]["uiAmount"] or 0) > pre_v:
                                target = "CRIME"
                        elif b.get("mint") == FRAUD_MINT:
                            pre_v = float(next((p["uiTokenAmount"]["uiAmount"] for p in pre_token
                                if p.get("accountIndex") == b["accountIndex"]), 0) or 0)
                            if float(b["uiTokenAmount"]["uiAmount"] or 0) > pre_v:
                                target = "FRAUD"

                if sol_spent >= 0.5:
                    log.info(f"Carnage: {sol_spent:.4f} SOL → {target} (fee_payer={fee_payer[:12]})")
                    await fire_carnage(bot, sol_spent, target, sig)
                    return
                else:
                    log.info(f"Carnage vault tx below threshold: {sol_spent:.4f} SOL sig={sig[:20]}")
                    return

        # Skip the in-house arb operator — its multi-pool rotations are
        # protocol rebalancing, not organic buys/sells. (Carnage above is
        # unaffected; it fires from the carnage vault, not the arb wallet.)
        if fee_payer == ARB_OPERATOR:
            return

        # ── MULTI-POOL BUY / SELL DETECTION ──────────────────────────
        # The factory is multi-pool: CRIME/FRAUD each trade against SOL,
        # USDC and HYPE. Every taxed swap routes through the tax program,
        # but a trade is only visible in *its own* pool's vault balances,
        # so we scan every pool via token-balance deltas on the base and
        # quote vaults. A genuine swap moves base and quote in OPPOSITE
        # directions; matching signs mean a liquidity deposit/withdrawal,
        # which we skip. One tx can touch several pools (arb); we fire on
        # the single largest leg to preserve one-alert-per-tx behaviour.
        def vault_delta(vault: str, decimals: int):
            if vault not in account_keys:
                return None
            idx = account_keys.index(vault)
            pre_raw = next((int(b["uiTokenAmount"]["amount"]) for b in pre_token
                            if b.get("accountIndex") == idx), 0)
            post_raw = next((int(b["uiTokenAmount"]["amount"]) for b in post_token
                             if b.get("accountIndex") == idx), 0)
            return (post_raw - pre_raw) / (10 ** decimals)

        best = None
        for pool in POOLS:
            q_diff = vault_delta(pool["quote_vault"], pool["quote_decimals"])
            b_diff = vault_delta(pool["base_vault"], BASE_DECIMALS)
            if q_diff is None or b_diff is None:
                continue
            if q_diff > 0 and b_diff < 0:
                direction, quote_amt, token_amt = "buy", q_diff, -b_diff
            elif q_diff < 0 and b_diff > 0:
                direction, quote_amt, token_amt = "sell", -q_diff, b_diff
            else:
                continue  # same-sign both vaults = liquidity event, not a trade
            sol_equiv = quote_to_sol(pool["quote"], quote_amt)
            if best is None or sol_equiv > best["sol_equiv"]:
                best = {"pool": pool, "direction": direction,
                        "quote_amt": quote_amt, "token_amt": token_amt,
                        "sol_equiv": sol_equiv}

        if not best:
            return

        pool      = best["pool"]
        base      = pool["base"]        # "CRIME" | "FRAUD"
        quote     = pool["quote"]       # "SOL" | "USDC" | "HYPE"
        sol_equiv = best["sol_equiv"]
        tokens    = best["token_amt"]

        if best["direction"] == "buy":
            min_sol = db.get_setting("min_buy_sol")
            if sol_equiv < min_sol:
                return
            mcap = cached_fdv["crime"] if base == "CRIME" else cached_fdv["fraud"]
            fdv  = cached_fdv_full["crime"] if base == "CRIME" else cached_fdv_full["fraud"]
            log.info(f"{base} buy [{pool['label']}]: {best['quote_amt']:.4f} {quote} "
                     f"(≈{sol_equiv:.4f} SOL) → {tokens:,.0f} tokens sig={sig[:20]}")
            fire = fire_crime_buy if base == "CRIME" else fire_fraud_buy
            await fire(bot, sol_equiv, tokens, mcap, sig, quote, best["quote_amt"], fdv)
            return
        else:  # sell
            sell_bps = cached_epoch_state.get(
                "crime_sell_bps" if base == "CRIME" else "fraud_sell_bps", 0)
            tax_extracted = sol_equiv * sell_bps / 10000
            min_sell_tax = db.get_setting("min_sell_tax_sol")
            if min_sell_tax is not None and tax_extracted >= min_sell_tax:
                log.info(f"{base} sell [{pool['label']}]: {best['quote_amt']:.4f} {quote} "
                         f"(≈{sol_equiv:.4f} SOL), tax {tax_extracted:.4f} SOL sig={sig[:20]}")
                await fire_big_sell(bot, sol_equiv, tokens, base, tax_extracted, sig,
                                    quote, best["quote_amt"])
            return

    except Exception as e:
        log.error(f"parse_and_dispatch error for {sig[:20]}: {e}\n{traceback.format_exc()}")

async def parse_stake(bot: Bot, sig: str):
    """Parse a Staking program transaction"""
    global activity

    try:
        if sig in processed_stake_sigs:
            return
        processed_stake_sigs[sig] = True

        while len(processed_stake_sigs) > 5000:
            processed_stake_sigs.popitem(last=False)

        activity["staking_txns_this_epoch"] += 1
        activity["last_staking_activity"] = time.time()

        tx = await rpc_get_transaction(sig)
        if not tx or tx.get("meta", {}).get("err"):
            return

        meta = tx.get("meta", {})
        pre_token  = meta.get("preTokenBalances", [])
        post_token = meta.get("postTokenBalances", [])

        # Look for PROFIT token changes on the staking pool's token account
        # Positive change = stake, negative change = unstake
        for b in post_token:
            if b.get("mint") == PROFIT_MINT and b.get("owner") == STAKE_POOL:
                pre = float(next((p["uiTokenAmount"]["uiAmount"] for p in pre_token
                                  if p.get("accountIndex") == b["accountIndex"]), 0) or 0)
                post_val = float(b["uiTokenAmount"]["uiAmount"] or 0)
                change = post_val - pre
                if change >= 100:
                    total = await rpc_get_token_balance(STAKING_PROFIT_VAULT)
                    log.info(f"PROFIT stake: {change:,.0f} tokens, total {total:,.0f}")
                    await fire_profit_stake(bot, change, total, sig)
                    return
                elif change <= -100:
                    log.info(f"PROFIT unstake: {abs(change):,.0f} tokens (no alert)")
                    return

    except Exception as e:
        log.error(f"parse_stake error for {sig[:20]}: {e}\n{traceback.format_exc()}")

# ============================================================
# EPOCH MONITOR — polls EpochState every 60s
# ============================================================
async def monitor_epochs(bot: Bot):
    global activity, cached_epoch_state
    log.info("Epoch monitor starting...")

    # Init escrow balance
    escrow_acct = await rpc_get_account(STAKING_ESCROW, "base64")
    if escrow_acct:
        activity["last_escrow_balance"] = escrow_acct.get("lamports", 0)

    while True:
        await asyncio.sleep(60)
        try:
            acct = await rpc_get_account(EPOCH_STATE, "base64")
            if not acct:
                continue

            raw = base64.b64decode(acct["data"][0])
            state = parse_epoch_state(raw)
            if not state:
                continue

            # Update cached state for use by alert builders
            cached_epoch_state = state

            current_epoch = state.get("epoch")
            cheap_side    = state.get("cheap_side")

            activity["current_epoch"]    = current_epoch
            activity["last_epoch_update"] = time.time()

            # Detect flip — only alert once per epoch number
            prev_cheap = activity.get("last_cheap_side")
            prev_epoch = activity.get("last_seen_epoch")
            last_alerted = activity.get("last_alerted_epoch")

            flipped = (prev_cheap is not None and prev_cheap != cheap_side)
            new_epoch = (prev_epoch is not None and prev_epoch != current_epoch)

            if (flipped or new_epoch) and current_epoch != last_alerted:
                # Wait for on-chain state to settle before alerting.
                # The program increments the epoch number first, then updates
                # tax rates in a separate transaction. A single read after the
                # epoch number changes can still have STALE tax rates from the
                # previous epoch. We must retry until the rates actually change.
                log.info(f"Epoch change detected (epoch {current_epoch}), waiting for tax rates to update...")

                # Snapshot previous rates so we can detect when they change
                prev_rates = (
                    cached_epoch_state.get("crime_buy_bps"),
                    cached_epoch_state.get("crime_sell_bps"),
                    cached_epoch_state.get("fraud_buy_bps"),
                    cached_epoch_state.get("fraud_sell_bps"),
                )

                settled = False
                settled_state = None
                last_rates = None  # Track previous read to detect when state stops changing
                for attempt in range(18):  # up to ~3 minutes
                    await asyncio.sleep(10)
                    settled_acct = await rpc_get_account(EPOCH_STATE, "base64")
                    if not settled_acct:
                        continue
                    settled_raw = base64.b64decode(settled_acct["data"][0])
                    settled_state = parse_epoch_state(settled_raw)
                    if not settled_state:
                        continue

                    new_rates = (
                        settled_state.get("cheap_side"),
                        settled_state.get("crime_buy_bps"),
                        settled_state.get("crime_sell_bps"),
                        settled_state.get("fraud_buy_bps"),
                        settled_state.get("fraud_sell_bps"),
                    )

                    # Check 1: rates must have actually changed from previous epoch
                    rates_changed = (new_rates[1:] != prev_rates)

                    # Check 2: cheap side's buy tax should be <= its sell tax
                    cs = settled_state.get("cheap_side")
                    if cs == "CRIME":
                        consistent = (settled_state["crime_buy_bps"] <= settled_state["crime_sell_bps"]
                                      and settled_state["fraud_sell_bps"] <= settled_state["fraud_buy_bps"])
                    else:
                        consistent = (settled_state["fraud_buy_bps"] <= settled_state["fraud_sell_bps"]
                                      and settled_state["crime_sell_bps"] <= settled_state["crime_buy_bps"])

                    # Check 3: two consecutive reads must return identical values
                    # (ensures the on-chain program has finished all update transactions)
                    stable = (last_rates is not None and new_rates == last_rates)

                    if rates_changed and consistent and stable:
                        state = settled_state
                        cached_epoch_state = settled_state
                        cheap_side = cs
                        settled = True
                        log.info(f"Settled state (attempt {attempt+1}): epoch {state.get('epoch')}, cheap={cheap_side}, "
                                 f"CRIME buy={state.get('crime_buy_bps')} sell={state.get('crime_sell_bps')}, "
                                 f"FRAUD buy={state.get('fraud_buy_bps')} sell={state.get('fraud_sell_bps')}")
                        break
                    else:
                        log.warning(f"Epoch state not settled (attempt {attempt+1}): rates_changed={rates_changed}, "
                                    f"consistent={consistent}, stable={stable}, cheap={cs}, "
                                    f"CRIME buy={settled_state.get('crime_buy_bps')} sell={settled_state.get('crime_sell_bps')}, "
                                    f"FRAUD buy={settled_state.get('fraud_buy_bps')} sell={settled_state.get('fraud_sell_bps')}")

                    last_rates = new_rates

                if not settled:
                    log.warning(f"Epoch state did not settle after 18 attempts, using last read")
                    if settled_state:
                        state = settled_state
                        cached_epoch_state = settled_state
                        cheap_side = state.get("cheap_side")

                sol_distributed = 0.0
                escrow_acct = await rpc_get_account(STAKING_ESCROW, "base64")
                if escrow_acct:
                    new_escrow = escrow_acct.get("lamports", 0)
                    prev_escrow = activity.get("last_escrow_balance", 0)
                    if prev_escrow and new_escrow > prev_escrow:
                        sol_distributed = (new_escrow - prev_escrow) / 1e9
                    activity["last_escrow_balance"] = new_escrow

                if flipped:
                    log.info(f"Epoch flip detected: {prev_cheap} → {cheap_side} (epoch {current_epoch})")
                elif new_epoch:
                    log.info(f"New epoch: {current_epoch}, cheap side: {cheap_side}")

                await fire_epoch_flip(bot, state, sol_distributed)

                activity["last_alerted_epoch"]      = current_epoch
                activity["tax_txns_this_epoch"]     = 0
                activity["staking_txns_this_epoch"] = 0

            activity["last_cheap_side"]  = cheap_side
            activity["last_seen_epoch"]  = current_epoch

        except Exception as e:
            log.error(f"Epoch monitor error: {e}")

# ============================================================
# HTTP POLLERS (replaced broken WebSocket logsSubscribe)
# ============================================================
async def rpc_get_signatures(program_id: str, limit: int = 10, until: str = None):
    """Fetch recent confirmed signatures for a program via HTTP RPC."""
    params = [program_id, {"limit": limit, "commitment": "confirmed"}]
    if until:
        params[1]["until"] = until
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress", "params": params}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(get_rpc_url(), json=payload, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if rpc_check_http(r.status):
                    return []
                resp = await r.json()
                if rpc_check_error(resp):
                    return []
                return resp.get("result", [])
    except Exception as e:
        log.warning(f"getSignaturesForAddress error: {e}")
        return []

async def poll_program(bot: Bot, program_id: str, label: str, handler):
    """Poll for new transactions on a program via HTTP instead of WebSocket logsSubscribe."""
    log.info(f"Starting HTTP poller: {label} (program={program_id[:16]}...)")
    last_sig = None

    # Seed with the most recent signature so we don't replay old txns
    initial = await rpc_get_signatures(program_id, limit=1)
    if initial:
        last_sig = initial[0]["signature"]
        log.info(f"{label} poller seeded at sig={last_sig[:20]}...")

    seen = set()  # Per-poller dedup (separate from handler-level processed_signatures)

    while True:
        await asyncio.sleep(5)
        try:
            sigs = await rpc_get_signatures(program_id, limit=25, until=last_sig)
            if not sigs:
                continue

            # Process oldest-first
            sigs.reverse()
            for entry in sigs:
                sig = entry["signature"]
                if entry.get("err"):
                    continue
                if sig in seen:
                    continue
                seen.add(sig)
                log.info(f"{label} new txn: {sig[:20]}...")
                asyncio.create_task(handler(bot, sig, []))

            # Keep seen set bounded
            if len(seen) > 5000:
                seen = set(list(seen)[-2000:])

            last_sig = sigs[-1]["signature"]

        except Exception as e:
            log.error(f"{label} poller error: {e}")
            await asyncio.sleep(10)

async def poll_staking(bot: Bot):
    """Poll for new staking transactions via HTTP."""
    log.info(f"Starting HTTP poller: Staking (program={STAKING_PROGRAM[:16]}...)")
    last_sig = None

    initial = await rpc_get_signatures(STAKING_PROGRAM, limit=1)
    if initial:
        last_sig = initial[0]["signature"]
        log.info(f"Staking poller seeded at sig={last_sig[:20]}...")

    seen = set()

    while True:
        await asyncio.sleep(5)
        try:
            sigs = await rpc_get_signatures(STAKING_PROGRAM, limit=25, until=last_sig)
            if not sigs:
                continue

            sigs.reverse()
            for entry in sigs:
                sig = entry["signature"]
                if entry.get("err"):
                    continue
                if sig in seen:
                    continue
                seen.add(sig)
                log.info(f"Staking new txn: {sig[:20]}...")
                asyncio.create_task(parse_stake(bot, sig))

            if len(seen) > 5000:
                seen = set(list(seen)[-2000:])

            last_sig = sigs[-1]["signature"]

        except Exception as e:
            log.error(f"Staking poller error: {e}")
            await asyncio.sleep(10)

# ============================================================
# WATCHDOG — alert if pollers go silent
# ============================================================
_watchdog_alerted = False

async def watchdog(bot: Bot):
    """Check every 5 minutes if the pollers have gone silent. Alert once, reset when they recover."""
    global _watchdog_alerted
    SILENT_THRESHOLD = 1800  # 30 minutes with no tax activity = alert

    # Give the pollers 2 minutes to start up before watching
    await asyncio.sleep(120)
    log.info("Watchdog started (threshold: 30m)")

    while True:
        await asyncio.sleep(300)  # check every 5 minutes
        try:
            last_tax = activity.get("last_tax_activity")
            if last_tax is None:
                continue

            gap = time.time() - last_tax
            if gap >= SILENT_THRESHOLD and not _watchdog_alerted:
                _watchdog_alerted = True
                gap_min = int(gap / 60)
                msg = (
                    f"⚠️ <b>WATCHDOG ALERT</b>\n\n"
                    f"No Tax program transactions received for <b>{gap_min} minutes</b>.\n"
                    f"Consecutive RPC failures: {_rpc_consecutive_failures}\n\n"
                    f"The bot may be missing buys/sells. Check Helius API quota."
                )
                log.warning(f"Watchdog: no Tax txns for {gap_min}m — alerting")
                await send_alert(bot, msg, chat_id=SUPER_ADMIN_ID)

            elif gap < SILENT_THRESHOLD and _watchdog_alerted:
                _watchdog_alerted = False
                log.info("Watchdog: pollers recovered, clearing alert state")

        except Exception as e:
            log.error(f"Watchdog error: {e}")

# ============================================================
# TELEGRAM COMMANDS
# ============================================================
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not db.is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ Admin only.")
            return
        return await func(update, context)
    return wrapper

@admin_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = db.get_all_settings()
    images = db.get_all_images()
    on  = [k.replace("alerts_", "") for k, v in s.items() if k.startswith("alerts_") and v]
    off = [k.replace("alerts_", "") for k, v in s.items() if k.startswith("alerts_") and not v]
    img_list = [r[0] for r in images]

    msg = (
        f"🧪 <b>Laboratory Assistant — Status</b>\n\n"
        f"<b>Alerts ON:</b> {', '.join(on) or 'none'}\n"
        f"<b>Alerts OFF:</b> {', '.join(off) or 'none'}\n\n"
        f"<b>Thresholds:</b>\n"
        f"Min buy: <code>{s.get('min_buy_sol')} SOL</code>\n"
        f"Medium: <code>{s.get('medium_buy_sol')} SOL</code>\n"
        f"Whale: <code>{s.get('whale_buy_sol')} SOL</code>\n"
        f"Mega: <code>{s.get('mega_whale_sol')} SOL</code>\n"
        f"Sell tax min: <code>{s.get('min_sell_tax_sol')} SOL</code>\n\n"
        f"<b>Images set:</b> {', '.join(img_list) or 'none'}\n"
        f"<b>Chat ID:</b> <code>{CHAT_ID}</code>\n"
        f"<b>SOL:</b> <code>${sol_price:.2f}</code>"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

@admin_only
async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    epoch_state = await fetch_epoch_state()
    carnage_acct = await rpc_get_account(CARNAGE_VAULT, "base64")
    carnage_sol = (carnage_acct.get("lamports", 0) / 1e9) if carnage_acct else 0

    tax_ok     = "🟢" if activity["last_tax_activity"] and (time.time() - activity["last_tax_activity"]) < 3600 else "🟡"
    staking_ok = "🟢" if activity["last_staking_activity"] and (time.time() - activity["last_staking_activity"]) < 7200 else "🟡"

    msg = (
        f"🔬 <b>Laboratory Health Check</b>\n\n"
        f"{tax_ok} <b>Tax program</b> — {activity['tax_txns_this_epoch']} txns this epoch, "
        f"last: {time_ago(activity['last_tax_activity'])}\n"
        f"{staking_ok} <b>Staking program</b> — {activity['staking_txns_this_epoch']} txns this epoch, "
        f"last: {time_ago(activity['last_staking_activity'])}\n"
        f"🟢 <b>Epoch monitor</b> — last update: {time_ago(activity['last_epoch_update'])}\n"
        f"🟢 <b>SOL price</b> — ${sol_price:.2f}, updated: {time_ago(activity['last_sol_price_update'])}\n\n"
        f"<b>Current epoch:</b> <code>{display_epoch(activity.get('current_epoch', '?'))}</code>\n"
        f"<b>Cheap side:</b> <code>{activity.get('last_cheap_side', '?')}</code>\n\n"
        f"<b>Epoch state (live):</b>\n"
        f"🔴 CRIME: Buy <code>{bps_to_pct(epoch_state.get('crime_buy_bps', 0))}</code> / "
        f"Sell <code>{bps_to_pct(epoch_state.get('crime_sell_bps', 0))}</code>\n"
        f"🔵 FRAUD: Buy <code>{bps_to_pct(epoch_state.get('fraud_buy_bps', 0))}</code> / "
        f"Sell <code>{bps_to_pct(epoch_state.get('fraud_sell_bps', 0))}</code>\n\n"
        f"💰 <b>Carnage vault:</b> <code>{carnage_sol:.4f} SOL</code>"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def cmd_epoch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = await fyi_get("/solana/epoch-state")
    if not data:
        # Fallback to on-chain
        state = await fetch_epoch_state()
        if not state:
            await update.message.reply_text("Failed to fetch epoch state.")
            return
        cheap = state.get("cheap_side", "?")
        msg = (
            f"🔄 <b>Epoch {display_epoch(state.get('epoch', '?'))} — Current Tax Regime</b>\n\n"
            f"🔴 CRIME: Buy <code>{bps_to_pct(state.get('crime_buy_bps', 0))}</code> / "
            f"Sell <code>{bps_to_pct(state.get('crime_sell_bps', 0))}</code>\n"
            f"🔵 FRAUD: Buy <code>{bps_to_pct(state.get('fraud_buy_bps', 0))}</code> / "
            f"Sell <code>{bps_to_pct(state.get('fraud_sell_bps', 0))}</code>\n\n"
            f"<b>Cheap side:</b> <code>{cheap}</code> "
            f"({cheap} is cheap to buy)"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        return

    epoch_num = data.get("epochNumber", "?")
    cheap_label = data.get("cheapSideLabel", "?")
    countdown = int(data.get("countdownSeconds", 0))
    mins, secs = divmod(countdown, 60)
    crime_buy = bps_to_pct(data.get("crimeBuyTaxBps", 0))
    crime_sell = bps_to_pct(data.get("crimeSellTaxBps", 0))
    fraud_buy = bps_to_pct(data.get("fraudBuyTaxBps", 0))
    fraud_sell = bps_to_pct(data.get("fraudSellTaxBps", 0))
    flipped = "⚡ JUST FLIPPED!" if data.get("flipped") else ""
    carnage = "💀 CARNAGE PENDING" if data.get("carnagePending") else ""

    msg = (
        f"🔄 <b>Epoch {display_epoch(epoch_num)} — Tax Regime</b>\n\n"
        f"🔴 CRIME: Buy <code>{crime_buy}</code> / Sell <code>{crime_sell}</code>\n"
        f"🔵 FRAUD: Buy <code>{fraud_buy}</code> / Sell <code>{fraud_sell}</code>\n\n"
        f"💰 <b>Cheap side:</b> <code>{cheap_label}</code>\n"
        f"⏱ <b>Next flip:</b> <code>{mins}m {secs}s</code>\n"
    )
    if flipped:
        msg += f"\n{flipped}\n"
    if carnage:
        msg += f"\n{carnage}\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Recompute live from all pools; fall back to the last cached valuation.
    v = await compute_pool_valuations()
    mcap = v["mcap"] if v else cached_fdv
    fdv = v["fdv"] if v else cached_fdv_full

    profit_line = (
        f"\n🟢 PROFIT  MC <code>{fmt_usd(mcap['profit'])}</code> · "
        f"FDV <code>{fmt_usd(fdv['profit'])}</code>"
        if mcap.get("profit") else ""
    )

    msg = (
        f"💹 <b>Live Prices</b> <i>(all pools)</i>\n\n"
        f"🔴 CRIME  MC <code>{fmt_usd(mcap['crime'])}</code> · FDV <code>{fmt_usd(fdv['crime'])}</code>\n"
        f"🔵 FRAUD  MC <code>{fmt_usd(mcap['fraud'])}</code> · FDV <code>{fmt_usd(fdv['fraud'])}</code>"
        f"{profit_line}\n"
        f"💎 SOL: <code>${sol_price:.2f}</code>"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def cmd_staking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_staked = await rpc_get_token_balance(STAKING_PROFIT_VAULT)
    # Use max circulating supply for staked %
    supply_data = await fyi_get("/supply/current")
    max_circ = PROFIT_SUPPLY  # fallback
    if supply_data and supply_data.get("profit"):
        max_circ = supply_data["profit"].get("maxCirculating", PROFIT_SUPPLY)
    pct = (total_staked / max_circ) * 100 if max_circ > 0 else 0
    escrow_acct = await rpc_get_account(STAKING_ESCROW, "base64")
    escrow_sol = (escrow_acct.get("lamports", 0) / 1e9) if escrow_acct else 0

    msg = (
        f"🟢 <b>Staking Pool</b>\n\n"
        f"Total staked: <code>{total_staked:,.0f} PROFIT</code>\n"
        f"Supply staked: <code>{pct:.1f}%</code>\n"
        f"Escrow balance: <code>{escrow_sol:.4f} SOL</code>\n\n"
        f"<i>71% of all trading taxes flow here every epoch.</i>"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def cmd_carnage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    acct = await rpc_get_account(CARNAGE_VAULT, "base64")
    sol = (acct.get("lamports", 0) / 1e9) if acct else 0
    usd = sol * sol_price

    msg = (
        f"☠️ <b>Carnage Vault</b>\n\n"
        f"Balance: <code>{sol:.4f} SOL</code> (${usd:,.0f})"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def cmd_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        f"📊 <b>Fraudsworth Analytics</b>\n\n"
        f"<a href='https://www.fraudsworth.fyi/analytics'>View the full dashboard</a>"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

@admin_only
async def cmd_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mapping = {
        "crime":   "alerts_crime_buys",
        "fraud":   "alerts_fraud_buys",
        "stakes":  "alerts_profit_stakes",
        "carnage": "alerts_carnage",
        "epoch":   "alerts_epoch_flips",
        "sells":   "alerts_sells",
    }
    if not context.args:
        await update.message.reply_text(
            f"Usage: /toggle &lt;type&gt;\nTypes: {', '.join(mapping.keys())}",
            parse_mode=ParseMode.HTML
        )
        return
    key = mapping.get(context.args[0].lower())
    if not key:
        await update.message.reply_text(f"Unknown. Options: {', '.join(mapping.keys())}")
        return
    new_val = not db.get_setting(key)
    db.set_setting(key, new_val)
    status = "✅ ON" if new_val else "❌ OFF"
    await update.message.reply_text(
        f"{status} — <b>{context.args[0].upper()}</b> alerts",
        parse_mode=ParseMode.HTML
    )

@admin_only
async def cmd_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mapping = {
        "min":     "min_buy_sol",
        "medium":  "medium_buy_sol",
        "whale":   "whale_buy_sol",
        "mega":    "mega_whale_sol",
        "selltax": "min_sell_tax_sol",
    }
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /threshold &lt;min|medium|whale|mega|selltax&gt; &lt;value&gt;",
                                        parse_mode=ParseMode.HTML)
        return
    key = mapping.get(context.args[0].lower())
    if not key:
        await update.message.reply_text(f"Unknown. Options: {', '.join(mapping.keys())}")
        return
    try:
        val = float(context.args[1])
        db.set_setting(key, val)
        await update.message.reply_text(
            f"✅ <b>{context.args[0]}</b> = <code>{val} SOL</code>",
            parse_mode=ParseMode.HTML
        )
    except ValueError:
        await update.message.reply_text("Value must be a number")


@admin_only
async def cmd_setimage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    valid = [
        "crime_buy", "fraud_buy",
        "profit_stake", "carnage", "epoch_flip",
        "big_sell", "startup"
    ]
    if not context.args:
        await update.message.reply_text(
            "Usage: /setimage &lt;type&gt; then send image/gif\n\n<b>Types:</b>\n" +
            "\n".join(f"  • {t}" for t in valid),
            parse_mode=ParseMode.HTML
        )
        return
    event_type = context.args[0].lower()
    if event_type not in valid:
        await update.message.reply_text(f"Unknown. Valid: {', '.join(valid)}")
        return
    pending_image_updates[update.effective_user.id] = event_type
    await update.message.reply_text(
        f"✅ Ready for <b>{event_type}</b> — send the image or GIF now",
        parse_mode=ParseMode.HTML
    )

@admin_only
async def cmd_removeimage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /removeimage &lt;event_type&gt;", parse_mode=ParseMode.HTML)
        return
    conn = db.get_conn()
    conn.execute("DELETE FROM images WHERE event_type = ?", (context.args[0].lower(),))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Image removed for <b>{context.args[0]}</b>", parse_mode=ParseMode.HTML)

@admin_only
async def cmd_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global cached_epoch_state
    if not context.args:
        await update.message.reply_text("Usage: /preview &lt;crime|fraud|stake|carnage|epoch|whale|mega|sell&gt;",
                                        parse_mode=ParseMode.HTML)
        return
    t = context.args[0].lower()
    bot = context.bot

    # Seed cached_epoch_state if empty for previews
    preview_state = cached_epoch_state or {
        "crime_buy_bps": 300, "crime_sell_bps": 1400,
        "fraud_buy_bps": 1200, "fraud_sell_bps": 200,
    }
    if not cached_epoch_state:
        cached_epoch_state = preview_state

    if t == "crime":
        await fire_crime_buy(bot, 1.5, 850000, 142000, "PREVIEW")
    elif t == "fraud":
        await fire_fraud_buy(bot, 1.5, 850000, 141000, "PREVIEW")
    elif t == "stake":
        await fire_profit_stake(bot, 50000, 3900000, "PREVIEW")
    elif t == "carnage":
        await fire_carnage(bot, 7.0, "FRAUD", "PREVIEW")
    elif t == "epoch":
        fake_state = {
            "epoch": activity.get("current_epoch", 486), "cheap_side": "CRIME",
            "crime_buy_bps": 300, "crime_sell_bps": 1400,
            "fraud_buy_bps": 1200, "fraud_sell_bps": 200,
        }
        await fire_epoch_flip(bot, fake_state, 0.847)
    elif t == "whale":
        await fire_crime_buy(bot, 15.0, 9500000, 142000, "PREVIEW")
    elif t == "mega":
        await fire_crime_buy(bot, 75.0, 47000000, 142000, "PREVIEW")
    elif t == "sell":
        await fire_big_sell(bot, 25.0, 15000000, "CRIME", 3.5, "PREVIEW")
    else:
        await update.message.reply_text("Types: crime, fraud, stake, carnage, epoch, whale, mega, sell")

@admin_only
async def cmd_botstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today    = db.get_stats_today()
    all_time = db.get_stats_all_time()
    t_lines  = "\n".join(f"  {e}: {c} ({s:.2f} SOL)" for e, c, s in today) or "  None yet"
    a_lines  = "\n".join(f"  {e}: {c} ({s:.2f} SOL)" for e, c, s in all_time) or "  None yet"
    msg = (
        f"📊 <b>Bot Statistics</b>\n\n"
        f"<b>Today:</b>\n{t_lines}\n\n"
        f"<b>All Time:</b>\n{a_lines}"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def cmd_apy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    apy_data, state_data = await asyncio.gather(
        fyi_get("/staking/apy"),
        fyi_get("/staking/state"),
    )
    if not apy_data or not state_data:
        await update.message.reply_text("Failed to fetch staking data.")
        return

    apy = apy_data.get("apy", 0)
    rate_per_epoch = apy_data.get("rewardRates", {}).get("lastEpochSol", 0)
    # Estimate SOL earned per epoch per 1,000 PROFIT staked
    total_staked = state_data.get("totalStakedHuman", 1)
    sol_per_1k = (rate_per_epoch * total_staked / 1) * (1000 / total_staked) if total_staked > 0 else 0
    # Simpler: rate_per_epoch is SOL per 1 PROFIT per epoch already? Let's compute from APY.
    # Actually rewardRates.lastEpochSol = SOL per 1 staked PROFIT per epoch
    sol_per_1k = rate_per_epoch * 1000

    escrow_sol = state_data.get("escrowSol", 0)
    staked_pct = state_data.get("stakedPercent", 0)

    msg = (
        f"📈 <b>Staking APY</b>\n\n"
        f"🔥 APY: <code>{apy:.1f}%</code>\n"
        f"💰 Est. per epoch (1K PROFIT): <code>{sol_per_1k:.6f} SOL</code>\n\n"
        f"🏦 Total staked: <code>{total_staked:,.0f} PROFIT</code> ({staked_pct:.1f}%)\n"
        f"💎 Escrow balance: <code>{escrow_sol:.3f} SOL</code>"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def cmd_supply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = await fyi_get("/supply/current")
    if not data:
        await update.message.reply_text("Failed to fetch supply data.")
        return

    lines = []
    for name, emoji in [("crime", "🔴"), ("fraud", "🔵"), ("profit", "🟢")]:
        t = data.get(name, {})
        circ = t.get("circulating", 0)
        circ_pct = t.get("circulatingPct", 0)
        burned = t.get("burned", 0)
        lines.append(
            f"{emoji} <b>{name.upper()}</b>\n"
            f"  Circulating: <code>{circ:,.0f}</code> ({circ_pct:.1f}%)\n"
            f"  Burned: <code>{burned:,.0f}</code>"
        )

    msg = (
        f"📦 <b>Token Supply</b>\n\n"
        + "\n\n".join(lines)
        + f"\n\n<i>Circulating = total − conversion vault − burns.\n"
        f"Staking does NOT affect circulating supply.</i>"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def cmd_liquidity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    liq = await compute_pool_liquidity()
    if not liq:
        await update.message.reply_text("Failed to fetch liquidity data.")
        return

    def fmt_depth(quote: str, bal: float) -> str:
        if quote == "USDC":
            return f"{bal/1000:.1f}K {quote}" if bal >= 1000 else f"{bal:.0f} {quote}"
        return f"{bal:,.1f} {quote}"   # SOL / HYPE

    blocks = []
    for name, emoji in [("CRIME", "🔴"), ("FRAUD", "🔵")]:
        t = liq[name]
        rows = [
            f"{pool['quote']:<4} {fmt_usd(pool['tvl']):>8}  {fmt_depth(pool['quote'], pool['quote_bal']):>12}"
            for pool in t["pools"]
        ]
        blocks.append(
            f"{emoji} <b>{name}</b>  ·  TVL <b>{fmt_usd(t['tvl'])}</b>\n"
            f"<pre>{chr(10).join(rows)}</pre>"
        )

    msg = (
        f"💧 <b>Liquidity Pools</b> <i>(all quotes · 1% LP fee)</i>\n\n"
        + "\n\n".join(blocks)
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = await fyi_get("/analytics/summary")
    if not data:
        await update.message.reply_text("Failed to fetch analytics.")
        return

    vol = data.get("totalVolumeSol", 0)
    wallets = data.get("uniqueWallets", 0)
    tax = data.get("totalTaxCollectedSol", 0)
    sol_usd = data.get("solUsdPrice", 0)

    msg = (
        f"📊 <b>All-Time Stats</b>\n\n"
        f"💰 Total volume: <code>{vol:,.2f} SOL</code> ({fmt_usd(vol * sol_usd)})\n"
        f"👥 Unique wallets: <code>{wallets:,}</code>\n"
        f"🏦 Tax collected: <code>{tax:,.2f} SOL</code> ({fmt_usd(tax * sol_usd)})"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

@admin_only
async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /addadmin &lt;user_id&gt;", parse_mode=ParseMode.HTML)
        return
    try:
        uid = int(context.args[0])
        db.add_admin(uid, "", update.effective_user.id)
        await update.message.reply_text(f"✅ Admin added: <code>{uid}</code>", parse_mode=ParseMode.HTML)
    except ValueError:
        await update.message.reply_text("User ID must be a number")

@admin_only
async def cmd_removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /removeadmin &lt;user_id&gt;", parse_mode=ParseMode.HTML)
        return
    try:
        uid = int(context.args[0])
        if uid == SUPER_ADMIN_ID:
            await update.message.reply_text("⛔ Cannot remove super admin")
            return
        db.remove_admin(uid)
        await update.message.reply_text(f"✅ Admin removed: <code>{uid}</code>", parse_mode=ParseMode.HTML)
    except ValueError:
        await update.message.reply_text("User ID must be a number")

@admin_only
async def cmd_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admins = db.get_all_admins()
    lines  = "\n".join(f"  <code>{uid}</code> — {uname or 'unknown'}" for uid, uname, at in admins)
    await update.message.reply_text(f"<b>Admins:</b>\n{lines}", parse_mode=ParseMode.HTML)

async def cmd_sol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"💎 SOL: <code>${sol_price:.2f}</code>",
        parse_mode=ParseMode.HTML
    )

async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        f"📜 <b>Fraudsworth Laboratory Safety Guide</b>\n\n"
        f"1️⃣ Don't piss off the Dr.\n"
        f"2️⃣ No racism or hate speech — zero tolerance.\n"
        f"3️⃣ No links, no shilling other coins.\n"
        f"4️⃣ No FUD or whining about your own trades — your entry, your risk.\n"
        f"5️⃣ Don't be a dickhead. Keep it fun, keep it respectful.\n\n"
        f"<i>Break the rules and you're gone — no warning.</i>"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

# ── CHART ────────────────────────────────────────────────────
# Timeframe buttons → (API period, max_hours to slice from recent end)
# max_hours=None means use full API response
CHART_TIMEFRAMES = {
    "1m":  ("6h",   2),       # last 2 hours
    "5m":  ("24h",  12),      # last 12 hours
    "30m": ("7d",   72),      # last 3 days  (default)
    "1h":  ("7d",   None),    # last 7 days  (full 7d response)
    "4h":  ("30d",  None),    # last 30 days
    "1d":  ("all",  None),    # full token lifetime
}
CHART_TF_ORDER = ["1m", "5m", "30m", "1h", "4h", "1d"]
CHART_DEFAULT_TF = "30m"

# ── Colour palette (Fraudsworth dark theme) ──
_C_BG         = "#000000"
_C_LINE       = "#ff2020"
_C_GLOW       = "#e82020"
_C_GRID       = (1.0, 1.0, 1.0, 0.10)
_C_AXIS       = "#888888"
_C_WHITE      = "#ffffff"
_C_GOLD       = "#b07830"
_C_PILL_BG    = "#1a1200"
_C_PILL_BD    = "#3a2500"
_C_RED        = "#ff2200"
_C_GREEN      = "#00cc44"
_C_PRICE_PILL = "#cc2200"
_C_REF_LINE   = (1.0, 0.47, 0.47, 0.4)   # rgba(255,120,120,0.4)
_C_WATERMARK  = (0.71, 0.71, 0.71, 0.25)

# Output pixel dimensions
_CHART_W = 1456
_CHART_H = 816
_CHART_DPI = 140


def _fmt_mc(x, _pos=None):
    if abs(x) < 0.01:
        return "$0"
    if x >= 1_000_000:
        return f"${x / 1_000_000:.2f}M"
    elif x >= 1_000:
        return f"${x / 1_000:.0f}K"
    return f"${x:,.0f}"


def _fmt_pct(v):
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.1f}%"


def _pct_color(v):
    return _C_GREEN if v >= 0 else _C_RED


def _slice_points(points: list[dict], max_hours: int | None) -> list[dict]:
    """Slice points to the last max_hours of data. None = keep all."""
    if max_hours is None or not points:
        return points
    cutoff = points[-1]["time"] - max_hours * 3600
    return [p for p in points if p["time"] >= cutoff] or points


def _render_profit_chart(points: list[dict], active_tf: str,
                         pool: dict | None = None) -> bytes:
    """Render PROFIT market-cap chart as PNG. Fraudsworth dark premium style."""
    times  = [datetime.utcfromtimestamp(p["time"]) for p in points]
    values = [p["mcUsd"] for p in points]

    # Dynamic colour: green if price went up over timeframe, red if down
    went_up = values[-1] >= values[0] if len(values) >= 2 else True
    line_color = "#00cc44" if went_up else "#ff2020"
    glow_color = "#00cc44" if went_up else "#e82020"
    fill_color = "#004d1a" if went_up else "#8b0000"
    fill_color2 = "#001a0a" if went_up else "#3d0000"
    ref_line_color = (0.47, 1.0, 0.47, 0.4) if went_up else (1.0, 0.47, 0.47, 0.4)
    pill_bg_color = "#00cc44" if went_up else _C_PRICE_PILL

    fig_w = _CHART_W / _CHART_DPI
    fig_h = _CHART_H / _CHART_DPI

    has_stats = pool is not None
    if has_stats:
        fig = plt.figure(figsize=(fig_w, fig_h), dpi=_CHART_DPI)
        gs = fig.add_gridspec(2, 1, height_ratios=[1, 6.5], hspace=0.04,
                              left=0.01, right=0.93, top=0.97, bottom=0.07)
        ax_stats = fig.add_subplot(gs[0])
        ax = fig.add_subplot(gs[1])
    else:
        fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=_CHART_DPI)
        fig.subplots_adjust(left=0.01, right=0.93, top=0.95, bottom=0.07)

    fig.patch.set_facecolor(_C_BG)

    # ── Stats bar (compact) ──
    if has_stats:
        ax_stats.set_facecolor(_C_BG)
        ax_stats.set_xlim(0, 1)
        ax_stats.set_ylim(0, 1)
        ax_stats.axis("off")

        profit = pool.get("profit", {})
        crime  = pool.get("crime", {})
        fraud  = pool.get("fraud", {})
        sol_price = pool.get("solUsdPrice", 0)

        # PROFIT MARKET CAP label (full label to match reference)
        pills = [
            ("$PROFIT MARKET CAP", _fmt_mc(profit.get("marketCapUsd", 0)), "#f5c000",
             _fmt_pct(profit.get("percentChange", 0)), _pct_color(profit.get("percentChange", 0)),
             f"{profit.get('marketCapUsd',0)/sol_price:,.0f} SOL" if sol_price else ""),
            ("$PROFIT PRICE", f"${profit.get('priceUsd', 0):.4f}", "#ff4400",
             _fmt_pct(profit.get("percentChange", 0)), _pct_color(profit.get("percentChange", 0)), ""),
            ("$CRIME MC", _fmt_mc(crime.get("marketCapUsd", 0)), _C_RED,
             _fmt_pct(crime.get("percentChange", 0)), _pct_color(crime.get("percentChange", 0)), ""),
            ("$FRAUD MC", _fmt_mc(fraud.get("marketCapUsd", 0)), _C_RED,
             _fmt_pct(fraud.get("percentChange", 0)), _pct_color(fraud.get("percentChange", 0)), ""),
            ("SOL / USD", f"${sol_price:,.2f}", _C_WHITE, "", "", ""),
            ("PERIOD", active_tf.upper(), _C_WHITE, "", "", ""),
        ]

        n = len(pills)
        pad = 0.010
        w = (1.0 - pad * (n + 1)) / n

        for i, (label, val, val_col, pct_str, pct_col, sub) in enumerate(pills):
            x0 = pad + i * (w + pad)
            rect = plt.Rectangle((x0, 0.05), w, 0.90, transform=ax_stats.transAxes,
                                  facecolor=_C_PILL_BG, edgecolor=_C_PILL_BD,
                                  linewidth=0.7, clip_on=False, zorder=2)
            ax_stats.add_patch(rect)
            cx = x0 + w / 2
            # label — small caps
            ax_stats.text(cx, 0.82, label, transform=ax_stats.transAxes,
                          ha="center", va="top", fontsize=5.5, fontweight="bold",
                          color=_C_GOLD, zorder=3)
            # value — large bold
            ax_stats.text(cx, 0.50, val, transform=ax_stats.transAxes,
                          ha="center", va="center", fontsize=10, fontweight="bold",
                          color=val_col, zorder=3)
            # percent change
            if pct_str:
                y_pct = 0.20 if not sub else 0.24
                ax_stats.text(cx, y_pct, pct_str, transform=ax_stats.transAxes,
                              ha="center", va="center", fontsize=6.5, fontweight="bold",
                              color=pct_col, zorder=3)
            # sub-label (SOL equiv)
            if sub:
                ax_stats.text(cx, 0.10, sub, transform=ax_stats.transAxes,
                              ha="center", va="center", fontsize=5.5, color=_C_AXIS, zorder=3)

    # ── Chart area ──
    ax.set_facecolor(_C_BG)

    # area fill
    ax.fill_between(times, values, alpha=0.35, color=fill_color, zorder=2)
    ax.fill_between(times, values, alpha=0.15, color=fill_color2, zorder=1)

    # glow behind line
    ax.plot(times, values, color=glow_color, linewidth=4, alpha=0.15, zorder=3)
    ax.plot(times, values, color=glow_color, linewidth=2.5, alpha=0.25, zorder=4)

    # main line
    ax.plot(times, values, color=line_color, linewidth=1.4, zorder=5)

    # grid — horizontal only, clean white at low opacity
    ax.grid(True, axis="y", color=_C_GRID, linewidth=0.5)
    ax.grid(False, axis="x")
    ax.set_axisbelow(True)

    # spines
    for spine in ("top", "left", "right", "bottom"):
        ax.spines[spine].set_visible(False)

    # y-axis on right
    ax.yaxis.tick_right()
    ax.yaxis.set_label_position("right")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(_fmt_mc))
    ax.tick_params(axis="y", colors=_C_WHITE, labelsize=8, length=0, pad=6)
    data_max = max(values) if values else 1
    data_min = min(values) if values else 0
    # Short timeframes: auto-scale with breathing room above & below
    # Long timeframes: start at $0 for dramatic full-range context
    if active_tf in ("1m", "5m"):
        data_range = data_max - data_min
        pad_y = data_range * 0.5 if data_range > 0 else data_max * 0.05
        ax.set_ylim(max(0, data_min - pad_y), data_max + pad_y)
    else:
        ax.set_ylim(0, data_max * 1.08)

    # current price — dashed reference line + red pill badge
    if values:
        latest = values[-1]
        ax.axhline(y=latest, color=ref_line_color, linewidth=0.7, linestyle="--", zorder=6)

        price_label = _fmt_mc(latest)
        ax.annotate(
            price_label,
            xy=(1.0, latest), xycoords=("axes fraction", "data"),
            xytext=(6, 0), textcoords="offset points",
            fontsize=8, fontweight="bold", color=_C_WHITE,
            bbox=dict(boxstyle="round,pad=0.25", facecolor=pill_bg_color,
                      edgecolor="none", alpha=0.95),
            va="center", ha="left", zorder=10, clip_on=False,
        )

    # x-axis formatting
    span_days = (times[-1] - times[0]).days if len(times) >= 2 else 0
    span_hours = (times[-1] - times[0]).total_seconds() / 3600 if len(times) >= 2 else 0
    if span_hours <= 6:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    elif span_days < 2:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    else:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%-d"))
    ax.tick_params(axis="x", colors=_C_WHITE, labelsize=8, length=0, pad=6)

    # x margins
    ax.margins(x=0.005)

    # TV logo placeholder — bottom left
    ax.text(0.01, 0.02, "TV", transform=ax.transAxes,
            ha="left", va="bottom", fontsize=14, fontweight="bold",
            color=(1.0, 1.0, 1.0, 0.6), zorder=1,
            fontfamily="monospace")

    # watermark — bottom right
    ax.text(0.99, 0.02, "fraudsworth.fyi", transform=ax.transAxes,
            ha="right", va="bottom", fontsize=8, color=_C_WATERMARK, zorder=1)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=_C_BG, edgecolor="none",
                bbox_inches=None)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _chart_keyboard(active_tf: str) -> InlineKeyboardMarkup:
    """Build inline timeframe buttons. Active button gets a bracket marker."""
    buttons = []
    for label in CHART_TF_ORDER:
        text = f"[{label}]" if label == active_tf else label
        buttons.append(InlineKeyboardButton(text, callback_data=f"chart_{label}"))
    return InlineKeyboardMarkup([buttons])


async def _fetch_chart_data(tf: str):
    """Fetch chart history + pool state concurrently, then slice."""
    api_period, max_hours = CHART_TIMEFRAMES.get(tf, ("24h", None))
    history_coro = fyi_get(f"/profit/history?period={api_period}")
    pool_coro = fyi_get("/solana/pool-state")
    history, pool = await asyncio.gather(history_coro, pool_coro)
    # Slice to the requested window
    if history and history.get("points"):
        history["points"] = _slice_points(history["points"], max_hours)
    return history, pool


async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tf = CHART_DEFAULT_TF
    history, pool = await _fetch_chart_data(tf)
    if not history or not history.get("points"):
        await update.message.reply_text("Could not fetch chart data.")
        return
    png = _render_profit_chart(history["points"], tf, pool)
    await update.message.reply_photo(
        photo=png,
        reply_markup=_chart_keyboard(tf),
    )


async def chart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle timeframe button taps — swap chart image in-place."""
    query = update.callback_query
    await query.answer()

    tf = query.data.removeprefix("chart_")
    if tf not in CHART_TIMEFRAMES:
        return

    history, pool = await _fetch_chart_data(tf)
    if not history or not history.get("points"):
        await query.answer("Could not fetch data", show_alert=True)
        return

    png = _render_profit_chart(history["points"], tf, pool)
    await query.edit_message_media(
        media=InputMediaPhoto(media=png),
        reply_markup=_chart_keyboard(tf),
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        f"🧪 <b>Laboratory Assistant Commands</b>\n\n"
        f"<b>Anyone:</b>\n"
        f"/sol — SOL price\n"
        f"/epoch — tax rates, countdown &amp; cheap side\n"
        f"/price — CRIME, FRAUD &amp; PROFIT FDV\n"
        f"/apy — staking APY &amp; rewards\n"
        f"/supply — circulating supply &amp; burns\n"
        f"/liquidity — pool balances &amp; TVL\n"
        f"/stats — all-time protocol stats\n"
        f"/staking — staking pool info\n"
        f"/carnage — carnage vault balance\n"
        f"/analytics — view analytics dashboard\n"
        f"/rules — group rules\n"
        f"/chart — view chart\n"
        f"/help — this message"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

# ============================================================
# IMAGE HANDLER
# ============================================================
async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not db.is_admin(user_id):
        return
    event_type = pending_image_updates.get(user_id)
    if not event_type:
        return
    msg = update.message
    file_id = None
    file_type = "photo"
    if msg.animation:
        file_id = msg.animation.file_id
        file_type = "animation"
    elif msg.photo:
        file_id = msg.photo[-1].file_id
    elif msg.document and msg.document.mime_type == "image/gif":
        file_id = msg.document.file_id
        file_type = "animation"
    if file_id:
        db.set_image(event_type, file_id, file_type)
        del pending_image_updates[user_id]
        await msg.reply_text(
            f"✅ Image set for <b>{event_type}</b>",
            parse_mode=ParseMode.HTML
        )
    else:
        await msg.reply_text("Please send a photo or GIF")

# ============================================================
# STARTUP MESSAGE
# ============================================================
async def send_startup(bot: Bot):
    """Startup hook — silent restart, no message sent."""
    pass

# ============================================================
# MAIN
# ============================================================
async def main():
    global cached_epoch_state
    db.init_db()
    log.info("Laboratory Assistant v6 starting...")

    # Pre-fetch epoch state so alerts have tax data immediately
    cached_epoch_state = await fetch_epoch_state()
    if cached_epoch_state:
        log.info(f"Initial epoch state: epoch {cached_epoch_state.get('epoch')}, "
                 f"cheap={cached_epoch_state.get('cheap_side')}")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    handlers = [
        ("status",       cmd_status),
        ("health",       cmd_health),
        ("epoch",        cmd_epoch),
        ("price",        cmd_price),
        ("staking",      cmd_staking),
        ("carnage",      cmd_carnage),
        ("analytics",    cmd_analytics),
        ("toggle",       cmd_toggle),
        ("threshold",    cmd_threshold),
        ("setimage",     cmd_setimage),
        ("removeimage",  cmd_removeimage),
        ("preview",      cmd_preview),
        ("stats",        cmd_stats),
        ("botstats",     cmd_botstats),
        ("apy",          cmd_apy),
        ("supply",       cmd_supply),
        ("liquidity",    cmd_liquidity),
        ("addadmin",     cmd_addadmin),
        ("removeadmin",  cmd_removeadmin),
        ("admins",       cmd_admins),
        ("sol",          cmd_sol),
        ("rules",        cmd_rules),
        ("chart",        cmd_chart),
        ("help",         cmd_help),
    ]
    for cmd, handler in handlers:
        app.add_handler(CommandHandler(cmd, handler))

    app.add_handler(CallbackQueryHandler(chart_callback, pattern=r"^chart_"))

    app.add_handler(MessageHandler(
        filters.PHOTO | filters.ANIMATION | filters.Document.GIF,
        handle_media
    ))

    await app.initialize()
    await app.start()
    bot = app.bot

    await send_startup(bot)

    log.info("All systems starting...")
    await asyncio.gather(
        update_sol_price(),
        update_fdv_cache(),
        poll_program(bot, TAX_PROGRAM, "Tax", parse_and_dispatch),
        poll_staking(bot),
        monitor_epochs(bot),
        watchdog(bot),
        app.updater.start_polling(),
    )

if __name__ == "__main__":
    asyncio.run(main())
