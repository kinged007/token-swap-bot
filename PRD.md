## Product Requirements Document (PRD)

## 1. Executive Summary## 1.1 Objective
The purpose of this project is to build an automated framework that enables a fleet of secondary BNB Smart Chain (BSC) wallets containing target tokens but 0 BNB to perform token swaps and transfers. [5] 
## 1.2 The Problem

* Standard token contracts lack native gasless approval features (EIP-2612 Permit), requiring an initial on-chain approve() transaction before a smart contract can manage their assets. [6] 
* Executing transactions from a fleet of wallets typically requires distributing BNB to each account ("dusting").
* Dusting leaves residual BNB capital permanently trapped across multiple wallets, causing significant capital fragmentation and operational tracking overhead.

## 1.3 The Solution
A unified hybrid architecture consisting of:

   1. On-Chain Component: A custom routing smart contract deployed on BSC that acts as a secure intermediary executor. It holds a centralized pool of native BNB to absorb execution gas fees and maintains an access whitelist.
   2. Off-Chain Component: An automation engine (Python) that executes a precise one-time Gas-Refund Loop to handle token allowances without leaving trapped capital, and subsequently triggers automated trades.

------------------------------
## 2. System Architecture & Flow## 2.1 The Initialization Flow (One-Time Setup)

[Automation Engine] ──► 1. Calculates exact approval + sweep gas requirement
        │
        ├──► 2. Transfers precise BNB gas amount to empty target wallet
        ├──► 3. Triggers target wallet to grant unlimited allowance to Router Contract
        └──► 4. Instantly sweeps 100% of residual BNB back to Master Wallet

## 2.2 The Execution Flow (Continuous Swapping)

[Automation Engine] ──► 1. Initiates trade request via Whitelisted Account
                                │
                     [Gas Sponsor Router Contract]
                                │
                                ├──► Checks Whitelist Permission
                                ├──► Pulls target tokens from empty wallet
                                ├──► Swaps tokens via PancakeSwap V3
                                └──► Delivers swapped output assets to target wallet

------------------------------
## 3. Functional Requirements## 3.1 On-Chain Smart Contract Requirements

* Centralized Gas Pool: The contract must securely store native BNB deposited by the administrator to fund the execution costs of automated swaps.
* Dynamic Access Control: The contract must implement a dynamic authorization system (whitelist mapping). Only explicitly authorized addresses can invoke the execution methods, preventing external public exploitation of the BNB pool.
* Administrative Controls: The contract owner must have exclusive authority to add/remove addresses from the whitelist and withdraw unspent BNB in an emergency.
* Asset Retrieval: The contract must be capable of pulling specified token amounts directly from user wallets that have granted it a valid allowance.
* DEX Integration: The contract must interface directly with PancakeSwap V3 routing structures to execute trades and route the output assets back to the respective target wallets.

## 3.2 Off-Chain Automation Engine Requirements

* State Verification: The script must query the blockchain to identify whether a target wallet already possesses an active allowance for the router contract before proceeding.
* Precise Gas Provisioning: For uninitialized wallets, the engine must calculate real-time gas costs and provision the exact amount of BNB needed to cover the approval and sweeping sequence.
* Capital Recovery (The Sweep): Immediately following a confirmed allowance transaction, the script must calculate the target wallet’s exact remaining balance and extract 100% of the residual native capital back to the master balance.
* Continuous Trade Routing: For initialized wallets, the engine must construct swap payloads (mapping tokens, amounts, pool tiers, and slippage requirements) and dispatch them to the smart contract using an authorized whitelist key.

------------------------------
## 4. Key Architectural Functions## 4.1 On-Chain Contract Interface

* updateWhitelist(address, bool): Administrative function allowing the contract owner to grant or revoke an account's ability to trigger sponsored swaps.
* sponsorSwap(...): Core execution function accessible only by whitelisted callers. It pulls tokens from the target wallet, executes the swap on PancakeSwap V3, and deposits the resulting assets back into the target wallet using the contract’s BNB pool for gas.
* emergencyWithdrawBNB(): Safety feature allowing the owner to reclaim the contract's entire native BNB balance.

## 4.2 Off-Chain Script Orchestration

* checkAllowanceState(): Queries the token contract to determine if the target wallet needs to undergo initialization.
* executeGasRefundLoop(): Automates the sequence of funding the wallet, triggering the approval transaction, and executing the immediate recovery sweep of the remaining BNB.
* triggerSponsoredTrade(): Assembles the transaction payloads and sends them to the custom router contract using a whitelisted master account.

------------------------------
## 5. Security & Risk Mitigation

* Griefing/Drainage Prevention: The dynamic whitelist ensures that unauthorized bots or external users cannot execute transactions or drain the contract's centralized BNB gas pool.
* Slippage Enforcement: The automation engine must calculate and supply strict minimum output thresholds to the contract interface during live trading to prevent MEV/Sandwich exploitation on the liquidity pool.
* Concurrency & Nonce Collisions: When iterating across multiple wallets simultaneously during the setup phase, the engine must manage sequential transaction nonces for the Master Account to avoid processing overlaps.

Here is the expanded Security & Risk Mitigation section incorporating explicit business rules for tracking gas usage, followed by the User Acceptance Testing (UAT) parameters to ensure the system is completely safe and functional before production deployment.
------------------------------
## 5. Security & Risk Mitigation (Expanded)

## 5.1 Griefing & Gas Pool Drainage Prevention

* Strict Sender Validation: The smart contract must immediately revert any transaction where msg.sender is not explicitly set to true in the dynamic whitelist mapping. This check must occur at the entry point of the function to minimize execution gas overhead from malicious callers.
* Gas Consumption Rate Limiting: The off-chain automation engine must implement an internal tracker that monitors the total BNB spent by the contract's gas pool. If gas consumption exceeds a pre-defined daily threshold (e.g., 0.5 BNB per 24 hours), the script must pause all executions and alert the administrator.

## 5.2 Slippage & MEV Protection Business Rules

* Dynamic Minimum Output Calculation: The automation engine must query current liquidity pool depths and spot prices immediately before submitting a trade. It must never pass a static or zero value for the minimum output parameter (minAmountOut).
* Slippage Tolerance Cap: The engine must reject trade payloads if the required slippage tolerance to fulfill the order exceeds a maximum threshold (e.g., 2%). This prevents the contract from executing trades that are highly vulnerable to sandwich attacks or front-running by MEV bots.
* Deadline Enforcement: All swap payloads sent to the DEX router must include a short expiration deadline (e.g., maximum 3 minutes from broadcast time) to prevent transactions from sitting in the mempool and being executed at unfavorable historical prices during network congestion.

## 5.3 Concurrency, Nonce, and Network Failure Management

* Deterministic Nonce Management: The automation engine must utilize a local transactional lock or queue system to track sequential nonces for the Master Account. It must not rely solely on the blockchain’s getTransactionCount('pending') to prevent nonce collisions during simultaneous wallet initializations.
* Atomic Step Validation: During the off-chain Gas-Refund Loop, the engine must wait for an on-chain transaction receipt confirming the approve() step before broadcasting the subsequent sweep transaction. It must never broadcast the sweep speculatively.

------------------------------
## 6. User Acceptance Testing (UAT) Parameters
To consider this system ready for production, it must successfully pass the following test scenarios within a staging or local fork environment (e.g., Hardhat or Anvil mainnet fork).
## 6.1 Whitelist & Access Control Tests

| Test Case ID | Scenario Description | Expected Outcome | Pass/Fail Criteria |
|---|---|---|---|
| UAT-01 | Unauthorized wallet attempts to call sponsorSwap directly. | The transaction must immediately revert at the RPC level. | Pass: 0 BNB gas is consumed from the contract pool. Fail: Transaction executes or burns pool gas. |
| UAT-02 | Whitelisted Master Account calls sponsorSwap on behalf of an empty wallet. | The transaction executes successfully. | Pass: Target tokens are swapped and output is delivered to the empty wallet. Gas is billed to the contract. |
| UAT-03 | Contract Owner revokes a Master Account's whitelist status, and that account attempts a swap. | The transaction must revert. | Pass: System blocks the revoked account instantly. |

## 6.2 Gas-Refund Loop Tests (One-Time Setup)

| Test Case ID | Scenario Description | Expected Outcome | Pass/Fail Criteria |
|---|---|---|---|
| UAT-04 | Script initializes a wallet with 0 BNB and 0 allowance. | The loop executes: funds wallet, approves contract, sweeps remainder. | Pass: Target wallet allowance is maximum. Leftover BNB in target wallet is 0. Master Wallet recovers unused dust. |
| UAT-05 | Network gas prices spike mid-execution during the Gas-Refund Loop. | The script dynamically adjusts the sweep value or safely retries. | Pass: The sweep transaction does not fail due to underfunded gas costs. Capital does not get trapped. |

## 6.3 Financial & Operational Safety Tests

| Test Case ID | Scenario Description | Expected Outcome | Pass/Fail Criteria |
|---|---|---|---|
| UAT-06 | Contract Owner executes emergencyWithdrawBNB while the gas pool is funded. | All native BNB held inside the contract is moved back to the Owner wallet. | Pass: Contract balance becomes 0. Owner receives exact balance amount. |
| UAT-07 | A high-slippage swap is triggered on a low-liquidity token pair via the contract. | The transaction reverts safely inside the PancakeSwap router context. | Pass: Tokens are returned safely to the user wallet. No capital loss occurs due to slippage exploitation. |


[1] [https://www.chatprd.ai](https://www.chatprd.ai/learn/product-requirements-document)
[2] [https://www.jamasoftware.com](https://www.jamasoftware.com/requirements-management-guide/writing-requirements/functional-requirements-examples-and-templates/)
[3] [https://www.news.aakashg.com](https://www.news.aakashg.com/p/product-requirements-documents-prds)
[4] [https://qracorp.com](https://qracorp.com/guides_checklists/use-the-most-common-requirements-documents/)
[5] [https://dev.to](https://dev.to/cleancodestudio/new-product-initiative-requirements-gathering-automation-with-ai-agents-4k49)
[6] [https://academy.extropy.io](https://academy.extropy.io/pages/articles/review-eip-3009.html)
