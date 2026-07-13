# Token Swap Bot

Token → USDT swaps on BNB Smart Chain via **PancakeSwap V3**.
Supports two execution modes:
- **Sponsored mode** (default): Gasless swaps via a custom **GasSponsor** smart contract — wallets never need BNB.
- **Direct mode**: Wallets pay their own gas — activated when `GAS_SPONSOR_CONTRACT` is not set in `.env`.

Designed for multi-wallet, cron-driven execution — one swap per run, then exits.

GasSponsor Contract: https://github.com/kinged007/gas-sponsor-contract

## How It Works

### Sponsored Mode (GAS_SPONSOR_CONTRACT is set)

1. Reads wallet addresses from `wallets.csv`, picks the first non-DEPLETED wallet
2. **Auto-whitelists** the target wallet on the GasSponsor contract (if needed)
3. Runs a **Gas-Refund Loop** (one-time per wallet):
   - Sends a small amount of BNB from master to target wallet
   - Target wallet approves GasSponsor for unlimited **token_in** AND **token_out** spending
   - Sweeps residual BNB back to master
4. Gets a **PancakeSwap V3** quote for the swap (tries fee tiers: 100, 500, 2500, 10000)
5. Executes a **sponsored swap** via GasSponsor (all gas paid by the contract):
   - **Step A**: Pull tokens from user wallet to GasSponsor (`transferFrom`)
   - **Step B1**: Approve PancakeSwap router to spend tokens held by GasSponsor
   - **Step B2**: Execute the swap via PancakeSwap V3 (`exactInputSingle`)
   - **Step C**: Forward output USDT from GasSponsor to user wallet
6. If a collection wallet is configured and `collection_timing: after_swap`, forwards USDT there
7. If the wallet has 0 token_in, attempts collection of any remaining token_out, then marks it DEPLETED
8. If the wallet balance drops below the swap amount, marks it DEPLETED
   - If `collection_timing: on_deplete`, forwards all accumulated USDT to collection wallet just before marking DEPLETED
9. Exits cleanly — ready for the next cron tick

### Direct Mode (GAS_SPONSOR_CONTRACT is empty or not set)

1. Reads wallet addresses from `wallets.csv`, picks the first non-DEPLETED wallet
2. **Funds the wallet** with BNB from master (configurable `direct_swap_bnb`, default 0.002 BNB)
   - Only sends BNB if wallet balance is too low to cover swap gas
   - Checked on every run — tops up if needed
3. Skips GasSponsor authorization entirely (no contract to approve)
4. Gets a **PancakeSwap V3** quote for the swap
5. Wallet **approves the PancakeSwap router** directly (if allowance insufficient)
6. Wallet **calls `exactInputSingle` directly** — output tokens land in the wallet itself
7. If a collection wallet is configured, wallet **transfers token_out directly** to collection address
8. When wallet is depleted, **sweeps remaining BNB** to collection address (or master if no collection wallet)
9. Marks wallet DEPLETED and exits

### Wallet Lifecycle

**Sponsored mode:**
- Wallets with any status **except** `DEPLETED` are eligible
- First run triggers the **Gas-Refund Loop** (approve token_in + token_out, sweep BNB), status set to `INITIALIZED`
- Legacy wallets missing token_out approval are **auto-approved** on first collection attempt (funds wallet, approves, then collects)
- If a wallet has **fewer tokens** than `amount_per_run`, it sells what's left
- After selling, if the wallet is emptied → status set to `DEPLETED`
- DEPLETED wallets are **never touched again** (kept in CSV for records)
- Whitelisted wallets remain whitelisted permanently for future transactions

**Direct mode:**
- First run sends BNB from master to wallet (if needed)
- Wallet pays its own gas for all transactions (approve, swap, collect)
- On deplete, remaining BNB is swept to collection address (or master)
- Otherwise identical: sells what's left, marks DEPLETED when empty

## Project Structure

```
token-swap-bot/
├── config.yaml         # All settings (tokens, amounts, contracts, network)
├── wallets.csv         # Wallet ledger — Address, Private Key, Secret Phrase, Status
├── swap.py             # Main swap script (one-shot, cron-ready)
├── wallet_manager.py   # CLI tool to manage wallets.csv
├── check_balances.py   # Scan all wallets, show BNB + token + USDT balances + allowance
├── .env                # Secrets (master wallet, contract addresses, API keys)
├── .env.example        # Template for .env
├── swap_log.jsonl      # JSONL audit log of all swap events
├── gas_tracker.json    # Daily BNB gas spending tracker
├── .gitignore          # Excludes secrets/, .env, *.log
└── .venv/              # Virtual environment
```

## Setup

### 1. Prerequisites

- Python 3.11+
- A funded **master wallet** with BNB for gas
- **Sponsored mode**: The **GasSponsor** smart contract deployed on BSC mainnet
- **Direct mode**: No contract needed — just a master wallet with BNB to fund target wallets

### 2. Install

```bash
cd ~/Sync/token-swap-bot
python3 -m venv .venv
source .venv/bin/activate
pip install web3 eth-account python-dotenv pyyaml
```

### 3. Configure environment

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

Required variables in `.env`:

```env
# Network
BSC_RPC_URL=https://bsc-dataseed.binance.org

# Master Wallet (pays for all gas orchestration)
MASTER_WALLET_ADDRESS=0xYourMasterAddress
MASTER_PRIVATE_KEY=0xYourMasterPrivateKey

# Deployed Contracts
GAS_SPONSOR_CONTRACT=0xYourGasSponsorContractAddress   # Leave empty for Direct Mode
PANCAKE_ROUTER=0x1b81D678ffb9C0263b24A97847620C99d213eB14
PANCAKE_QUOTER=0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997

# Collection Wallet (optional — receives swapped USDT)
COLLECTION_WALLET=
# Timing: "after_swap" (every swap) or "on_deplete" (once, when wallet runs out)
# Configured in config.yaml under collection_wallet.collection_timing

# Safety
MAX_SLIPPAGE_BPS=200
DAILY_GAS_LIMIT_BNB=0.5
```

> **Mode selection**: The bot automatically runs in **Sponsored mode** when `GAS_SPONSOR_CONTRACT` is set, or **Direct mode** when it is empty or omitted.

### 4. Configure swap parameters

Edit `config.yaml`:

```yaml
contracts:
  token_in:
    address: "0xTokenContractAddressToSwapFrom"
    symbol: "TOKEN"
    decimals: 18
  token_out:
    address: "0x55d398326f99059ff775485246999027b3197955"
    symbol: "USDT"
    decimals: 18

swap:
  amount_per_run: 100
  slippage_bps: 200          # 2% max slippage
  deadline_seconds: 180      # swap must complete within 3 minutes
  fee_tiers: [100, 500, 2500, 10000]  # PancakeSwap V3 fee tiers to try
  direct_swap_bnb: 0.002     # BNB sent to wallet for gas in Direct Mode

collection_wallet:
  address: "${COLLECTION_WALLET}"
  collection_timing: after_swap  # "after_swap" = every swap | "on_deplete" = once when wallet runs out
```

### 5. Add wallets

```bash
# Add with private key
python3 wallet_manager.py --add 0xYOURWALLET --private-key 0xYOURKEY

# Add with mnemonic (auto-derives private key using BIP-44 path m/44'/60'/0'/0/0)
python3 wallet_manager.py --add 0xYOURWALLET --secret-phrase "your twelve word seed phrase here"
```

Or edit `wallets.csv` directly. **CSV format:**

```csv
Address,Private Key,Secret Phrase,Status
0xYourWalletAddressHere,0xYourPrivateKeyHere,your seed phrase words go here,
0xAnotherWallet,0xAnotherPrivateKey,,DEPLETED
```

> **Security note:** Private keys and seed phrases are stored in plaintext in `wallets.csv`. Never commit this file to git (it's already in `.gitignore`).

### 6. Check balances

```bash
python3 check_balances.py
```

Shows BNB balance, TOKEN balance, USDT balance, and GasSponsor allowance for each wallet (allowance shows "N/A" in Direct mode).

## Usage

### Run a single swap

```bash
source .venv/bin/activate
python3 swap.py
```

### Dry run (preview without sending transactions)

```bash
python3 swap.py --dry-run
```

> Note: dry-run still executes prerequisite steps (whitelisting, funding GasSponsor) since those are needed to validate the full flow.

### Target a specific wallet

```bash
python3 swap.py --wallet 0xAnotherWallet
```

### Collect USDT to collection wallet (no swap)

```bash
# Collect from the next available wallet
python3 swap.py --collect-only

# Collect from a specific wallet
python3 swap.py --collect-only --wallet 0xADDRESS
```

### Run via cron (once per hour)

```bash
crontab -e
# Add (use --quiet for brief Telegram-friendly output):
0 * * * * cd /home/user/Sync/token-swap-bot && source .venv/bin/activate && python3 swap.py --quiet >> logs/cron.log 2>&1
```

Quiet mode output example (Sponsored):
```
✅ Swap: 100 TOKEN → 0.101365 USDT (0x1694...f056)
👛 Wallet: 4216.65 TOKEN | 0.101365 USDT
💰 Master: 0.0088 BNB | Sponsor: 0.0062 BNB
```

Quiet mode output example (Direct):
```
✅ Swap: 100 TOKEN → 0.101365 USDT (0x1694...f056)
👛 Wallet: 4216.65 TOKEN | 0.101365 USDT
💰 Master: 0.0088 BNB | Wallet BNB: 0.001540
```

On error:
```
❌ Swap failed for 100 TOKEN (0x1694...f056)
```

### Manage wallets

```bash
# List all wallets
python3 wallet_manager.py --list

# Add a new wallet with private key
python3 wallet_manager.py --add 0xNEWADDR --private-key 0xPRIVKEY

# Add a new wallet with mnemonic (auto-derives key)
python3 wallet_manager.py --add 0xNEWADDR --secret-phrase "twelve words here"

# Update wallet status
python3 wallet_manager.py --status DEPLETED --add 0xADDRESS

# Show next available wallet
python3 wallet_manager.py --next

# Create a fresh CSV
python3 wallet_manager.py --create

# Use a custom CSV path
python3 wallet_manager.py --path /path/to/custom.csv --list
```

### Stop the bot

```bash
python3 swap.py --stop       # creates STOP_BOT (halts future runs)
```

To resume:
```bash
python3 swap.py --resume     # removes STOP_BOT
```

Or manually:
```bash
touch STOP_BOT   # halt
rm STOP_BOT      # resume
```

The script checks for `STOP_BOT` on every run and exits immediately if found.

## CLI Reference

### swap.py

| Flag | Description |
|------|-------------|
| `--stop` | Create `STOP_BOT` kill-switch file |
| `--resume` | Remove `STOP_BOT` kill-switch file |
| `--config CONFIG` | Path to config file (default: `config.yaml`) |
| `--dry-run` | Show what would happen without sending swap transactions |
| `--collect-only` | Skip swap, only transfer token_out to collection wallet |
| `--wallet WALLET` | Target a specific wallet address instead of next available |
| `--quiet`, `-q` | Brief output only — shows summary/error line (for cron jobs) |

### wallet_manager.py

| Flag | Description |
|------|-------------|
| `--path PATH` | Path to CSV file (default: `wallets.csv`) |
| `--create` | Create a new empty CSV |
| `--add ADDRESS` | Add a wallet address |
| `--private-key KEY` | Private key for `--add` |
| `--secret-phrase PHRASE` | 12/24-word mnemonic for `--add` (auto-derives key) |
| `--status STATUS` | Update wallet status (requires `--add`) |
| `--list` | List all wallets and their statuses |
| `--next` | Show next available (non-DEPLETED) wallet |

## Safety Guards

| Guard | What it does |
|-------|-------------|
| `STOP_BOT` kill switch | `--stop` creates it; script exits immediately if found |
| Daily gas limit | Tracks BNB spent per day in `gas_tracker.json`, halts if exceeded |
| Slippage protection | `slippage_bps` in config sets minimum output amount |
| Swap deadline | `deadline_seconds` — swap reverts if not filled in time |
| Max retries | Each step retries up to 3 times with exponential backoff |
| Token recovery | If swap fails, tokens are automatically returned from GasSponsor to user |
| Balance check | Script checks wallet balance against `amount_per_run` |
| DEPLETED status | Empty wallets are permanently skipped |
| One swap per run | One wallet, one transaction — exits after completion |
| JSONL audit log | Every event logged to `swap_log.jsonl` |
| Receipt validation | Every transaction receipt is checked for success status |
| Whitelist protection | Only whitelisted addresses can call GasSponsor `execute()` |

## Architecture

### Execution Modes

The bot detects its mode at startup based on `GAS_SPONSOR_CONTRACT`:

| | Sponsored Mode | Direct Mode |
|---|---|---|
| **Trigger** | `GAS_SPONSOR_CONTRACT` is set | `GAS_SPONSOR_CONTRACT` is empty/omitted |
| **Who pays gas** | GasSponsor contract | Target wallet (funded by master) |
| **Wallet init** | Gas-Refund Loop (approve + sweep) | BNB top-up from master |
| **Swap caller** | Master → GasSponsor.execute() | Wallet → Router.exactInputSingle() |
| **Collection** | Via GasSponsor (pull + forward) | Wallet → collection wallet (direct transfer) |
| **On deplete** | Mark DEPLETED | Sweep BNB + mark DEPLETED |

### GasSponsor Contract (Sponsored Mode only)

The GasSponsor contract (`0xF36Aa6f2...`) is a generic executor with whitelist protection:

- `execute(to, data, value)` — calls any contract on behalf of the GasSponsor (whitelist-gated)
- `updateWhitelist(address, bool)` — owner manages the whitelist
- `receive()` — accepts BNB deposits for the gas pool
- `emergencyWithdrawBNB()` — owner can withdraw BNB in emergency
- `emergencyWithdrawToken(address)` — owner can recover stuck tokens

The **master wallet** (contract owner) orchestrates everything. Target wallets never need BNB after the initial gas-refund loop.

### PancakeSwap V3 Integration

- **SwapRouter**: `0x1b81D678ffb9C0263b24A97847620C99d213eB14`
- **QuoterV2**: `0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997`
- **Fee tiers**: 100 (0.01%), 500 (0.05%), 2500 (0.25%), 10000 (1%)
- Fee parameter type is `uint24` (not `uint256`) — critical for correct ABI encoding
- SwapRouter's `exactInputSingle` requires a `deadline` field in its params struct

### Multi-wallet Strategy

The CSV acts as a stateful queue — wallets at the top are used first. Once a wallet hits 0 tokens (or its remaining balance is less than `amount_per_run`), it gets `DEPLETED`. The script always picks the first non-DEPLETED wallet.

```text
Before:                     After 3 cron runs:
  Wallet A  → (empty)         Wallet A  → DEPLETED
  Wallet B  → (empty)         Wallet B  → DEPLETED
  Wallet C  → (empty)         Wallet C  → INITIALIZED (next)
```

### Mnemonic Derivation

If a wallet row has a Secret Phrase but no Private Key, the bot automatically derives the key using BIP-44 path `m/44'/60'/0'/0/0` (standard for BSC/ETH) and caches it in the CSV.

## Troubleshooting

**"Master wallet is NOT whitelisted and is NOT the owner"**
→ The master wallet must be the GasSponsor contract owner. If it is the owner, the bot auto-whitelists it. Otherwise, call `updateWhitelist(master_addr, true)` from the owner.

**"No valid quote from any fee tier"**
→ The token pair may not have liquidity on PancakeSwap V3. Check [DexScreener](https://dexscreener.com) for available pools. Add the correct fee tier to `config.yaml`.

**"GasSponsor needs BNB"**
→ The contract needs BNB to pay gas for sponsored swaps. The bot auto-funds it from the master wallet. Ensure the master wallet has sufficient BNB.

**"No available wallets"**
→ Add wallets with `wallet_manager.py --add` or edit `wallets.csv` directly.

**Swap reverts with `ExecutionFailed`**
→ Check that the PancakeSwap pool has sufficient liquidity. The bot recovers tokens automatically on failure.
