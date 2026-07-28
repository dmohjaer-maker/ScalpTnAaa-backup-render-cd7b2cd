"""
Account Service — business logic for account management.
"""

import logging
from typing import Optional
from ..models.account import Account
from ..config.constants import AccountType, ConnectionStatus
from ..storage.repositories.account_repo import AccountRepository
from ..storage.encryption import EncryptionService

logger = logging.getLogger(__name__)


class AccountService:
    def __init__(
        self,
        account_repo: AccountRepository,
        encryption: EncryptionService,
        mt5_service=None,
    ) -> None:
        self._repo = account_repo
        self._encryption = encryption
        self._mt5 = mt5_service

    async def get_all_accounts(self) -> list[Account]:
        accounts = await self._repo.get_all()
        # Enrich with live MT5 data if available
        for account in accounts:
            if account.is_enabled and self._mt5:
                try:
                    info = await self._mt5.get_account_info(account)
                    account.balance = info.get("balance", account.balance)
                    account.equity = info.get("equity", account.equity)
                    account.margin = info.get("margin", account.margin)
                    account.free_margin = info.get("free_margin", account.free_margin)
                    account.margin_level = info.get("margin_level", account.margin_level)
                    account.floating_profit = info.get("floating_profit", account.floating_profit)
                    raw_status = info.get("connection_status", "disconnected")
                    try:
                        account.connection_status = ConnectionStatus(raw_status)
                    except ValueError:
                        account.connection_status = ConnectionStatus.DISCONNECTED
                except Exception as e:
                    logger.warning(f"Failed to enrich account {account.id}: {e}")
        return accounts

    async def get_account(self, account_id: int) -> Optional[Account]:
        account = await self._repo.get_by_id(account_id)
        if account and self._mt5:
            try:
                info = await self._mt5.get_account_info(account)
                account.balance          = info.get("balance",          account.balance)
                account.equity           = info.get("equity",           account.equity)
                account.margin           = info.get("margin",           account.margin)
                account.free_margin      = info.get("free_margin",      account.free_margin)
                account.margin_level     = info.get("margin_level",     account.margin_level)
                account.floating_profit  = info.get("floating_profit",  account.floating_profit)
                account.leverage         = info.get("leverage",         account.leverage)
                raw_status = info.get("connection_status", "disconnected")
                try:
                    account.connection_status = ConnectionStatus(raw_status)
                except ValueError:
                    account.connection_status = ConnectionStatus.DISCONNECTED
            except Exception as e:
                logger.warning(f"Failed to enrich account {account_id}: {e}")
        return account

    async def get_active_account(self) -> Optional[Account]:
        account = await self._repo.get_active()
        if account and self._mt5:
            try:
                info = await self._mt5.get_account_info(account)
                account.balance = info.get("balance", account.balance)
                account.equity = info.get("equity", account.equity)
                account.margin = info.get("margin", account.margin)
                account.free_margin = info.get("free_margin", account.free_margin)
                account.margin_level = info.get("margin_level", account.margin_level)
                account.floating_profit = info.get("floating_profit", account.floating_profit)
            except Exception as e:
                logger.warning(f"Failed to enrich active account: {e}")
        return account

    async def add_account(
        self,
        name: str,
        account_type: AccountType,
        broker: str,
        server: str,
        login: str,
        password: str,
        **kwargs,
    ) -> Account:
        encrypted_pw = self._encryption.encrypt(password)
        account = Account(
            id=None,
            name=name,
            account_type=account_type,
            broker=broker,
            server=server,
            login=login,
            password_encrypted=encrypted_pw,
            **kwargs,
        )
        return await self._repo.create(account)

    async def delete_account(self, account_id: int) -> bool:
        return await self._repo.delete(account_id)

    async def switch_account(self, account_id: int) -> bool:
        return await self._repo.switch_active(account_id)

    async def enable_account(self, account_id: int) -> bool:
        return await self._repo.set_enabled(account_id, True)

    async def disable_account(self, account_id: int) -> bool:
        return await self._repo.set_enabled(account_id, False)

    async def test_connection(self, account_id: int) -> dict[str, object]:
        account = await self._repo.get_by_id(account_id)
        if not account:
            return {"success": False, "error": "Account not found"}
        if self._mt5:
            try:
                info = await self._mt5.get_account_info(account)
                return {
                    "success": True,
                    "status": info.get("connection_status", "unknown"),
                    "balance": info.get("balance", 0.0),
                }
            except Exception as e:
                return {"success": False, "error": str(e)}
        return {"success": False, "error": "MT5 service not available"}

    async def reconnect(self, account_id: int) -> bool:
        if self._mt5:
            result = await self._mt5.send_trade_command(
                "RECONNECT", {"account_id": account_id}
            )
            return result.get("success", False)
        return False
