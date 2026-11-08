"""Telegram Bot API notifier and command listener (/kill, /status)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine

import httpx

from src.core.config import BotConfig

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """
    Sends real-time alerts to Telegram and processes incoming operator commands
    from the configured authorized chat_id.
    """

    def __init__(
        self,
        config: BotConfig,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._token = (config.secrets.telegram_bot_token or "").strip()
        self._chat_id = str(config.secrets.telegram_chat_id or "").strip()
        self._enabled = config.telegram.enabled and bool(self._token and self._chat_id)
        self._notify_on = set(config.telegram.notify_on)

        self._external_client = http_client is not None
        self._client = http_client or httpx.AsyncClient(timeout=15.0)
        self._last_update_id: int = 0

        # Sent message log (for testing/inspection when disabled or mocked)
        self.sent_messages: list[tuple[str, str]] = []

        # Background listener state
        self._listener_task: asyncio.Task[None] | None = None
        self._is_polling: bool = False
        self._on_kill_cb: Callable[[str], Coroutine[Any, Any, Any]] | None = None
        self._on_status_cb: Callable[[], Coroutine[Any, Any, str]] | None = None

    def reload_config(self, config: BotConfig) -> None:
        """Dynamically reload telegram credentials and activation flag without restart."""
        self.config = config
        self._token = (config.secrets.telegram_bot_token or "").strip()
        self._chat_id = str(config.secrets.telegram_chat_id or "").strip()
        self._enabled = config.telegram.enabled and bool(self._token and self._chat_id)
        self._notify_on = set(config.telegram.notify_on)
        logger.info("TelegramNotifier reloaded: enabled=%s, chat_id=%s", self._enabled, self._chat_id)
        if self._token and not self._is_polling:
            self.start_background_listener()

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def should_notify(self, event_category: str) -> bool:
        """Check whether notifications are enabled for the given category."""
        return event_category in self._notify_on

    async def send_direct_reply(self, text: str, target_chat_id: str | None = None) -> bool:
        """Send a direct response to the operator regardless of category filter."""
        dest = str(target_chat_id or self._chat_id or "").strip()
        if not self._token or not dest:
            return False
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {
            "chat_id": dest,
            "text": text,
            "parse_mode": "HTML",
        }
        try:
            resp = await self._client.post(url, json=payload)
            if resp.status_code == 200:
                logger.info("Telegram command reply sent successfully to %s", dest)
                return True
            logger.warning("Telegram send_direct_reply failed: HTTP %d %s", resp.status_code, resp.text)
            return False
        except Exception as e:
            logger.error("Telegram send_direct_reply error: %s", e)
            return False

    async def close(self) -> None:
        """Close background task and HTTP client session."""
        await self.stop_background_listener()
        if not self._external_client and not self._client.is_closed:
            await self._client.aclose()

    async def send_message(self, event_category: str, text: str) -> bool:
        """
        Send a notification message if the category is in `notify_on`.
        Always records to `self.sent_messages` for in-memory auditability.
        """
        if not self.should_notify(event_category):
            return False

        self.sent_messages.append((event_category, text))

        if not self._enabled:
            logger.debug("Telegram disabled; queued [%s] message locally", event_category)
            return True

        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        try:
            resp = await self._client.post(url, json=payload)
            if resp.status_code == 200:
                logger.info("Telegram notification sent successfully to %s", self._chat_id)
                return True
            logger.warning("Telegram sendMessage returned HTTP %d: %s", resp.status_code, resp.text)
            return False
        except Exception as e:
            logger.error("Failed to send Telegram notification: %s", e)
            return False

    def start_background_listener(
        self,
        on_kill_command: Callable[[str], Coroutine[Any, Any, Any]] | None = None,
        on_status_command: Callable[[], Coroutine[Any, Any, str]] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Launch continuous background polling for incoming operator commands."""
        if on_kill_command is not None:
            self._on_kill_cb = on_kill_command
        if on_status_command is not None:
            self._on_status_cb = on_status_command
        self._is_polling = True

        if self._listener_task is not None and not self._listener_task.done():
            return

        async def _loop() -> None:
            logger.info("Telegram background command listener started.")
            while self._is_polling:
                try:
                    if not self._token:
                        await asyncio.sleep(2.0)
                        continue
                    await self.poll_commands(
                        on_kill_command=self._on_kill_cb,
                        on_status_command=self._on_status_cb,
                        timeout=5,
                    )
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.debug("Telegram polling loop error: %s", e)
                    await asyncio.sleep(2.0)
            logger.info("Telegram background command listener stopped.")

        target_loop = loop
        if target_loop is None:
            try:
                target_loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

        if target_loop is not None and target_loop.is_running():
            self._listener_task = target_loop.create_task(_loop())
        else:
            try:
                self._listener_task = asyncio.create_task(_loop())
            except RuntimeError:
                logger.warning("No running asyncio event loop found to attach Telegram listener.")

    async def stop_background_listener(self) -> None:
        """Stop background command polling task."""
        self._is_polling = False
        if self._listener_task is not None:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None

    async def poll_commands(
        self,
        on_kill_command: Callable[[str], Coroutine[Any, Any, Any]] | None = None,
        on_status_command: Callable[[], Coroutine[Any, Any, str]] | None = None,
        mock_updates: list[dict[str, Any]] | None = None,
        timeout: int = 1,
    ) -> list[str]:
        """
        Check incoming Telegram updates and execute operator commands (/start, /status, /kill, /getid).
        Returns list of processed command strings.
        """
        processed: list[str] = []
        updates = mock_updates

        if updates is None:
            if not self._token:
                return processed
            url = f"https://api.telegram.org/bot{self._token}/getUpdates"
            params: dict[str, Any] = {"timeout": timeout}
            if self._last_update_id > 0:
                params["offset"] = self._last_update_id + 1
            try:
                resp = await self._client.get(url, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    updates = data.get("result") or []
                else:
                    return processed
            except Exception as e:
                logger.debug("Telegram getUpdates failed: %s", type(e).__name__)
                return processed

        for upd in updates:
            upd_id = int(upd.get("update_id", 0))
            if upd_id > self._last_update_id:
                self._last_update_id = upd_id

            msg = upd.get("message") or {}
            sender_chat_id = str(msg.get("chat", {}).get("id", "")).strip()
            if not sender_chat_id:
                continue

            text = (msg.get("text") or "").strip()
            if not text:
                continue

            logger.info("Received Telegram message: '%s' from chat_id=%s", text, sender_chat_id)

            # 1. /getid / /myid / /id — Always answer so user can discover their chat_id!
            if text.startswith("/getid") or text.startswith("/myid") or text.startswith("/id"):
                processed.append("/getid")
                id_reply = (
                    f"🆔 <b>Chat ID Anda:</b> <code>{sender_chat_id}</code>\n\n"
                    f"Salin Chat ID di atas ke menu <b>Pengaturan Web UI</b> (Notifikasi Telegram) "
                    f"atau ke file <code>.env</code> (<code>TELEGRAM_CHAT_ID={sender_chat_id}</code>)."
                )
                await self.send_direct_reply(id_reply, target_chat_id=sender_chat_id)
                continue

            # 2. /start or /help
            if text.startswith("/start") or text.startswith("/help"):
                processed.append(text.split()[0])
                welcome = (
                    "👋 <b>Solana Meme Trading Bot Aktif!</b>\n\n"
                    f"🆔 <b>Chat ID Anda:</b> <code>{sender_chat_id}</code>\n\n"
                    "Perintah yang tersedia:\n"
                    "• <code>/status</code> - Cek Live Equity, PnL, & Posisi Terbuka\n"
                    "• <code>/getid</code> - Cek ID chat Telegram Anda\n"
                    "• <code>/kill</code> - Emergency Kill Switch (Tutup semua posisi & stop bot)\n"
                )
                await self.send_direct_reply(welcome, target_chat_id=sender_chat_id)
                continue

            # 3. Security Verification for administrative commands (/kill, /status)
            is_authorized = not self._chat_id or (sender_chat_id == self._chat_id)
            if not is_authorized:
                logger.warning(
                    "Ignored unauthorized Telegram command '%s' from chat_id=%s (authorized: %s)",
                    text,
                    sender_chat_id,
                    self._chat_id,
                )
                denied_msg = (
                    f"⛔ <b>Akses Ditolak</b>\n"
                    f"Chat ID <code>{sender_chat_id}</code> belum diizinkan sebagai operator bot.\n"
                    f"Masukkan ID ini di pengaturan Web UI terlebih dahulu."
                )
                await self.send_direct_reply(denied_msg, target_chat_id=sender_chat_id)
                continue

            # 4. Authorized commands
            if text.startswith("/kill"):
                reason = text[len("/kill"):].strip() or "Operator sent /kill via Telegram"
                processed.append("/kill")
                if on_kill_command is not None:
                    await on_kill_command(reason)
                await self.send_direct_reply(f"🛑 <b>Kill switch activated:</b> {reason}", target_chat_id=sender_chat_id)
            elif text.startswith("/status"):
                processed.append("/status")
                if on_status_command is not None:
                    status_text = await on_status_command()
                    await self.send_direct_reply(status_text, target_chat_id=sender_chat_id)
                else:
                    await self.send_direct_reply("ℹ️ Bot berjalan normal. (Status provider belum siap)", target_chat_id=sender_chat_id)

        return processed


