"""
VideoFactory - AI text provider (the "brain").

LLMProvider is the general shape every AI text service must follow, so we
can swap or add providers later without rewriting the factory.
GeminiProvider is the first real one. It talks to Google's official
Gemini API over HTTPS using only Python's built-in tools.

The key is sent in a request header (never in the web address), and is
never written to logs, the database, or the dashboard.
"""
import json
import socket
import urllib.error
import urllib.request

from .config import GEMINI_API_KEY, GEMINI_MODELS
from .db import get_db

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class LLMError(Exception):
    """A problem talking to the AI, explained in plain language."""


class LLMProvider:
    """Every AI text provider must offer these two methods."""

    name = "base"

    def is_configured(self):
        raise NotImplementedError

    def generate(self, prompt, job_id=None, temperature=0.7, json_mode=False, schema=None):
        """Return {'text': ..., 'model': ..., 'tokens': ...} or raise LLMError.

        schema: optional description of the exact JSON shape the answer must have."""
        raise NotImplementedError


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key=GEMINI_API_KEY, models=GEMINI_MODELS, timeout=120):
        self.api_key = api_key
        self.models = list(models)
        self.timeout = timeout

    def is_configured(self):
        return bool(self.api_key)

    def _call(self, model, prompt, temperature, json_mode, schema=None):
        config = {"temperature": temperature}
        if json_mode or schema:
            config["responseMimeType"] = "application/json"
        if schema:
            config["responseSchema"] = schema
        body = json.dumps({
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": config,
        }).encode("utf-8")
        request = urllib.request.Request(
            GEMINI_URL.format(model=model),
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _extract_text(data):
        for candidate in data.get("candidates", []) or []:
            pieces = []
            for part in (candidate.get("content") or {}).get("parts", []) or []:
                if part.get("thought"):
                    continue
                if part.get("text"):
                    pieces.append(part["text"])
            if pieces:
                return "".join(pieces).strip()
        return ""

    @staticmethod
    def _record_usage(model, tokens, job_id):
        with get_db() as conn:
            conn.execute(
                "INSERT INTO provider_usage "
                "(job_id, provider, category, units, unit_name, estimated_cost_usd) "
                "VALUES (?, ?, 'llm', ?, 'tokens', 0)",
                (job_id, f"gemini:{model}", tokens),
            )

    def generate(self, prompt, job_id=None, temperature=0.7, json_mode=False, schema=None):
        if not self.is_configured():
            raise LLMError("No Gemini key found. Redo Step 9 to save your key in the .env file.")

        problems = []
        for model in self.models:
            try:
                try:
                    data = self._call(model, prompt, temperature, json_mode, schema)
                except urllib.error.HTTPError as first:
                    # If this model refuses the exact-shape instructions, ask again without them.
                    if first.code == 400 and schema:
                        data = self._call(model, prompt, temperature, True, None)
                    else:
                        raise
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:600]
                code = exc.code
                if code == 400 and ("API_KEY_INVALID" in detail or "API key not valid" in detail):
                    raise LLMError("Google rejected the key. Create a new key in AI Studio and redo Step 9.")
                if "location is not supported" in detail.lower():
                    raise LLMError("Google says the Gemini API is not available from your location.")
                if code == 403:
                    raise LLMError("The key is not allowed to use Gemini. Create a new key in AI Studio and redo Step 9.")
                if code == 404:
                    problems.append(f"{model}: not available")
                elif code == 429:
                    problems.append(f"{model}: free limit reached or busy")
                elif code >= 500:
                    problems.append(f"{model}: Google temporarily busy ({code})")
                else:
                    problems.append(f"{model}: error {code}")
                continue
            except (TimeoutError, socket.timeout):
                problems.append(f"{model}: took too long to answer")
                continue
            except urllib.error.URLError:
                raise LLMError("Could not reach Google. Check the Mac's internet connection.")

            text = self._extract_text(data)
            if not text:
                block = (data.get("promptFeedback") or {}).get("blockReason")
                candidates = data.get("candidates") or [{}]
                reason = block or candidates[0].get("finishReason", "unknown")
                problems.append(f"{model}: empty answer ({reason})")
                continue

            usage = data.get("usageMetadata") or {}
            tokens = int(usage.get("totalTokenCount", 0) or 0)
            self._record_usage(model, tokens, job_id)
            return {"text": text, "model": model, "tokens": tokens}

        raise LLMError("No free Gemini model answered. Details: " + " | ".join(problems))
