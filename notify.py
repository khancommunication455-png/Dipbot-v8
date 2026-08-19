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