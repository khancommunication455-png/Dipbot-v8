"""Telegram alerts + remote commands. Uses plain requests against the Bot API.
Failures are swallowed — notifications must never break trading."""
import logging
import threading
import time
import requests

log = logging.getLogger("dipbot")

HELP = (
    "🤖 Bot commands:\n"
    "/status — current bot state\n"
    "/summary — realized P/L summary\n"
    "/close SYMBOL — market-sell one position (e.g. /close BTCUSDT)\n"
    "/closeall — market-sell every open position\n"
    "/pause — stop NEW entries (open positions still managed)\n"
    "/resume — allow new entries again\n"
    "/help — this message"
)

class Telegram:
    def __init__(self, token, chat_id, controller=None, commands_enabled=True):
        self.token, self.chat_id, self.controller = token, chat_id, controller
        self.commands_enabled = commands_enabled and bool(controller)
        self._offset = 0
        self._lock = threading.Lock()

    def send(self, text):
        if not (self.token and self.chat_id): return
        try:
            with self._lock:
                requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                              json={"chat_id": self.chat_id, "text": str(text)[:4000]}, timeout=10)
        except Exception as e:
            log.warning(f"Telegram send failed: {e}")

    def start(self):
        if not (self.token and self.chat_id): return
        if self.commands_enabled:
            threading.Thread(target=self._poll_loop, daemon=True, name="telegram-poll").start()
            log.info("Telegram enabled (alerts + commands)")
        else:
            log.info("Telegram enabled (alerts only)")

    def _poll_loop(self):
        while True:
            try:
                r = requests.get(f"https://api.telegram.org/bot{self.token}/getUpdates",
                                 params={"timeout": 25, "offset": self._offset}, timeout=35)
                for u in r.json().get("result", []):
                    self._offset = u["update_id"] + 1
                    msg = u.get("message") or u.get("edited_message")
                    if msg and str(msg.get("chat", {}).get("id")) == str(self.chat_id):
                        self._handle(msg.get("text", ""))
            except Exception:
                time.sleep(5)

    def _handle(self, text):
        text = (text or "").strip()
        if not text.startswith("/"): return
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        c = self.controller
        try:
            if cmd == "/help": self.send(HELP)
            elif cmd == "/status": self.send(c.status_text())
            elif cmd == "/summary": self.send(c.summary_text())
            elif cmd == "/pause": self.send(c.set_paused(True)["msg"])
            elif cmd == "/resume": self.send(c.set_paused(False)["msg"])
            elif cmd == "/close" and len(parts) > 1: self.send(c.close_symbol(parts[1])["msg"])
            elif cmd == "/closeall": self.send(c.close_all()["msg"])
            else: self.send("Unknown command — /help")
        except Exception as e:
            self.send(f"Command failed: {e}")

class Discord:
    """
    Simple Discord webhook notifier — one-way alerts only. No bot token, no
    hosting, no gateway connection needed: just a webhook URL from a Discord
    channel's Integrations settings. Does not support remote commands
    (that needs a real bot with a live gateway connection, not a webhook).
    """
    def __init__(self, webhook_url):
        self.webhook_url = webhook_url
        self._lock = threading.Lock()

    def send(self, text):
        if not self.webhook_url:
            return
        try:
            with self._lock:
                # Discord webhook messages cap at 2000 chars
                requests.post(self.webhook_url, json={"content": str(text)[:2000]}, timeout=10)
        except Exception as e:
            log.warning(f"Discord send failed: {e}")

    def start(self):
        if self.webhook_url:
            log.info("Discord webhook enabled (alerts only, no remote commands)")


class MultiNotify:
    """
    Fans out .send() to every configured channel (Telegram, Discord, both, or
    neither) so the rest of the bot only ever needs to call notify.send(...)
    without caring which providers are actually active. Remote commands still
    route through Telegram specifically, since that's the only provider that
    supports them.
    """
    def __init__(self, channels):
        self.channels = [c for c in channels if c is not None]

    def send(self, text):
        for c in self.channels:
            c.send(text)

    def start(self):
        for c in self.channels:
            c.start()

    @property
    def controller(self):
        for c in self.channels:
            if hasattr(c, "controller"):
                return c.controller
        return None

    @controller.setter
    def controller(self, value):
        for c in self.channels:
            if hasattr(c, "controller"):
                c.controller = value

    @property
    def commands_enabled(self):
        for c in self.channels:
            if hasattr(c, "commands_enabled"):
                return c.commands_enabled
        return False

    @commands_enabled.setter
    def commands_enabled(self, value):
        for c in self.channels:
            if hasattr(c, "commands_enabled"):
                c.commands_enabled = value
