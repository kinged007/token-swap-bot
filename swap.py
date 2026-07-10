#!/usr/bin/env python3
"""Token Swap Bot — Gasless swaps via GasSponsor contract + PancakeSwap V3.

Architecture:
  - Gas-Refund Loop (one-time): fund wallet (if needed) -> approve GasSponsor
    for token_in AND token_out -> sweep ALL BNB back to master (wallet needs 0 BNB after init)
  - Sponsored Swap: master wallet calls GasSponsor.execute() to pull tokens,
    swap via PancakeSwap V3, and deliver output to target wallet
  - Collection Transfer: forward swapped tokens to a dedicated collection wallet
    (auto-approves token_out for legacy wallets if allowance is insufficient)

One wallet, one transaction per run. Respects STOP_BOT kill switch.
"""
import builtins
import json, os, re, sys, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from eth_account import Account
from hexbytes import HexBytes
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

import wallet_manager as wm


def _out(*args, **kwargs):
    """Print that always outputs (even in --quiet mode)."""
    if hasattr(builtins, '_original_print'):
        builtins._original_print(*args, **kwargs)
    else:
        builtins.print(*args, **kwargs)


def _error_exit(msg: str, code: int = 1):
    """Print error message and exit (visible even in --quiet mode)."""
    _out(f"❌ {msg}")
    sys.exit(code)

# ══════════════════════════════════════════════════════════════════
# ABIs
# ══════════════════════════════════════════════════════════════════

ERC20_ABI = json.loads("""[
  {"inputs":[{"name":"owner","type":"address"}],"name":"balanceOf",
   "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
  {"inputs":[],"name":"decimals",
   "outputs":[{"name":"","type":"uint8"}],"stateMutability":"view","type":"function"},
  {"inputs":[],"name":"symbol",
   "outputs":[{"name":"","type":"string"}],"stateMutability":"view","type":"function"},
  {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],
   "name":"approve","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
  {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],
   "name":"allowance",
   "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
  {"inputs":[{"name":"to","type":"address"},{"name":"amount","type":"uint256"}],
   "name":"transfer","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
  {"inputs":[{"name":"from","type":"address"},{"name":"to","type":"address"},{"name":"amount","type":"uint256"}],
   "name":"transferFrom","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"}
]""")

GAS_SPONSOR_ABI = json.loads("""[
  {"inputs":[{"name":"to","type":"address"},{"name":"data","type":"bytes"},
   {"name":"value","type":"uint256"}],"name":"execute",
   "outputs":[{"name":"result","type":"bytes"}],"stateMutability":"nonpayable","type":"function"},
  {"inputs":[{"name":"","type":"address"}],"name":"whitelist",
   "outputs":[{"name":"","type":"bool"}],"stateMutability":"view","type":"function"},
  {"inputs":[{"name":"account","type":"address"},{"name":"status","type":"bool"}],
   "name":"updateWhitelist",
   "outputs":[],"stateMutability":"nonpayable","type":"function"},
  {"inputs":[{"name":"accounts","type":"address[]"},{"name":"statuses","type":"bool[]"}],
   "name":"updateWhitelistBulk",
   "outputs":[],"stateMutability":"nonpayable","type":"function"},
  {"inputs":[],"name":"owner",
   "outputs":[{"name":"","type":"address"}],"stateMutability":"view","type":"function"},
  {"inputs":[],"name":"emergencyWithdrawBNB",
   "outputs":[],"stateMutability":"nonpayable","type":"function"},
  {"inputs":[{"name":"token","type":"address"}],"name":"emergencyWithdrawToken",
   "outputs":[],"stateMutability":"nonpayable","type":"function"},
  {"inputs":[],"name":"renounceOwnership",
   "outputs":[],"stateMutability":"nonpayable","type":"function"},
  {"inputs":[{"name":"newOwner","type":"address"}],"name":"transferOwnership",
   "outputs":[],"stateMutability":"nonpayable","type":"function"},
  {"stateMutability":"payable","type":"receive"}
]""")

QUOTER_ABI = json.loads("""[
  {"inputs":[{"components":[
    {"name":"tokenIn","type":"address"},
    {"name":"tokenOut","type":"address"},
    {"name":"amountIn","type":"uint256"},
    {"name":"fee","type":"uint24"},
    {"name":"sqrtPriceLimitX96","type":"uint160"}
   ],"name":"params","type":"tuple"}],
   "name":"quoteExactInputSingle",
   "outputs":[{"name":"amountOut","type":"uint256"},
    {"name":"sqrtPriceX96After","type":"uint160"},
    {"name":"initializedTicksCrossed","type":"uint32"},
    {"name":"gasEstimate","type":"uint256"}],
   "stateMutability":"nonpayable","type":"function"}
]""")

ROUTER_ABI = json.loads("""[
  {"inputs":[{"components":[
    {"name":"tokenIn","type":"address"},
    {"name":"tokenOut","type":"address"},
    {"name":"fee","type":"uint24"},
    {"name":"recipient","type":"address"},
    {"name":"deadline","type":"uint256"},
    {"name":"amountIn","type":"uint256"},
    {"name":"amountOutMinimum","type":"uint256"},
    {"name":"sqrtPriceLimitX96","type":"uint160"}
   ],"name":"params","type":"tuple"}],
   "name":"exactInputSingle",
   "outputs":[{"name":"amountOut","type":"uint256"}],
   "stateMutability":"payable","type":"function"},
  {"inputs":[{"name":"data","type":"bytes[]"}],"name":"multicall",
   "outputs":[{"name":"results","type":"bytes[]"}],
   "stateMutability":"payable","type":"function"}
]""")

MAX_UINT256 = 2**256 - 1
ZERO_ADDR = "0x0000000000000000000000000000000000000000"


# ══════════════════════════════════════════════════════════════════
# Config & Env Loading
# ══════════════════════════════════════════════════════════════════

def load_env():
    """Load .env file if present."""
    env_path = Path(".env")
    if env_path.exists():
        load_dotenv(env_path)


def load_config(path="config.yaml") -> dict:
    """Load YAML config with env var substitution."""
    with open(path) as f:
        raw = f.read()
    raw = re.sub(r'\$\{(\w+)\}', lambda m: os.environ.get(m.group(1), ""), raw)
    return yaml.safe_load(raw)


# ══════════════════════════════════════════════════════════════════
# Logging
# ══════════════════════════════════════════════════════════════════

def log_event(log_file: str, event_type: str, data: dict):
    """Append a JSON line to the log file."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        **data,
    }
    with open(log_file, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ══════════════════════════════════════════════════════════════════
# Gas Tracker (daily BNB spending limit)
# ══════════════════════════════════════════════════════════════════

def load_gas_tracker(path: str) -> dict:
    """Load daily gas tracker from JSON file."""
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_gas_tracker(path: str, data: dict):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def check_daily_gas_limit(tracker_path: str, limit_bnb: float) -> bool:
    """Return True if under daily limit, False if exceeded."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tracker = load_gas_tracker(tracker_path)
    spent = tracker.get(today, 0.0)
    if spent >= limit_bnb:
        print(f"  ALERT: Daily gas limit reached ({spent:.4f} / {limit_bnb} BNB)")
        return False
    return True


def record_gas_spent(tracker_path: str, bnb_amount: float):
    """Record BNB spent today."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tracker = load_gas_tracker(tracker_path)
    tracker[today] = tracker.get(today, 0.0) + bnb_amount
    save_gas_tracker(tracker_path, tracker)


# ══════════════════════════════════════════════════════════════════
# Web3 Helpers
# ══════════════════════════════════════════════════════════════════

def connect_web3(rpc_url: str) -> Web3:
    """Connect to BSC node with POA middleware."""
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
    # BSC uses POA consensus — inject middleware
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if not w3.is_connected():
        raise ConnectionError(f"Cannot connect to RPC: {rpc_url}")
    print(f"  Connected to chain {w3.eth.chain_id}, block {w3.eth.block_number}")
    return w3


def get_token_balance(w3: Web3, token_addr: str, wallet_addr: str) -> int:
    """Get ERC-20 token balance in wei."""
    c = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI)
    return c.functions.balanceOf(Web3.to_checksum_address(wallet_addr)).call()


def get_token_allowance(w3: Web3, token_addr: str, owner: str, spender: str) -> int:
    """Get ERC-20 allowance in wei."""
    c = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI)
    return c.functions.allowance(
        Web3.to_checksum_address(owner),
        Web3.to_checksum_address(spender),
    ).call()


def send_and_wait(w3: Web3, signed_tx, label: str, receipt_timeout: int = 120) -> dict:
    """Send a signed transaction and wait for receipt.

    Returns the receipt dict. Raises on failure or timeout.
    """
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    print(f"    TX sent: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=receipt_timeout)
    status = "OK" if receipt["status"] == 1 else "REVERTED"
    gas_used = receipt["gasUsed"]
    gas_bnb = w3.from_wei(gas_used * receipt.get("effectiveGasPrice", 0), "ether")
    print(f"    {label}: {status} (gas: {gas_used}, cost: {gas_bnb:.6f} BNB)")
    if receipt["status"] != 1:
        raise RuntimeError(f"Transaction {label} reverted: {tx_hash.hex()}")
    return receipt


def sign_and_send(w3: Web3, account_key: str, tx: dict, label: str,
                  receipt_timeout: int = 120) -> dict:
    """Sign a transaction dict and send it. Returns receipt."""
    signed = w3.eth.account.sign_transaction(tx, account_key)
    return send_and_wait(w3, signed, label, receipt_timeout)


# ══════════════════════════════════════════════════════════════════
# Gas-Refund Loop (One-Time Wallet Initialization)
# ══════════════════════════════════════════════════════════════════

def execute_gas_refund_loop(
    w3: Web3,
    master_key: str,
    master_addr: str,
    wallet_addr: str,
    wallet_key: str,
    token_in_addr: str,
    token_out_addr: str,
    gas_sponsor_addr: str,
    max_retries: int = 3,
    receipt_timeout: int = 120,
) -> bool:
    """Run the one-time gas-refund loop:
    1. Send BNB from master to target wallet (only if wallet has insufficient BNB)
    2. Target wallet approves GasSponsor for unlimited token_in AND token_out spending
    3. Sweep ALL BNB from wallet back to master (since future txns are sponsored)

    Returns True on success, False on failure.
    """
    print("\n  === Gas-Refund Loop (One-Time Init) ===")
    gas_price = w3.eth.gas_price
    print(f"  Current gas price: {w3.from_wei(gas_price, 'gwei')} gwei")

    # ── Step 1: Estimate gas for approve (token_in + token_out) + sweep ──
    token_in_c = w3.eth.contract(
        address=Web3.to_checksum_address(token_in_addr), abi=ERC20_ABI
    )
    token_out_c = w3.eth.contract(
        address=Web3.to_checksum_address(token_out_addr), abi=ERC20_ABI
    )

    # Estimate approve gas (same for both tokens, estimate once)
    try:
        approve_tx = token_in_c.functions.approve(
            Web3.to_checksum_address(gas_sponsor_addr), MAX_UINT256
        ).build_transaction({
            "from": Web3.to_checksum_address(wallet_addr),
            "gas_price": gas_price,
            "nonce": 0,
            "chainId": w3.eth.chain_id,
        })
        approve_gas = w3.eth.estimate_gas(approve_tx)
    except Exception:
        approve_gas = 60000  # safe fallback
    print(f"  Estimated approve gas: {approve_gas} (x2 for token_in + token_out)")

    # Estimate sweep (simple BNB transfer) gas
    sweep_gas = 21000  # standard transfer

    # Total BNB needed = 2x approve cost + sweep cost + 15% buffer
    total_gas_cost_wei = int(
        (2 * approve_gas + sweep_gas) * gas_price * 1.15
    )
    print(f"  BNB needed for gas: {w3.from_wei(total_gas_cost_wei, 'ether')} (with 15% buffer)")

    # ── Step 2: Ensure wallet has BNB for approve (fund from master if needed) ──
    wallet_bnb = w3.eth.get_balance(Web3.to_checksum_address(wallet_addr))
    print(f"\n  Wallet BNB balance: {w3.from_wei(wallet_bnb, 'ether')} BNB")

    if wallet_bnb < total_gas_cost_wei:
        # Wallet needs BNB — send from master
        master_balance = w3.eth.get_balance(Web3.to_checksum_address(master_addr))
        if master_balance < total_gas_cost_wei:
            print(f"  ERROR: Master wallet has insufficient BNB ({w3.from_wei(master_balance, 'ether')})")
            return False

        print(f"  Step 1/3: Sending BNB to target wallet...")
        for attempt in range(1, max_retries + 1):
            try:
                nonce = w3.eth.get_transaction_count(
                    Web3.to_checksum_address(master_addr), "pending"
                )
                fund_tx = {
                    "from": Web3.to_checksum_address(master_addr),
                    "to": Web3.to_checksum_address(wallet_addr),
                    "value": total_gas_cost_wei,
                    "gas": 21000,
                    "gasPrice": gas_price,
                    "nonce": nonce,
                    "chainId": w3.eth.chain_id,
                }
                receipt = sign_and_send(w3, master_key, fund_tx, "Fund wallet", receipt_timeout)
                gas_bnb = float(w3.from_wei(
                    receipt["gasUsed"] * receipt.get("effectiveGasPrice", gas_price), "ether"
                ))
                record_gas_spent("gas_tracker.json", gas_bnb)
                break
            except Exception as e:
                print(f"    Attempt {attempt}/{max_retries} failed: {e}")
                if attempt == max_retries:
                    print("  ERROR: Failed to fund wallet after max retries")
                    return False
                time.sleep(2 ** attempt)
                gas_price = w3.eth.gas_price  # refresh gas price
    else:
        print(f"  Wallet already has sufficient BNB — skipping master funding")

    # ── Step 3: Approve GasSponsor for token_in ──
    print(f"\n  Step 2/4: Approving GasSponsor for token_in...")
    for attempt in range(1, max_retries + 1):
        try:
            wallet_nonce = w3.eth.get_transaction_count(
                Web3.to_checksum_address(wallet_addr), "pending"
            )
            approve_tx = token_in_c.functions.approve(
                Web3.to_checksum_address(gas_sponsor_addr), MAX_UINT256
            ).build_transaction({
                "from": Web3.to_checksum_address(wallet_addr),
                "gas": approve_gas + 5000,  # small buffer
                "gasPrice": gas_price,
                "nonce": wallet_nonce,
                "chainId": w3.eth.chain_id,
            })
            receipt = sign_and_send(w3, wallet_key, approve_tx, "Approve token_in", receipt_timeout)
            gas_bnb = float(w3.from_wei(
                receipt["gasUsed"] * receipt.get("effectiveGasPrice", gas_price), "ether"
            ))
            record_gas_spent("gas_tracker.json", gas_bnb)
            break
        except Exception as e:
            print(f"    Attempt {attempt}/{max_retries} failed: {e}")
            if attempt == max_retries:
                print("  ERROR: token_in approve failed after max retries")
                return False
            time.sleep(2 ** attempt)
            gas_price = w3.eth.gas_price

    # ── Step 4: Approve GasSponsor for token_out ──
    print(f"\n  Step 3/4: Approving GasSponsor for token_out...")
    for attempt in range(1, max_retries + 1):
        try:
            wallet_nonce = w3.eth.get_transaction_count(
                Web3.to_checksum_address(wallet_addr), "pending"
            )
            approve_tx = token_out_c.functions.approve(
                Web3.to_checksum_address(gas_sponsor_addr), MAX_UINT256
            ).build_transaction({
                "from": Web3.to_checksum_address(wallet_addr),
                "gas": approve_gas + 5000,
                "gasPrice": gas_price,
                "nonce": wallet_nonce,
                "chainId": w3.eth.chain_id,
            })
            receipt = sign_and_send(w3, wallet_key, approve_tx, "Approve token_out", receipt_timeout)
            gas_bnb = float(w3.from_wei(
                receipt["gasUsed"] * receipt.get("effectiveGasPrice", gas_price), "ether"
            ))
            record_gas_spent("gas_tracker.json", gas_bnb)
            break
        except Exception as e:
            print(f"    Attempt {attempt}/{max_retries} failed: {e}")
            if attempt == max_retries:
                print("  WARNING: token_out approve failed — collection may require manual approval")
                # Non-fatal — token_in approve succeeded, which is the critical part
            time.sleep(2 ** attempt)
            gas_price = w3.eth.gas_price

    # ── Verify allowances before sweeping ──
    allowance_in = get_token_allowance(w3, token_in_addr, wallet_addr, gas_sponsor_addr)
    allowance_out = get_token_allowance(w3, token_out_addr, wallet_addr, gas_sponsor_addr)
    if allowance_in < MAX_UINT256 // 2:
        print(f"  ERROR: token_in allowance not set correctly: {allowance_in}")
        return False
    print(f"  token_in allowance:  MAX")
    print(f"  token_out allowance: {'MAX' if allowance_out > MAX_UINT256 // 2 else allowance_out}")

    # ── Step 5: Sweep ALL BNB back to master ──
    # The wallet only needs BNB for the approve txs. All BNB (including any
    # pre-existing balance) is swept back since future txns are sponsored.
    print(f"\n  Step 4/4: Sweeping ALL BNB back to master...")
    for attempt in range(1, max_retries + 1):
        try:
            wallet_balance = w3.eth.get_balance(Web3.to_checksum_address(wallet_addr))
            gas_price = w3.eth.gas_price  # refresh
            sweep_cost = sweep_gas * gas_price
            sweep_amount = wallet_balance - sweep_cost

            if sweep_amount <= 0:
                print(f"  No BNB to sweep (balance: {w3.from_wei(wallet_balance, 'ether')})")
                break

            wallet_nonce = w3.eth.get_transaction_count(
                Web3.to_checksum_address(wallet_addr), "pending"
            )
            sweep_tx = {
                "from": Web3.to_checksum_address(wallet_addr),
                "to": Web3.to_checksum_address(master_addr),
                "value": sweep_amount,
                "gas": sweep_gas,
                "gasPrice": gas_price,
                "nonce": wallet_nonce,
                "chainId": w3.eth.chain_id,
            }
            receipt = sign_and_send(w3, wallet_key, sweep_tx, "Sweep BNB", receipt_timeout)
            gas_bnb = float(w3.from_wei(
                receipt["gasUsed"] * receipt.get("effectiveGasPrice", gas_price), "ether"
            ))
            record_gas_spent("gas_tracker.json", gas_bnb)
            print(f"  Swept {w3.from_wei(sweep_amount, 'ether')} BNB back to master")
            break
        except Exception as e:
            print(f"    Attempt {attempt}/{max_retries} failed: {e}")
            if attempt == max_retries:
                print("  WARNING: Sweep failed — some BNB may be trapped in wallet")
                # Not a hard failure — the approve succeeded, which is the important part
            time.sleep(2 ** attempt)

    # ── Verify final state ──
    final_bnb = w3.eth.get_balance(Web3.to_checksum_address(wallet_addr))
    final_allowance_in = get_token_allowance(w3, token_in_addr, wallet_addr, gas_sponsor_addr)
    final_allowance_out = get_token_allowance(w3, token_out_addr, wallet_addr, gas_sponsor_addr)
    print(f"\n  Init complete:")
    print(f"    Wallet BNB balance:    {w3.from_wei(final_bnb, 'ether')} (0 expected — txns are sponsored)")
    print(f"    token_in allowance:    {'MAX' if final_allowance_in > MAX_UINT256 // 2 else final_allowance_in}")
    print(f"    token_out allowance:   {'MAX' if final_allowance_out > MAX_UINT256 // 2 else final_allowance_out}")

    return final_allowance_in > MAX_UINT256 // 2


# ══════════════════════════════════════════════════════════════════
# PancakeSwap V3 Quoter
# ══════════════════════════════════════════════════════════════════

def get_pancake_quote(
    w3: Web3,
    quoter_addr: str,
    token_in: str,
    token_out: str,
    amount_in_wei: int,
    fee_tiers: list,
) -> tuple:
    """Query QuoterV2 for expected output. Tries multiple fee tiers.

    Returns (amount_out, fee_tier) or (0, 0) if all fail.
    """
    quoter = w3.eth.contract(
        address=Web3.to_checksum_address(quoter_addr), abi=QUOTER_ABI
    )

    for fee in fee_tiers:
        try:
            result = quoter.functions.quoteExactInputSingle((
                Web3.to_checksum_address(token_in),
                Web3.to_checksum_address(token_out),
                amount_in_wei,
                fee,
                0,  # sqrtPriceLimitX96 = 0 means no limit
            )).call()
            amount_out = result[0]
            if amount_out > 0:
                print(f"  Quote (fee={fee}): {amount_in_wei} -> {amount_out}")
                return amount_out, fee
        except Exception as e:
            print(f"  Fee tier {fee}: no liquidity ({e})")
            continue

    print("  ERROR: No valid quote from any fee tier")
    return 0, 0


# ══════════════════════════════════════════════════════════════════
# Sponsored Swap via GasSponsor Contract
# ══════════════════════════════════════════════════════════════════

def execute_sponsored_swap(
    w3: Web3,
    master_key: str,
    master_addr: str,
    wallet_addr: str,
    token_in_addr: str,
    token_out_addr: str,
    amount_in_wei: int,
    min_amount_out: int,
    gas_sponsor_addr: str,
    router_addr: str,
    fee_tier: int,
    deadline_seconds: int = 180,
    max_retries: int = 3,
    receipt_timeout: int = 120,
) -> bool:
    """Execute a sponsored swap via the GasSponsor contract.

    Flow (all calls are master->GasSponsor.execute()):
      A) transferFrom(user -> gasSponsor) — pull tokens to contract
      B) multicall([approve(router), exactInputSingle]) — swap via PancakeSwap
      C) transfer(gasSponsor -> user) — deliver output tokens to user

    Returns True on success.
    """
    print("\n  === Sponsored Swap ===")
    gas_sponsor = w3.eth.contract(
        address=Web3.to_checksum_address(gas_sponsor_addr), abi=GAS_SPONSOR_ABI
    )
    token_in_c = w3.eth.contract(
        address=Web3.to_checksum_address(token_in_addr), abi=ERC20_ABI
    )
    token_out_c = w3.eth.contract(
        address=Web3.to_checksum_address(token_out_addr), abi=ERC20_ABI
    )
    router_c = w3.eth.contract(
        address=Web3.to_checksum_address(router_addr), abi=ROUTER_ABI
    )

    gas_price = w3.eth.gas_price
    deadline = int(time.time()) + deadline_seconds

    # ── Step A: Pull tokens from user to GasSponsor ──
    print(f"\n  Step A: Pulling tokens from user wallet to GasSponsor...")
    transfer_from_data = token_in_c.functions.transferFrom(
        Web3.to_checksum_address(wallet_addr),
        Web3.to_checksum_address(gas_sponsor_addr),
        amount_in_wei,
    )._encode_transaction_data()

    for attempt in range(1, max_retries + 1):
        try:
            nonce = w3.eth.get_transaction_count(
                Web3.to_checksum_address(master_addr), "pending"
            )
            exec_tx = gas_sponsor.functions.execute(
                Web3.to_checksum_address(token_in_addr),
                transfer_from_data,
                0,
            ).build_transaction({
                "from": Web3.to_checksum_address(master_addr),
                "gasPrice": gas_price,
                "nonce": nonce,
                "chainId": w3.eth.chain_id,
            })
            # Estimate gas with buffer
            try:
                est = w3.eth.estimate_gas(exec_tx)
                exec_tx["gas"] = int(est * 1.2)
            except Exception:
                exec_tx["gas"] = 200000

            receipt = sign_and_send(w3, master_key, exec_tx, "Pull tokens", receipt_timeout)
            gas_bnb = float(w3.from_wei(
                receipt["gasUsed"] * receipt.get("effectiveGasPrice", gas_price), "ether"
            ))
            record_gas_spent("gas_tracker.json", gas_bnb)
            break
        except Exception as e:
            print(f"    Attempt {attempt}/{max_retries} failed: {e}")
            if attempt == max_retries:
                print("  ERROR: Failed to pull tokens from user wallet")
                return False
            time.sleep(2 ** attempt)
            gas_price = w3.eth.gas_price

    # Verify tokens arrived at GasSponsor
    sponsor_balance = get_token_balance(w3, token_in_addr, gas_sponsor_addr)
    if sponsor_balance < amount_in_wei:
        print(f"  ERROR: Tokens not received by GasSponsor (has {sponsor_balance}, expected {amount_in_wei})")
        return False
    print(f"  Tokens in GasSponsor: {sponsor_balance}")

    # ── Step B1: Approve router to spend tokens held by GasSponsor ──
    print(f"\n  Step B1: Approving router to spend tokens...")
    approve_data = token_in_c.functions.approve(
        Web3.to_checksum_address(router_addr), amount_in_wei
    )._encode_transaction_data()

    for attempt in range(1, max_retries + 1):
        try:
            nonce = w3.eth.get_transaction_count(
                Web3.to_checksum_address(master_addr), "pending"
            )
            exec_tx = gas_sponsor.functions.execute(
                Web3.to_checksum_address(token_in_addr),
                approve_data,
                0,
            ).build_transaction({
                "from": Web3.to_checksum_address(master_addr),
                "gasPrice": gas_price,
                "nonce": nonce,
                "chainId": w3.eth.chain_id,
            })
            try:
                est = w3.eth.estimate_gas(exec_tx)
                exec_tx["gas"] = int(est * 1.2)
            except Exception:
                exec_tx["gas"] = 200000
            receipt = sign_and_send(w3, master_key, exec_tx, "Approve router", receipt_timeout)
            gas_bnb = float(w3.from_wei(
                receipt["gasUsed"] * receipt.get("effectiveGasPrice", gas_price), "ether"
            ))
            record_gas_spent("gas_tracker.json", gas_bnb)
            break
        except Exception as e:
            print(f"    Attempt {attempt}/{max_retries} failed: {e}")
            if attempt == max_retries:
                print("  ERROR: Router approve failed")
                _recover_tokens(w3, master_key, master_addr, gas_sponsor_addr,
                                wallet_addr, token_in_addr, receipt_timeout)
                return False
            time.sleep(2 ** attempt)
            gas_price = w3.eth.gas_price

    # ── Step B2: Execute swap via router ──
    print(f"\n  Step B2: Executing swap via PancakeSwap V3...")
    swap_params = (
        Web3.to_checksum_address(token_in_addr),
        Web3.to_checksum_address(token_out_addr),
        fee_tier,
        Web3.to_checksum_address(gas_sponsor_addr),  # recipient = GasSponsor
        deadline,  # deadline timestamp
        amount_in_wei,
        min_amount_out,
        0,  # sqrtPriceLimitX96
    )
    swap_data = router_c.functions.exactInputSingle(swap_params)._encode_transaction_data()

    for attempt in range(1, max_retries + 1):
        try:
            nonce = w3.eth.get_transaction_count(
                Web3.to_checksum_address(master_addr), "pending"
            )
            exec_tx = gas_sponsor.functions.execute(
                Web3.to_checksum_address(router_addr),
                swap_data,
                0,
            ).build_transaction({
                "from": Web3.to_checksum_address(master_addr),
                "gasPrice": gas_price,
                "nonce": nonce,
                "chainId": w3.eth.chain_id,
            })
            try:
                est = w3.eth.estimate_gas(exec_tx)
                exec_tx["gas"] = int(est * 1.3)  # bigger buffer for swap
            except Exception:
                exec_tx["gas"] = 500000
            receipt = sign_and_send(w3, master_key, exec_tx, "Swap", receipt_timeout)
            gas_bnb = float(w3.from_wei(
                receipt["gasUsed"] * receipt.get("effectiveGasPrice", gas_price), "ether"
            ))
            record_gas_spent("gas_tracker.json", gas_bnb)
            break
        except Exception as e:
            print(f"    Attempt {attempt}/{max_retries} failed: {e}")
            if attempt == max_retries:
                print("  ERROR: Swap failed")
                _recover_tokens(w3, master_key, master_addr, gas_sponsor_addr,
                                wallet_addr, token_in_addr, receipt_timeout)
                return False
            time.sleep(2 ** attempt)
            gas_price = w3.eth.gas_price

    # Check output received by GasSponsor
    output_balance = get_token_balance(w3, token_out_addr, gas_sponsor_addr)
    print(f"  Output tokens in GasSponsor: {output_balance}")

    if output_balance == 0:
        print("  ERROR: No output tokens received from swap")
        _recover_tokens(w3, master_key, master_addr, gas_sponsor_addr,
                        wallet_addr, token_in_addr, receipt_timeout)
        return False

    # ── Step C: Forward output tokens to user wallet ──
    print(f"\n  Step C: Forwarding output tokens to user wallet...")
    transfer_out_data = token_out_c.functions.transfer(
        Web3.to_checksum_address(wallet_addr), output_balance
    )._encode_transaction_data()

    for attempt in range(1, max_retries + 1):
        try:
            nonce = w3.eth.get_transaction_count(
                Web3.to_checksum_address(master_addr), "pending"
            )
            exec_tx = gas_sponsor.functions.execute(
                Web3.to_checksum_address(token_out_addr),
                transfer_out_data,
                0,
            ).build_transaction({
                "from": Web3.to_checksum_address(master_addr),
                "gasPrice": gas_price,
                "nonce": nonce,
                "chainId": w3.eth.chain_id,
            })
            try:
                est = w3.eth.estimate_gas(exec_tx)
                exec_tx["gas"] = int(est * 1.2)
            except Exception:
                exec_tx["gas"] = 100000

            receipt = sign_and_send(w3, master_key, exec_tx, "Forward output", receipt_timeout)
            gas_bnb = float(w3.from_wei(
                receipt["gasUsed"] * receipt.get("effectiveGasPrice", gas_price), "ether"
            ))
            record_gas_spent("gas_tracker.json", gas_bnb)
            break
        except Exception as e:
            print(f"    Attempt {attempt}/{max_retries} failed: {e}")
            if attempt == max_retries:
                print("  ERROR: Failed to forward output tokens to user")
                print(f"  WARNING: {output_balance} output tokens stuck in GasSponsor")
                return False
            time.sleep(2 ** attempt)
            gas_price = w3.eth.gas_price

    # Also return any unspent input tokens from GasSponsor back to user
    remaining_input = get_token_balance(w3, token_in_addr, gas_sponsor_addr)
    if remaining_input > 0:
        print(f"  Returning {remaining_input} unspent input tokens to user...")
        _transfer_from_sponsor_to_user(
            w3, master_key, master_addr, gas_sponsor_addr,
            wallet_addr, token_in_addr, remaining_input, receipt_timeout
        )

    print(f"  Swap complete! {amount_in_wei} input -> {output_balance} output")
    return True


def _recover_tokens(w3, master_key, master_addr, gas_sponsor_addr,
                    user_addr, token_addr, receipt_timeout):
    """Emergency: return tokens from GasSponsor back to user wallet."""
    print("  Attempting to recover tokens from GasSponsor back to user...")
    balance = get_token_balance(w3, token_addr, gas_sponsor_addr)
    if balance == 0:
        print("  No tokens to recover")
        return
    _transfer_from_sponsor_to_user(
        w3, master_key, master_addr, gas_sponsor_addr,
        user_addr, token_addr, balance, receipt_timeout
    )


def _transfer_from_sponsor_to_user(w3, master_key, master_addr, gas_sponsor_addr,
                                    user_addr, token_addr, amount, receipt_timeout):
    """Transfer tokens from GasSponsor contract to user wallet."""
    gas_sponsor = w3.eth.contract(
        address=Web3.to_checksum_address(gas_sponsor_addr), abi=GAS_SPONSOR_ABI
    )
    token_c = w3.eth.contract(
        address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI
    )
    transfer_data = token_c.functions.transfer(
        Web3.to_checksum_address(user_addr), amount
    )._encode_transaction_data()

    try:
        nonce = w3.eth.get_transaction_count(
            Web3.to_checksum_address(master_addr), "pending"
        )
        exec_tx = gas_sponsor.functions.execute(
            Web3.to_checksum_address(token_addr),
            transfer_data,
            0,
        ).build_transaction({
            "from": Web3.to_checksum_address(master_addr),
            "gasPrice": w3.eth.gas_price,
            "nonce": nonce,
            "chainId": w3.eth.chain_id,
        })
        try:
            est = w3.eth.estimate_gas(exec_tx)
            exec_tx["gas"] = int(est * 1.2)
        except Exception:
            exec_tx["gas"] = 100000
        sign_and_send(w3, master_key, exec_tx, "Recover tokens", receipt_timeout)
        print(f"  Recovered {amount} tokens to {user_addr}")
    except Exception as e:
        print(f"  WARNING: Token recovery failed: {e}")


# ══════════════════════════════════════════════════════════════════
# Token-Out Approval (for legacy wallets)
# ══════════════════════════════════════════════════════════════════

def ensure_token_out_approval(
    w3: Web3,
    master_key: str,
    master_addr: str,
    wallet_addr: str,
    wallet_key: str,
    token_out_addr: str,
    gas_sponsor_addr: str,
    required_amount: int,
    max_retries: int = 3,
    receipt_timeout: int = 120,
) -> bool:
    """Ensure wallet has approved GasSponsor for token_out.

    If allowance is already sufficient, returns True immediately.
    Otherwise, funds the wallet with BNB for one approve tx and approves.
    This handles legacy wallets initialized before token_out approval was added.
    """
    allowance = get_token_allowance(w3, token_out_addr, wallet_addr, gas_sponsor_addr)
    if allowance >= required_amount:
        return True

    print(f"  token_out allowance insufficient — auto-approving GasSponsor...")
    gas_price = w3.eth.gas_price

    # Estimate approve gas
    token_out_c = w3.eth.contract(
        address=Web3.to_checksum_address(token_out_addr), abi=ERC20_ABI
    )
    try:
        approve_tx = token_out_c.functions.approve(
            Web3.to_checksum_address(gas_sponsor_addr), MAX_UINT256
        ).build_transaction({
            "from": Web3.to_checksum_address(wallet_addr),
            "gas_price": gas_price,
            "nonce": 0,
            "chainId": w3.eth.chain_id,
        })
        approve_gas = w3.eth.estimate_gas(approve_tx)
    except Exception:
        approve_gas = 60000

    # Fund wallet with BNB for the approve tx (no sweep — tiny amount)
    fund_amount = int(approve_gas * gas_price * 1.2)  # 20% buffer
    wallet_bnb = w3.eth.get_balance(Web3.to_checksum_address(wallet_addr))

    if wallet_bnb < fund_amount:
        for attempt in range(1, max_retries + 1):
            try:
                nonce = w3.eth.get_transaction_count(
                    Web3.to_checksum_address(master_addr), "pending"
                )
                fund_tx = {
                    "from": Web3.to_checksum_address(master_addr),
                    "to": Web3.to_checksum_address(wallet_addr),
                    "value": fund_amount,
                    "gas": 21000,
                    "gasPrice": gas_price,
                    "nonce": nonce,
                    "chainId": w3.eth.chain_id,
                }
                sign_and_send(w3, master_key, fund_tx, "Fund for approve", receipt_timeout)
                break
            except Exception as e:
                print(f"    Fund attempt {attempt}/{max_retries} failed: {e}")
                if attempt == max_retries:
                    print(f"  ERROR: Failed to fund wallet for token_out approval")
                    return False
                time.sleep(2 ** attempt)
                gas_price = w3.eth.gas_price
                fund_amount = int(approve_gas * gas_price * 1.2)

    # Approve token_out for GasSponsor
    for attempt in range(1, max_retries + 1):
        try:
            wallet_nonce = w3.eth.get_transaction_count(
                Web3.to_checksum_address(wallet_addr), "pending"
            )
            approve_tx = token_out_c.functions.approve(
                Web3.to_checksum_address(gas_sponsor_addr), MAX_UINT256
            ).build_transaction({
                "from": Web3.to_checksum_address(wallet_addr),
                "gas": approve_gas + 5000,
                "gasPrice": gas_price,
                "nonce": wallet_nonce,
                "chainId": w3.eth.chain_id,
            })
            sign_and_send(w3, wallet_key, approve_tx, "Approve token_out", receipt_timeout)
            break
        except Exception as e:
            print(f"    Approve attempt {attempt}/{max_retries} failed: {e}")
            if attempt == max_retries:
                print(f"  ERROR: token_out auto-approval failed")
                return False
            time.sleep(2 ** attempt)
            gas_price = w3.eth.gas_price

    # Verify
    new_allowance = get_token_allowance(w3, token_out_addr, wallet_addr, gas_sponsor_addr)
    if new_allowance >= required_amount:
        print(f"  token_out auto-approval: OK")
        return True
    print(f"  WARNING: token_out allowance still insufficient after approval ({new_allowance})")
    return False


# ══════════════════════════════════════════════════════════════════
# Collection Wallet Transfer
# ══════════════════════════════════════════════════════════════════

def execute_collection_transfer(
    w3: Web3,
    master_key: str,
    master_addr: str,
    wallet_addr: str,
    token_out_addr: str,
    collection_addr: str,
    gas_sponsor_addr: str,
    receipt_timeout: int = 120,
) -> bool:
    """Transfer swapped tokens from user wallet to collection wallet via GasSponsor.

    Returns True on success.
    """
    if not collection_addr or collection_addr == ZERO_ADDR:
        print("  Collection wallet not configured — skipping transfer")
        return True

    balance = get_token_balance(w3, token_out_addr, wallet_addr)
    if balance == 0:
        print("  No output tokens in wallet to collect")
        return True

    print(f"\n  === Collection Transfer ===")
    print(f"  Transferring {balance} output tokens to collection wallet...")

    gas_sponsor = w3.eth.contract(
        address=Web3.to_checksum_address(gas_sponsor_addr), abi=GAS_SPONSOR_ABI
    )
    token_out_c = w3.eth.contract(
        address=Web3.to_checksum_address(token_out_addr), abi=ERC20_ABI
    )

    # First pull tokens from user to GasSponsor
    transfer_from_data = token_out_c.functions.transferFrom(
        Web3.to_checksum_address(wallet_addr),
        Web3.to_checksum_address(gas_sponsor_addr),
        balance,
    )._encode_transaction_data()

    try:
        nonce = w3.eth.get_transaction_count(
            Web3.to_checksum_address(master_addr), "pending"
        )
        exec_tx = gas_sponsor.functions.execute(
            Web3.to_checksum_address(token_out_addr),
            transfer_from_data,
            0,
        ).build_transaction({
            "from": Web3.to_checksum_address(master_addr),
            "gasPrice": w3.eth.gas_price,
            "nonce": nonce,
            "chainId": w3.eth.chain_id,
        })
        try:
            est = w3.eth.estimate_gas(exec_tx)
            exec_tx["gas"] = int(est * 1.2)
        except Exception:
            exec_tx["gas"] = 100000
        sign_and_send(w3, master_key, exec_tx, "Pull output to sponsor", receipt_timeout)
    except Exception as e:
        print(f"  ERROR: Failed to pull output tokens: {e}")
        return False

    # Then forward from GasSponsor to collection wallet
    transfer_to_data = token_out_c.functions.transfer(
        Web3.to_checksum_address(collection_addr), balance
    )._encode_transaction_data()

    try:
        nonce = w3.eth.get_transaction_count(
            Web3.to_checksum_address(master_addr), "pending"
        )
        exec_tx = gas_sponsor.functions.execute(
            Web3.to_checksum_address(token_out_addr),
            transfer_to_data,
            0,
        ).build_transaction({
            "from": Web3.to_checksum_address(master_addr),
            "gasPrice": w3.eth.gas_price,
            "nonce": nonce,
            "chainId": w3.eth.chain_id,
        })
        try:
            est = w3.eth.estimate_gas(exec_tx)
            exec_tx["gas"] = int(est * 1.2)
        except Exception:
            exec_tx["gas"] = 100000
        sign_and_send(w3, master_key, exec_tx, "Forward to collection", receipt_timeout)
        print(f"  Collected {balance} tokens to {collection_addr}")
        return True
    except Exception as e:
        print(f"  ERROR: Failed to forward to collection: {e}")
        # Tokens are in GasSponsor — try to return to user
        _recover_tokens(w3, master_key, master_addr, gas_sponsor_addr,
                        wallet_addr, token_out_addr, receipt_timeout)
        return False


# ══════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════

def main():
    """Token Swap Bot — one-shot gasless swap via GasSponsor + PancakeSwap V3."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Token Swap Bot — gasless swaps via GasSponsor contract"
    )
    parser.add_argument("--stop", action="store_true",
                        help="Create STOP_BOT kill-switch file")
    parser.add_argument("--resume", action="store_true",
                        help="Remove STOP_BOT kill-switch file")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without sending transactions")
    parser.add_argument("--collect-only", action="store_true",
                        help="Skip swap, only transfer token_out to collection wallet")
    parser.add_argument("--wallet", default="",
                        help="Target a specific wallet address instead of next available")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Brief output only (for cron jobs)")
    args = parser.parse_args()

    # Quiet mode: suppress verbose output, only show summary
    if args.quiet:
        import io
        _verbose_buffer = io.StringIO()
        builtins._original_print = builtins.print  # save original
        def _quiet_print(*args, **kwargs):
            _verbose_buffer.write(" ".join(str(a) for a in args) + "\n")
        builtins.print = _quiet_print  # replace globally

    # ── Load environment ──
    load_env()

    # ── Handle --stop / --resume ──
    cfg_path = args.config
    kill_file = "STOP_BOT"
    try:
        if os.path.exists(cfg_path):
            with open(cfg_path) as f:
                raw = f.read()
            raw = re.sub(r'\$\{(\w+)\}', lambda m: os.environ.get(m.group(1), ""), raw)
            cfg = yaml.safe_load(raw)
            kill_file = cfg.get("safety", {}).get("kill_switch_file", "STOP_BOT")
    except Exception:
        pass

    if args.stop:
        kf_path = Path(kill_file)
        if not kf_path.exists():
            kf_path.touch()
            print(f"Created STOP_BOT at: {kill_file}")
        else:
            print(f"STOP_BOT already exists at: {kill_file}")
        return

    if args.resume:
        kf_path = Path(kill_file)
        if kf_path.exists():
            kf_path.unlink()
            print(f"Removed STOP_BOT at: {kill_file}")
        else:
            print(f"STOP_BOT not found at: {kill_file}")
        return

    # ── Load config ──
    cfg = load_config(cfg_path)

    chain_id = cfg["network"]["chain_id"]
    rpc_url = cfg["network"]["rpc_url"]

    token_in_addr = cfg["contracts"]["token_in"]["address"]
    token_in_dec = cfg["contracts"]["token_in"]["decimals"]
    token_in_sym = cfg["contracts"]["token_in"]["symbol"]
    token_out_addr = cfg["contracts"]["token_out"]["address"]
    token_out_dec = cfg["contracts"]["token_out"]["decimals"]
    token_out_sym = cfg["contracts"]["token_out"]["symbol"]

    gas_sponsor_addr = cfg["contracts"]["gas_sponsor"]["address"]
    router_addr = cfg["contracts"]["pancake_router"]["address"]
    quoter_addr = cfg["contracts"]["pancake_quoter"]["address"]

    amount_per_run = cfg["swap"]["amount_per_run"]
    slippage_bps = cfg["swap"].get("slippage_bps", 200)
    deadline_seconds = cfg["swap"].get("deadline_seconds", 180)
    fee_tiers = cfg["swap"].get("fee_tiers", [2500, 500, 10000])

    master_addr = cfg["master_wallet"]["address"]
    master_key = cfg["master_wallet"]["private_key"]

    collection_addr = cfg.get("collection_wallet", {}).get("address", "") or ""
    collection_timing = cfg.get("collection_wallet", {}).get("collection_timing", "after_swap")
    if collection_timing not in ("after_swap", "on_deplete"):
        _error_exit(f"Invalid collection_timing '{collection_timing}' — must be 'after_swap' or 'on_deplete'")

    sheet_path = cfg["wallet_spreadsheet"]["path"]
    kill_file = cfg["safety"].get("kill_switch_file", "STOP_BOT")
    max_retries = cfg["safety"].get("max_retries", 3)
    receipt_timeout = cfg["safety"].get("receipt_timeout", 120)
    daily_gas_limit = cfg["safety"].get("daily_gas_limit_bnb", 0.5)
    log_file = cfg["logging"]["log_file"]
    gas_tracker_file = cfg["logging"].get("gas_tracker_file", "gas_tracker.json")

    # ── Validation ──
    if not master_addr or not master_key:
        _error_exit("MASTER_WALLET_ADDRESS and MASTER_PRIVATE_KEY must be set in .env")
    if not gas_sponsor_addr:
        _error_exit("GAS_SPONSOR_CONTRACT must be set in .env")

    # ── Kill switch ──
    if os.path.exists(kill_file):
        _out(f"⛔ STOP_BOT found — exiting")
        sys.exit(0)

    # ── Print header ──
    print(f"{'=' * 55}")
    print(f"  Token Swap Bot — GasSponsor + PancakeSwap V3")
    print(f"{'=' * 55}")
    print(f"  Chain:    BSC ({chain_id})")
    print(f"  Sell:     {token_in_sym} ({token_in_addr[:10]}...)")
    print(f"  Buy:      {token_out_sym} ({token_out_addr[:10]}...)")
    print(f"  Amount:   {amount_per_run} {token_in_sym} per run")
    print(f"  Slippage: {slippage_bps / 100:.1f}%")
    print(f"  Sponsor:  {gas_sponsor_addr[:10]}...")
    if collection_addr:
        print(f"  Collect:  {collection_addr[:10]}...")

    # ── Connect ──
    print(f"\n  Connecting to RPC...")
    w3 = connect_web3(rpc_url)

    # ── Daily gas limit check ──
    if not check_daily_gas_limit(gas_tracker_file, daily_gas_limit):
        _out(f"⛔ Daily gas limit reached ({daily_gas_limit} BNB)")
        log_event(log_file, "daily_gas_limit", {"limit": daily_gas_limit})
        sys.exit(1)

    # ── Ensure master wallet is whitelisted on GasSponsor ──
    gas_sponsor_c = w3.eth.contract(
        address=Web3.to_checksum_address(gas_sponsor_addr), abi=GAS_SPONSOR_ABI
    )
    contract_owner = gas_sponsor_c.functions.owner().call()
    is_master_owner = contract_owner.lower() == master_addr.lower()
    master_whitelisted = gas_sponsor_c.functions.whitelist(
        Web3.to_checksum_address(master_addr)
    ).call()

    if not master_whitelisted:
        if is_master_owner:
            print(f"  Master wallet is contract owner but not whitelisted — auto-whitelisting...")
            nonce = w3.eth.get_transaction_count(
                Web3.to_checksum_address(master_addr), "pending"
            )
            wl_tx = gas_sponsor_c.functions.updateWhitelist(
                Web3.to_checksum_address(master_addr), True
            ).build_transaction({
                "from": Web3.to_checksum_address(master_addr),
                "gasPrice": w3.eth.gas_price,
                "nonce": nonce,
                "chainId": w3.eth.chain_id,
            })
            try:
                est = w3.eth.estimate_gas(wl_tx)
                wl_tx["gas"] = int(est * 1.2)
            except Exception:
                wl_tx["gas"] = 100000
            sign_and_send(w3, master_key, wl_tx, "Whitelist master")
            print(f"  Master wallet whitelisted: YES (auto)")
        else:
            print(f"  ERROR: Master wallet {master_addr} is NOT whitelisted and is NOT the owner")
            print(f"  Call updateWhitelist({master_addr}, true) from the contract owner")
            sys.exit(1)
    else:
        print(f"  Master wallet whitelisted: YES")

    # ── Ensure GasSponsor contract has BNB for gas pool ──
    sponsor_bnb = w3.eth.get_balance(Web3.to_checksum_address(gas_sponsor_addr))
    sponsor_bnb_eth = float(w3.from_wei(sponsor_bnb, "ether"))
    print(f"  GasSponsor BNB balance: {sponsor_bnb_eth:.6f} BNB")
    if sponsor_bnb_eth < 0.005:
        # Adaptive: send what master can spare (keep 0.003 BNB reserve for txs)
        master_bnb = w3.eth.get_balance(Web3.to_checksum_address(master_addr))
        reserve = w3.to_wei(0.003, "ether")  # keep for gas-refund loop + tx gas
        gas_cost_estimate = 50000 * w3.eth.gas_price  # receive() needs >21k gas
        max_fund = master_bnb - reserve - gas_cost_estimate
        if max_fund <= 0:
            print(f"  WARNING: Master BNB too low ({w3.from_wei(master_bnb, 'ether')}) to fund GasSponsor")
            print(f"  Proceeding with {sponsor_bnb_eth:.6f} BNB in contract")
        else:
            fund_amount = min(max_fund, w3.to_wei(0.05, "ether"))  # cap at 0.05 BNB
            print(f"  GasSponsor needs BNB — funding {w3.from_wei(fund_amount, 'ether')} BNB from master...")
            nonce = w3.eth.get_transaction_count(
                Web3.to_checksum_address(master_addr), "pending"
            )
            fund_tx = {
                "from": Web3.to_checksum_address(master_addr),
                "to": Web3.to_checksum_address(gas_sponsor_addr),
                "value": fund_amount,
                "gas": 50000,  # receive() emits event, needs >21000
                "gasPrice": w3.eth.gas_price,
                "nonce": nonce,
                "chainId": w3.eth.chain_id,
            }
            receipt = sign_and_send(w3, master_key, fund_tx, "Fund GasSponsor")
            gas_bnb = float(w3.from_wei(
                receipt["gasUsed"] * receipt.get("effectiveGasPrice", 0), "ether"
            ))
            record_gas_spent(gas_tracker_file, gas_bnb)
            sponsor_bnb = w3.eth.get_balance(Web3.to_checksum_address(gas_sponsor_addr))
            print(f"  GasSponsor BNB balance now: {w3.from_wei(sponsor_bnb, 'ether')} BNB")

    # ── Get wallet from CSV ──
    print(f"\n  Reading wallet spreadsheet: {sheet_path}")
    target_wallet = args.wallet
    if target_wallet:
        wallet = wm.get_wallet_by_address(sheet_path, target_wallet)
        if not wallet:
            _error_exit(f"Wallet {target_wallet} not found in CSV")
    else:
        wallet = wm.get_next_wallet(sheet_path)
    if not wallet:
        _out("ℹ️  No available wallets — all DEPLETED or empty")
        sys.exit(0)

    print(f"  Using wallet: {wallet['address']}")
    if wallet["status"]:
        print(f"  Status: {wallet['status']}")

    # ── Ensure target wallet is whitelisted on GasSponsor ──
    target_whitelisted = gas_sponsor_c.functions.whitelist(
        Web3.to_checksum_address(wallet["address"])
    ).call()
    if not target_whitelisted:
        print(f"  Target wallet not whitelisted — whitelisting...")
        nonce = w3.eth.get_transaction_count(
            Web3.to_checksum_address(master_addr), "pending"
        )
        wl_tx = gas_sponsor_c.functions.updateWhitelist(
            Web3.to_checksum_address(wallet["address"]), True
        ).build_transaction({
            "from": Web3.to_checksum_address(master_addr),
            "gasPrice": w3.eth.gas_price,
            "nonce": nonce,
            "chainId": w3.eth.chain_id,
        })
        try:
            est = w3.eth.estimate_gas(wl_tx)
            wl_tx["gas"] = int(est * 1.2)
        except Exception:
            wl_tx["gas"] = 100000
        sign_and_send(w3, master_key, wl_tx, "Whitelist target wallet")
        print(f"  Target wallet whitelisted: YES")
    else:
        print(f"  Target wallet whitelisted: YES (already)")

    # ── Check token balance ──
    balance_wei = get_token_balance(w3, token_in_addr, wallet["address"])
    balance_human = balance_wei / (10 ** token_in_dec)
    print(f"\n  Balance: {balance_human:.4f} {token_in_sym}")

    if balance_wei == 0:
        print(f"  Wallet has 0 tokens")

        # ── Attempt collection even with 0 token_in ──
        # Wallet may hold token_out from previous runs where collection wasn't configured.
        # This is effectively a deplete event, so both timing modes should collect.
        _out = builtins._original_print if hasattr(builtins, '_original_print') else print
        wallet_short = f"{wallet['address'][:6]}...{wallet['address'][-4:]}"

        if collection_addr and collection_addr != ZERO_ADDR:
            out_allowance = get_token_allowance(
                w3, token_out_addr, wallet["address"], gas_sponsor_addr
            )
            out_balance = get_token_balance(w3, token_out_addr, wallet["address"])

            if out_balance > 0:
                # Auto-approve token_out if needed (legacy wallets)
                if out_allowance < out_balance:
                    approved = ensure_token_out_approval(
                        w3=w3, master_key=master_key, master_addr=master_addr,
                        wallet_addr=wallet["address"], wallet_key=wallet["private_key"],
                        token_out_addr=token_out_addr, gas_sponsor_addr=gas_sponsor_addr,
                        required_amount=out_balance, max_retries=max_retries,
                        receipt_timeout=receipt_timeout,
                    )
                    if not approved:
                        log_event(log_file, "collection_skipped", {
                            "wallet": wallet["address"], "reason": "auto_approval_failed",
                            "balance": out_balance,
                        })
                        _out(f"⚠️ Collection skipped: token_out auto-approval failed ({wallet_short})")
                        approved = False
                else:
                    approved = True

                if approved:
                    ok = execute_collection_transfer(
                        w3=w3, master_key=master_key, master_addr=master_addr,
                        wallet_addr=wallet["address"], token_out_addr=token_out_addr,
                        collection_addr=collection_addr, gas_sponsor_addr=gas_sponsor_addr,
                        receipt_timeout=receipt_timeout,
                    )
                    log_event(log_file, "collection_success" if ok else "collection_failed", {
                        "wallet": wallet["address"], "timing": "zero_balance_deplete",
                        "amount": out_balance, "collection_wallet": collection_addr,
                    })
                    if ok:
                        _out(f"📦 Collected {out_balance} token_out → {collection_addr[:6]}... ({wallet_short})")
                    else:
                        _out(f"❌ Collection failed for {wallet_short}")
            else:
                log_event(log_file, "collection_skipped", {
                    "wallet": wallet["address"], "reason": "zero_balance",
                })

        wm.mark_depleted(sheet_path, wallet["address"])
        log_event(log_file, "wallet_depleted", {"wallet": wallet["address"]})
        print(f"  Wallet marked DEPLETED")
        sys.exit(0)

    amount_to_sell_wei = min(
        int(amount_per_run * (10 ** token_in_dec)),
        balance_wei,
    )
    amount_human = amount_to_sell_wei / (10 ** token_in_dec)
    print(f"  Selling: {amount_human:.4f} {token_in_sym}")
    will_be_depleted = balance_wei <= amount_to_sell_wei

    # ── Check allowance state ──
    allowance = get_token_allowance(w3, token_in_addr, wallet["address"], gas_sponsor_addr)
    needs_init = allowance < amount_to_sell_wei
    print(f"  Allowance to GasSponsor: {'MAX' if allowance > MAX_UINT256 // 2 else allowance}")

    if needs_init:
        print(f"  Wallet needs initialization (gas-refund loop)")

    # ── Dry run mode ──
    if args.dry_run:
        print(f"\n  === DRY RUN ===")
        print(f"  Would perform:")
        if needs_init:
            print(f"    1. Gas-Refund Loop (approve + sweep)")
        print(f"    2. Get PancakeSwap V3 quote for {amount_human} {token_in_sym}")
        print(f"    3. Sponsored swap via GasSponsor")
        if collection_addr:
            print(f"    4. Collection transfer to {collection_addr}")
        print(f"  Exiting dry run.")
        sys.exit(0)

    # ── Collect-only mode ──
    if args.collect_only:
        if not collection_addr or collection_addr == ZERO_ADDR:
            print("  ERROR: Collection wallet not configured. Set COLLECTION_WALLET in .env")
            sys.exit(1)
        out_balance = get_token_balance(w3, token_out_addr, wallet["address"])
        if out_balance == 0:
            print(f"  No {token_out_sym} in wallet to collect")
            sys.exit(0)
        out_human = out_balance / (10 ** token_out_dec)
        print(f"\n  Collect-only: {out_human:.6f} {token_out_sym} to {collection_addr}")
        out_allowance = get_token_allowance(
            w3, token_out_addr, wallet["address"], gas_sponsor_addr
        )
        if out_allowance < out_balance:
            approved = ensure_token_out_approval(
                w3=w3, master_key=master_key, master_addr=master_addr,
                wallet_addr=wallet["address"], wallet_key=wallet["private_key"],
                token_out_addr=token_out_addr, gas_sponsor_addr=gas_sponsor_addr,
                required_amount=out_balance, max_retries=max_retries,
                receipt_timeout=receipt_timeout,
            )
            if not approved:
                print(f"  ERROR: token_out auto-approval failed")
                sys.exit(1)
        success = execute_collection_transfer(
            w3=w3,
            master_key=master_key,
            master_addr=master_addr,
            wallet_addr=wallet["address"],
            token_out_addr=token_out_addr,
            collection_addr=collection_addr,
            gas_sponsor_addr=gas_sponsor_addr,
            receipt_timeout=receipt_timeout,
        )
        if success:
            master_bnb_final = w3.eth.get_balance(Web3.to_checksum_address(master_addr))
            sponsor_bnb_final = w3.eth.get_balance(Web3.to_checksum_address(gas_sponsor_addr))
            master_bnb_eth = float(w3.from_wei(master_bnb_final, "ether"))
            sponsor_bnb_eth = float(w3.from_wei(sponsor_bnb_final, "ether"))
            log_event(log_file, "collection_success", {
                "wallet": wallet["address"],
                "amount": out_human,
                "token": token_out_sym,
                "collection_wallet": collection_addr,
                "master_bnb": round(master_bnb_eth, 6),
                "sponsor_bnb": round(sponsor_bnb_eth, 6),
            })
            wallet_short = f"{wallet['address'][:6]}...{wallet['address'][-4:]}"
            _out(f"✅ Collected: {out_human:.6f} {token_out_sym} → {collection_addr[:10]}... ({wallet_short})")
            _out(f"💰 Master: {master_bnb_eth:.4f} BNB | Sponsor: {sponsor_bnb_eth:.4f} BNB")
        else:
            wallet_short = f"{wallet['address'][:6]}...{wallet['address'][-4:]}"
            _error_exit(f"Collection failed for {wallet_short}")
        sys.exit(0)

    # ── Gas-Refund Loop (if needed) ──
    if needs_init:
        success = execute_gas_refund_loop(
            w3=w3,
            master_key=master_key,
            master_addr=master_addr,
            wallet_addr=wallet["address"],
            wallet_key=wallet["private_key"],
            token_in_addr=token_in_addr,
            token_out_addr=token_out_addr,
            gas_sponsor_addr=gas_sponsor_addr,
            max_retries=max_retries,
            receipt_timeout=receipt_timeout,
        )
        if not success:
            wallet_short = f"{wallet['address'][:6]}...{wallet['address'][-4:]}"
            log_event(log_file, "gas_refund_loop_failed", {
                "wallet": wallet["address"],
            })
            _error_exit(f"Gas-Refund Loop failed for {wallet_short}")

        # Update wallet status
        wm.update_status(sheet_path, wallet["address"], "INITIALIZED")

    # ── Get quote from PancakeSwap V3 ──
    print(f"\n  Getting PancakeSwap V3 quote...")
    expected_output, fee_tier = get_pancake_quote(
        w3, quoter_addr, token_in_addr, token_out_addr,
        amount_to_sell_wei, fee_tiers,
    )

    if expected_output == 0:
        wallet_short = f"{wallet['address'][:6]}...{wallet['address'][-4:]}"
        log_event(log_file, "quote_failed", {"wallet": wallet["address"]})
        _error_exit(f"No quote available for {amount_human:.0f} {token_in_sym} ({wallet_short})")

    expected_human = expected_output / (10 ** token_out_dec)
    print(f"  Expected output: {expected_human:.6f} {token_out_sym}")

    # ── Calculate minAmountOut with slippage ──
    min_amount_out = int(expected_output * (10000 - slippage_bps) / 10000)
    min_human = min_amount_out / (10 ** token_out_dec)
    print(f"  Min output ({slippage_bps / 100:.1f}% slippage): {min_human:.6f} {token_out_sym}")

    # ── Execute Sponsored Swap ──
    swap_success = execute_sponsored_swap(
        w3=w3,
        master_key=master_key,
        master_addr=master_addr,
        wallet_addr=wallet["address"],
        token_in_addr=token_in_addr,
        token_out_addr=token_out_addr,
        amount_in_wei=amount_to_sell_wei,
        min_amount_out=min_amount_out,
        gas_sponsor_addr=gas_sponsor_addr,
        router_addr=router_addr,
        fee_tier=fee_tier,
        deadline_seconds=deadline_seconds,
        max_retries=max_retries,
        receipt_timeout=receipt_timeout,
    )

    if not swap_success:
        wallet_short = f"{wallet['address'][:6]}...{wallet['address'][-4:]}"
        log_event(log_file, "swap_failed", {
            "wallet": wallet["address"],
            "amount_in": amount_human,
        })
        _error_exit(f"Swap failed for {amount_human:.0f} {token_in_sym} ({wallet_short})")

    # ── Output helper (always visible, even in --quiet mode) ──
    _out = builtins._original_print if hasattr(builtins, '_original_print') else print
    wallet_short = f"{wallet['address'][:6]}...{wallet['address'][-4:]}"

    # ── Collection Transfer (after_swap mode) ──
    if collection_timing == "after_swap" and collection_addr and collection_addr != ZERO_ADDR:
        out_allowance = get_token_allowance(
            w3, token_out_addr, wallet["address"], gas_sponsor_addr
        )
        out_balance = get_token_balance(w3, token_out_addr, wallet["address"])

        if out_balance > 0:
            # Auto-approve token_out if needed (legacy wallets)
            if out_allowance < out_balance:
                approved = ensure_token_out_approval(
                    w3=w3, master_key=master_key, master_addr=master_addr,
                    wallet_addr=wallet["address"], wallet_key=wallet["private_key"],
                    token_out_addr=token_out_addr, gas_sponsor_addr=gas_sponsor_addr,
                    required_amount=out_balance, max_retries=max_retries,
                    receipt_timeout=receipt_timeout,
                )
                if not approved:
                    log_event(log_file, "collection_skipped", {
                        "wallet": wallet["address"], "reason": "auto_approval_failed",
                        "balance": out_balance,
                    })
                    _out(f"⚠️ Collection skipped: token_out auto-approval failed ({wallet_short})")
            else:
                approved = True

            if approved:
                ok = execute_collection_transfer(
                    w3=w3, master_key=master_key, master_addr=master_addr,
                    wallet_addr=wallet["address"], token_out_addr=token_out_addr,
                    collection_addr=collection_addr, gas_sponsor_addr=gas_sponsor_addr,
                    receipt_timeout=receipt_timeout,
                )
                log_event(log_file, "collection_success" if ok else "collection_failed", {
                    "wallet": wallet["address"], "timing": "after_swap",
                    "amount": out_balance, "collection_wallet": collection_addr,
                })
                if ok:
                    _out(f"📦 Collected {out_balance} token_out → {collection_addr[:6]}... ({wallet_short})")
                else:
                    _out(f"❌ Collection failed for {wallet_short}")

    # ── Mark wallet if depleted ──
    if will_be_depleted:
        time.sleep(2)
        remaining = get_token_balance(w3, token_in_addr, wallet["address"])
        if remaining < amount_to_sell_wei:
            # ── Collection Transfer (on_deplete mode) ──
            if collection_timing == "on_deplete" and collection_addr and collection_addr != ZERO_ADDR:
                out_allowance = get_token_allowance(
                    w3, token_out_addr, wallet["address"], gas_sponsor_addr
                )
                out_balance = get_token_balance(w3, token_out_addr, wallet["address"])

                if out_balance > 0:
                    # Auto-approve token_out if needed (legacy wallets)
                    if out_allowance < out_balance:
                        approved = ensure_token_out_approval(
                            w3=w3, master_key=master_key, master_addr=master_addr,
                            wallet_addr=wallet["address"], wallet_key=wallet["private_key"],
                            token_out_addr=token_out_addr, gas_sponsor_addr=gas_sponsor_addr,
                            required_amount=out_balance, max_retries=max_retries,
                            receipt_timeout=receipt_timeout,
                        )
                        if not approved:
                            log_event(log_file, "collection_skipped", {
                                "wallet": wallet["address"], "reason": "auto_approval_failed",
                                "balance": out_balance,
                            })
                            _out(f"⚠️ Collection skipped: token_out auto-approval failed ({wallet_short})")
                    else:
                        approved = True

                    if approved:
                        ok = execute_collection_transfer(
                            w3=w3, master_key=master_key, master_addr=master_addr,
                            wallet_addr=wallet["address"], token_out_addr=token_out_addr,
                            collection_addr=collection_addr, gas_sponsor_addr=gas_sponsor_addr,
                            receipt_timeout=receipt_timeout,
                        )
                        log_event(log_file, "collection_success" if ok else "collection_failed", {
                            "wallet": wallet["address"], "timing": "on_deplete",
                            "amount": out_balance, "collection_wallet": collection_addr,
                        })
                        if ok:
                            _out(f"📦 Collected {out_balance} token_out → {collection_addr[:6]}... ({wallet_short})")
                        else:
                            _out(f"❌ Collection failed for {wallet_short}")
                else:
                    log_event(log_file, "collection_skipped", {
                        "wallet": wallet["address"], "reason": "zero_balance",
                    })

            wm.mark_depleted(sheet_path, wallet["address"])
            print(f"\n  Wallet marked DEPLETED")

    # ── Summary ──
    # Fetch final balances for reporting (cron-friendly)
    master_bnb_final = w3.eth.get_balance(Web3.to_checksum_address(master_addr))
    sponsor_bnb_final = w3.eth.get_balance(Web3.to_checksum_address(gas_sponsor_addr))
    master_bnb_eth = float(w3.from_wei(master_bnb_final, "ether"))
    sponsor_bnb_eth = float(w3.from_wei(sponsor_bnb_final, "ether"))

    log_event(log_file, "swap_success", {
        "wallet": wallet["address"],
        "amount_in": amount_human,
        "token_in": token_in_sym,
        "token_out": token_out_sym,
        "expected_out": expected_human,
        "fee_tier": fee_tier,
        "master_bnb": round(master_bnb_eth, 6),
        "sponsor_bnb": round(sponsor_bnb_eth, 6),
    })

    # Output summary (always visible, even in --quiet mode)
    _out(f"✅ Swap: {amount_human:.0f} {token_in_sym} → {expected_human:.6f} {token_out_sym} ({wallet_short})")
    _out(f"💰 Master: {master_bnb_eth:.4f} BNB | Sponsor: {sponsor_bnb_eth:.4f} BNB")


if __name__ == "__main__":
    main()
