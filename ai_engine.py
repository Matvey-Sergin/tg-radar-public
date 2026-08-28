import asyncio
import re
import time
from datetime import datetime

from groq import AsyncGroq

from config import Config
from logging_utils import log_event


class AIEngine:
    def __init__(self):
        self.clients = []
        for key in Config.GROQ_KEYS[:3]:
            self.clients.append({
                "api": AsyncGroq(api_key=key, max_retries=0),
                "retry_after": 0,
            })
        self.current_idx = 0
        log_event("AI", "INIT", "AI engine initialized", keys=len(self.clients))

    def _now(self):
        return datetime.now().strftime("%H:%M:%S.%f")[:-3]

    async def chat(self, messages, model="llama-3.1-8b-instant", temperature=0.1, json_mode=False):
        req_id = f"REQ-{int(time.time() * 1000)}"
        started_at = time.time()
        content_str = str(messages).lower()

        if json_mode and "json" not in content_str:
            messages.append({"role": "system", "content": "Respond strictly in JSON format."})

        message_chars = sum(len(str(message.get("content", ""))) for message in messages if isinstance(message, dict))
        log_event(
            "AI",
            "REQUEST",
            "Starting chat completion",
            req_id=req_id,
            model=model,
            json_mode=json_mode,
            messages=len(messages),
            message_chars=message_chars,
        )
        if not self.clients:
            log_event("AI", "CONFIG_ERROR", "No Groq API keys configured", req_id=req_id)
            return None, 0

        attempts = 0
        max_attempts = max(1, len(self.clients) * 2)
        while attempts < max_attempts:
            client_index = self.current_idx
            client_data = self.clients[client_index]

            if time.time() < client_data["retry_after"]:
                sleep_left = round(client_data["retry_after"] - time.time(), 2)
                log_event("AI", "SKIP_KEY", "Key is cooling down", req_id=req_id, key_index=client_index, sleep_left=sleep_left)
                self.current_idx = (self.current_idx + 1) % len(self.clients)
                attempts += 1
                continue

            try:
                kwargs = {"messages": messages, "model": model, "temperature": temperature}
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}

                log_event("AI", "ATTEMPT", "Calling provider", req_id=req_id, key_index=client_index, attempt=attempts + 1)
                response = await client_data["api"].chat.completions.create(**kwargs)
                content = response.choices[0].message.content
                tokens = getattr(response.usage, "total_tokens", 0)
                duration_ms = int((time.time() - started_at) * 1000)
                log_event(
                    "AI",
                    "SUCCESS",
                    "Chat completion succeeded",
                    req_id=req_id,
                    key_index=client_index,
                    tokens=tokens,
                    response_chars=len(content or ""),
                    duration_ms=duration_ms,
                )
                return content, tokens

            except Exception as exc:
                err = str(exc)
                if "429" in err or "Rate limit reached" in err:
                    match = re.search(r"try again in (\d+\.?\d*)s", err)
                    wait = float(match.group(1)) + 1.0 if match else (600 if "70b" in model else 15)
                    client_data["retry_after"] = time.time() + wait
                    log_event(
                        "AI",
                        "RATE_LIMIT",
                        "Provider key rate limited",
                        req_id=req_id,
                        key_index=client_index,
                        wait_seconds=round(wait, 2),
                    )
                elif "413" in err:
                    log_event("AI", "PROMPT_TOO_LARGE", "Provider rejected prompt as too large", req_id=req_id, error=err)
                    return None, 0
                else:
                    log_event("AI", "ERROR", "Provider call failed", req_id=req_id, key_index=client_index, error=err)

                self.current_idx = (self.current_idx + 1) % len(self.clients)
                attempts += 1
                await asyncio.sleep(0.5)

        log_event("AI", "EXHAUSTED", "All provider keys unavailable; sleeping", req_id=req_id, sleep_seconds=30)
        await asyncio.sleep(30)
        return None, 0
