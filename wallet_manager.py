#!/usr/bin/env python3
"""Wallet CSV manager for Token Swap Bot.

Reads/writes wallets.csv in the project folder.
Supports deriving private keys from 12-word secret phrases (BIP-44 m/44'/60'/0'/0/0).
"""
import csv, os, sys
from pathlib import Path

COLS = ["Address", "Private Key", "Secret Phrase", "Status"]
CSV_FILE = "wallets.csv"

# BIP-44 derivation path for BSC / Ethereum
DERIVATION_PATH = "m/44'/60'/0'/0/0"


def create_csv(path: str):
    """Create a new wallet CSV with headers."""
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(COLS)
    print(f"Created {path}")


def derive_private_key_from_mnemonic(mnemonic: str) -> tuple:
    """Derive private key + address from a 12/24-word mnemonic.

    Uses BIP-44 path m/44'/60'/0'/0/0 (standard for BSC/ETH).
    Returns (address, private_key_hex) or raises on failure.
    """
    from eth_account import Account

    Account.enable_unaudited_hdwallet_features()
    acct = Account.from_mnemonic(mnemonic.strip(), account_path=DERIVATION_PATH)
    pk_hex = "0x" + acct.key.hex()
    return acct.address, pk_hex


def derive_key_if_needed(row: dict, csv_path: str) -> dict:
    """If row has Secret Phrase but no Private Key, derive and cache it.

    Returns updated row dict. Also writes back to the CSV.
    """
    pk = row.get("Private Key", "").strip()
    phrase = row.get("Secret Phrase", "").strip()

    if pk:
        return row  # already have a key

    if not phrase:
        return row  # nothing to derive from

    print(f"  Deriving private key from mnemonic for {row['Address'][:10]}...")
    try:
        derived_addr, derived_pk = derive_private_key_from_mnemonic(phrase)
    except Exception as e:
        print(f"  WARNING: Failed to derive key: {e}")
        return row

    # Verify address matches
    if derived_addr.lower() != row["Address"].strip().lower():
        print(f"  WARNING: Derived address {derived_addr} != CSV address {row['Address']}")
        print(f"           Key NOT saved. Check your mnemonic and address.")
        return row

    # Cache the derived key in the row
    row["Private Key"] = derived_pk
    print(f"  Derived key cached for {derived_addr}")

    # Write back to CSV
    _write_key_to_csv(csv_path, row["Address"], derived_pk)

    return row


def _write_key_to_csv(path: str, address: str, private_key: str):
    """Update the Private Key column for a given address in the CSV."""
    if not os.path.exists(path):
        return

    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            if row["Address"].strip().lower() == address.strip().lower():
                row["Private Key"] = private_key
            rows.append(row)

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def add_wallet(path: str, address: str, private_key: str, secret_phrase: str = ""):
    """Add a wallet to the CSV."""
    if not os.path.exists(path):
        create_csv(path)

    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([address, private_key, secret_phrase, ""])
    print(f"Added wallet: {address}")


def list_wallets(path: str) -> list:
    """List all wallets in the CSV."""
    if not os.path.exists(path):
        print(f"No CSV at {path}")
        return []

    wallets = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            addr = row.get("Address", "").strip()
            status = row.get("Status", "").strip()
            if addr:
                wallets.append({"address": addr, "status": status})
    return wallets


def update_status(path: str, address: str, status: str):
    """Update status for a wallet address."""
    if not os.path.exists(path):
        return

    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            if row["Address"].strip().lower() == address.lower():
                row["Status"] = status
                print(f"  {address} -> status={status}")
            rows.append(row)

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def get_next_wallet(path: str) -> dict:
    """Get first wallet with status != DEPLETED.

    If the wallet has a Secret Phrase but no Private Key, derives the key
    from the mnemonic and caches it.
    """
    if not os.path.exists(path):
        print(f"No CSV at {path}")
        return None

    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            addr = row.get("Address", "").strip()
            pk = row.get("Private Key", "").strip()
            phrase = row.get("Secret Phrase", "").strip()
            status = row.get("Status", "").strip()

            if not addr or status == "DEPLETED":
                continue

            # Derive key from mnemonic if missing
            if not pk and phrase:
                row = derive_key_if_needed(row, path)
                pk = row.get("Private Key", "").strip()

            if addr and pk:
                return {
                    "address": addr,
                    "private_key": pk,
                    "secret_phrase": phrase if phrase else "",
                    "status": status,
                }

    print("No available wallets (all DEPLETED, missing keys, or empty CSV)")
    return None


def get_wallet_by_address(path: str, address: str) -> dict:
    """Get a specific wallet by address."""
    if not os.path.exists(path):
        return None

    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            addr = row.get("Address", "").strip()
            if addr.lower() == address.lower():
                pk = row.get("Private Key", "").strip()
                phrase = row.get("Secret Phrase", "").strip()
                status = row.get("Status", "").strip()

                if not pk and phrase:
                    row = derive_key_if_needed(row, path)
                    pk = row.get("Private Key", "").strip()

                return {
                    "address": addr,
                    "private_key": pk,
                    "secret_phrase": phrase if phrase else "",
                    "status": status,
                }
    return None


def mark_depleted(path: str, address: str):
    """Shortcut to update_status to DEPLETED."""
    update_status(path, address, "DEPLETED")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Wallet CSV manager")
    parser.add_argument("--path", default=CSV_FILE, help="Path to CSV")
    parser.add_argument("--create", action="store_true", help="Create new CSV")
    parser.add_argument("--add", help="Add wallet address", metavar="ADDRESS")
    parser.add_argument("--private-key", help="Private key for --add")
    parser.add_argument("--secret-phrase", help="Secret phrase for --add")
    parser.add_argument("--status", help="Update wallet status")
    parser.add_argument("--list", action="store_true", help="List wallets")
    parser.add_argument("--next", action="store_true", help="Show next available wallet")
    args = parser.parse_args()

    if args.create:
        create_csv(args.path)
    elif args.add:
        if not args.private_key and not args.secret_phrase:
            print("--private-key or --secret-phrase required with --add")
            sys.exit(1)
        pk = args.private_key or ""
        # If only mnemonic given, derive the key
        if not pk and args.secret_phrase:
            try:
                addr, pk = derive_private_key_from_mnemonic(args.secret_phrase)
                print(f"  Derived address: {addr}")
            except Exception as e:
                print(f"  Failed to derive key: {e}")
                sys.exit(1)
        add_wallet(args.path, args.add, pk, args.secret_phrase or "")
    elif args.status:
        if not args.add:
            print("--add required with --status")
            sys.exit(1)
        update_status(args.path, args.add, args.status)
    elif args.list:
        wallets = list_wallets(args.path)
        print(f"{'Address':<48} {'Status':<15}")
        print("-" * 63)
        for w in wallets:
            print(f"{w['address']:<48} {w['status']:<15}")
    elif args.next:
        w = get_next_wallet(args.path)
        if w:
            print(f"{w['address']} [{w['status']}]")
        else:
            print("No available wallets")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
