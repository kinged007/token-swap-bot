#!/usr/bin/env python3
"""Check all wallet balances, BNB, allowance, and token balances."""
import csv, json, os, re, sys
from pathlib import Path
import yaml
from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

ERC20_ABI = json.loads("""[
  {"inputs":[{"name":"owner","type":"address"}],"name":"balanceOf",
   "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
  {"inputs":[],"name":"decimals",
   "outputs":[{"name":"","type":"uint8"}],"stateMutability":"view","type":"function"},
  {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],
   "name":"allowance",
   "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}
]""")

MAX_UINT256 = 2**256 - 1


def load_config(path="config.yaml"):
    load_dotenv(".env")
    with open(path) as f:
        raw = f.read()
    raw = re.sub(r'\$\{(\w+)\}', lambda m: os.environ.get(m.group(1), ""), raw)
    return yaml.safe_load(raw)


def main():
    cfg = load_config()
    rpc = cfg["network"]["rpc_url"]
    token_in = cfg["contracts"]["token_in"]
    token_out = cfg["contracts"]["token_out"]
    gas_sponsor_addr = cfg["contracts"]["gas_sponsor"]["address"]
    sheet_path = cfg.get("wallet_spreadsheet", {}).get("path", "wallets.csv")

    # Connect
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if not w3.is_connected():
        print(f"Cannot connect to {rpc}")
        sys.exit(1)

    print(f"Connected (chain {w3.eth.chain_id}, block {w3.eth.block_number})")
    print()

    # Read CSV
    if not os.path.exists(sheet_path):
        print(f"No CSV at {sheet_path}")
        sys.exit(0)

    with open(sheet_path, newline="") as f:
        reader = csv.DictReader(f)
        wallets = list(reader)

    if not wallets:
        print("CSV is empty (just headers)")
        return

    in_dec = token_in["decimals"]
    out_dec = token_out["decimals"]
    in_sym = token_in["symbol"]

    hdr = (
        f"{'Wallet':<44} {'Status':<12} {'BNB':<12} "
        f"{in_sym:<16} {'USDT':<16} {'Allowance':<10}"
    )
    print(hdr)
    print("-" * len(hdr))

    total_tokens = 0
    total_usdt = 0
    total_bnb = 0
    count = 0

    for row in wallets:
        addr = row.get("Address", "").strip()
        status = row.get("Status", "").strip()

        if not addr:
            continue

        count += 1
        try:
            addr_cs = Web3.to_checksum_address(addr)
        except Exception:
            print(f"{addr:<44} {'INVALID':<12}")
            continue

        # BNB balance
        try:
            bnb_bal = float(w3.from_wei(w3.eth.get_balance(addr_cs), "ether"))
        except Exception:
            bnb_bal = -1

        # Token in balance
        try:
            c_in = w3.eth.contract(
                address=Web3.to_checksum_address(token_in["address"]), abi=ERC20_ABI
            )
            bal_in = c_in.functions.balanceOf(addr_cs).call()
            bal_in_human = bal_in / (10 ** in_dec)
        except Exception:
            bal_in_human = -1

        # Token out balance
        try:
            c_out = w3.eth.contract(
                address=Web3.to_checksum_address(token_out["address"]), abi=ERC20_ABI
            )
            bal_out = c_out.functions.balanceOf(addr_cs).call()
            bal_out_human = bal_out / (10 ** out_dec)
        except Exception:
            bal_out_human = -1

        # Allowance to GasSponsor
        try:
            allowance = c_in.functions.allowance(
                addr_cs, Web3.to_checksum_address(gas_sponsor_addr)
            ).call()
            if allowance > MAX_UINT256 // 2:
                allow_str = "MAX"
            elif allowance > 0:
                allow_str = f"{allowance / (10 ** in_dec):.0f}"
            else:
                allow_str = "0"
        except Exception:
            allow_str = "N/A"

        bnb_str = f"{bnb_bal:.4f}" if bnb_bal >= 0 else "N/A"
        tok_str = f"{bal_in_human:.2f}" if bal_in_human >= 0 else "N/A"
        usdt_str = f"{bal_out_human:.2f}" if bal_out_human >= 0 else "N/A"

        print(
            f"{addr:<44} {status:<12} {bnb_str:<12} "
            f"{tok_str:<16} {usdt_str:<16} {allow_str:<10}"
        )

        if bal_in_human > 0:
            total_tokens += bal_in_human
        if bal_out_human > 0:
            total_usdt += bal_out_human
        if bnb_bal > 0:
            total_bnb += bnb_bal

    if count > 0:
        print("-" * len(hdr))
        print(
            f"{'TOTAL':<44} {'':<12} {total_bnb:.4f}{'':<6} "
            f"{total_tokens:.2f}{'':<10} {total_usdt:.2f}"
        )
    else:
        print("(no wallets found)")


if __name__ == "__main__":
    main()
