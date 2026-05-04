# ============================================================
# DR. FRAUDSWORTH — LABORATORY ASSISTANT BOT v5
# config.py
# ============================================================

import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

# Super Admin — cannot be removed, can use all commands privately
SUPER_ADMIN_ID = int(os.environ["SUPER_ADMIN_ID"])

# Chat IDs
TEST_CHAT_ID = int(os.environ["TEST_CHAT_ID"])
LIVE_CHAT_ID = int(os.environ["LIVE_CHAT_ID"])

# Currently active group — use /setchat live when ready
ACTIVE_CHAT_ID = TEST_CHAT_ID

# Solana RPC — Helius keys (dual-key failover)
HELIUS_API_KEYS = [
    os.environ["HELIUS_API_KEY_1"],
    os.environ["HELIUS_API_KEY_2"],
]
RPC_HTTP = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEYS[0]}"
RPC_WS   = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEYS[0]}"

# ============================================================
# TOKEN MINTS
# ============================================================
CRIME_MINT  = "cRiMEhAxoDhcEuh3Yf7Z2QkXUXUMKbakhcVqmDsqPXc"
FRAUD_MINT  = "FraUdp6YhtVJYPxC2w255yAbpTsPqd8Bfhy9rC56jau5"
PROFIT_MINT = "pRoFiTj36haRD5sG2Neqib9KoSrtdYMGrM7SEkZetfR"

# ============================================================
# PROGRAM IDs
# ============================================================
AMM_PROGRAM     = "5JsSAL3kJDUWD4ZveYXYZmgm1eVqueesTZVdAvtZg8cR"
TAX_PROGRAM     = "43fZGRtmEsP7ExnJE1dbTbNjaP1ncvVmMPusSeksWGEj"
EPOCH_PROGRAM   = "4Heqc8QEjJCspHR8y96wgZBnBfbe3Qb8N6JBZMQt9iw2"
STAKING_PROGRAM = "12b3t1cNiAUoYLiWFEnFa4w6qYxVAiqCWU7KZuzLPYtH"
VAULT_PROGRAM   = "5uawA6ehYTu69Ggvm3LSK84qFawPKxbWgfngwj15NRJ"
HOOK_PROGRAM    = "CiQPQrmQh6BPhb9k7dFnsEs5gKPgdrvNKFc5xie5xVGd"

# ============================================================
# POOL VAULT ACCOUNTS
# ============================================================
CRIME_WSOL_VAULT  = "14rFLiXzXk7aXLnwAz2kwQUjG9vauS84AQLu6LH9idUM"
CRIME_TOKEN_VAULT = "6s6cprCGxTAYCk9LiwCpCsdHzReW7CLZKqy3ZSCtmV1b"
FRAUD_WSOL_VAULT  = "3sUDyw1k61NSKgn2EA9CaS3FbSZAApGeCRNwNFQPwg8o"
FRAUD_TOKEN_VAULT = "2nzqXn6FivXjPSgrUGTA58eeVUDjGhvn4QLfhXK1jbjP"

# ============================================================
# POOL STATE ACCOUNTS (for raw reserve reading)
# ============================================================
CRIME_POOL_STATE = "ZWUZ3PzGk6bg6g3BS3WdXKbdAecUgZxnruKXQkte7wf"
FRAUD_POOL_STATE = "AngvViTVGd2zxP8KoFUjGU3TyrQjqeM1idRWiKM8p3mq"

# ============================================================
# KEY ACCOUNTS
# ============================================================
CARNAGE_VAULT    = "5988CYMcvJpNtGbtCDnAMxrjrLxRCq3qPME7w2v36aNT"
EPOCH_STATE      = "FjJrLcmDjA8FtavGWdhJq3pdirAH889oWXc2bhEAMbDU"
STAKE_POOL       = "5BdRPPwEDpHEtRgdp4MfywbwmZnrf6u23bXMnG1w8ViN"
STAKING_ESCROW   = "E68zPDgzMqnycj23g9T74ioHbDdvq3Npj5tT2yPd1SY"
VAULT_CRIME      = "Gh9QHMY3J2NGyaHFH2XQCWxedf4G7kBfyu7Jonwn1bHA"
VAULT_FRAUD      = "DLciB9t3qEuRcndGyjRmu1Z34NCwTPvNwbv7eUsFxTZG"
VAULT_PROFIT     = "DBMaWgfUW8WBb8VVvqDFkrMpEkPkCPTcLpSpyzHAiwp3"
STAKING_PROFIT_VAULT = "9knYFeYSupqdhQv6yyMv6q1FGpD5L3q3yaym7N5Lwafo"
TREASURY         = "3ihhwLnEJ2duwPSLYxhLbFrdhhxXLcvcrV9rAHqMgzCv"
WSOL_INTERMEDIARY= "2HPNULWVVdTcRiAm2DkghLA6frXxA2Nsu4VRu8a4qQ1s"

# ============================================================
# EPOCH STATE LAYOUT (byte offsets after 8-byte discriminator)
# From the arb spec doc
# ============================================================
EPOCH_OFFSET_GENESIS_SLOT      = 0   # u64 (8 bytes)
EPOCH_OFFSET_CURRENT_EPOCH     = 8   # u32 (4 bytes)
EPOCH_OFFSET_EPOCH_START_SLOT  = 12  # u64 (8 bytes)
EPOCH_OFFSET_CHEAP_SIDE        = 20  # u8  (1 byte) 0=CRIME cheap, 1=FRAUD cheap
EPOCH_OFFSET_LOW_TAX_BPS       = 21  # u16 (2 bytes)
EPOCH_OFFSET_HIGH_TAX_BPS      = 23  # u16 (2 bytes)
EPOCH_OFFSET_CRIME_BUY_TAX     = 25  # u16 (2 bytes)
EPOCH_OFFSET_CRIME_SELL_TAX    = 27  # u16 (2 bytes)
EPOCH_OFFSET_FRAUD_BUY_TAX     = 29  # u16 (2 bytes)
EPOCH_OFFSET_FRAUD_SELL_TAX    = 31  # u16 (2 bytes)
EPOCH_OFFSET_CARNAGE_PENDING   = 75  # u8  (1 byte)
EPOCH_OFFSET_CARNAGE_TARGET    = 76  # u8  (1 byte) 0=CRIME, 1=FRAUD

# Pool state reserve offsets (after 8-byte discriminator)
POOL_OFFSET_RESERVE_A = 129  # u64 SOL reserves
POOL_OFFSET_RESERVE_B = 137  # u64 token reserves

# ============================================================
# SUPPLY
# ============================================================
TOTAL_SUPPLY  = 1_000_000_000
PROFIT_SUPPLY = 20_000_000

# ============================================================
# DEFAULT RUNTIME SETTINGS
# ============================================================
DEFAULT_SETTINGS = {
    "chat_id":              ACTIVE_CHAT_ID,
    "min_buy_sol":          2.0,
    "medium_buy_sol":       2.0,
    "whale_buy_sol":        10.0,
    "mega_whale_sol":       50.0,
    "min_sell_tax_sol":     0.5,
    "alerts_crime_buys":    True,
    "alerts_fraud_buys":    True,
    "alerts_profit_stakes": True,
    "alerts_carnage":       True,
    "alerts_epoch_flips":   True,
    "alerts_sells":         False,
}
