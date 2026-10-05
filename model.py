"""Small, injectable Ollama adapter; no paid API, network call, or model by default."""
from __future__ import annotations

import json
import math
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class ModelError(RuntimeError):
    pass


class Ollama:
    def __init__(self, model: str, url: str = "http://localhost:11434", timeout: float = 30):
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Use an HTTP(S) endpoint without embedded credentials")
        if timeout <= 0 or not model.strip():
            raise ValueError("A model and positive timeout are required")
        self.model, self.url, self.timeout = model, url.rstrip("/"), timeout

    def _post(self, endpoint: str, payload: dict) -> dict:
        request = Request(self.url + endpoint, json.dumps(payload).encode(),
                          {"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read(2_000_001)
            if len(body) > 2_000_000:
                raise ModelError("Model response exceeds 2 MB")
            data = json.loads(body)
            if not isinstance(data, dict) or data.get("error"):
                raise ModelError("Model returned an invalid response")
            return data
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
            # Do not include response bodies, prompts, or credential-bearing URLs.
            raise ModelError(f"Model request failed: {type(error).__name__}") from None

    def complete(self, prompt: str, *, system: str = "", structured: bool = False,
                 max_tokens: int = 512) -> dict:
        if not 1 <= max_tokens <= 4096:
            raise ValueError("max_tokens must be between 1 and 4096")
        payload = {"model": self.model, "prompt": prompt, "system": system,
                   "stream": False, "options": {"temperature": 0, "num_predict": max_tokens}}
        if structured:
            payload["format"] = "json"
        data = self._post("/api/generate", payload)
        if not isinstance(data.get("response"), str) or data.get("done") is not True:
            raise ModelError("Model response is incomplete")
        usage = {}
        for source, target in [("prompt_eval_count", "input_tokens"), ("eval_count", "output_tokens")]:
            value = data.get(source)
            if type(value) is not int or value < 0:
                raise ModelError("Model usage is missing or invalid")
            usage[target] = value
        return {"text": data["response"], **usage}

    def json(self, prompt: str, *, system: str = "") -> dict:
        try:
            data = json.loads(self.complete(prompt, system=system, structured=True)["text"])
            if not isinstance(data, dict):
                raise ValueError("Expected object")
            return data
        except (ValueError, TypeError):
            raise ModelError("Model did not return a JSON object") from None

    def embed(self, texts: list[str]) -> list[list[float]]:
        data = self._post("/api/embed", {"model": self.model, "input": texts, "truncate": False})
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ModelError("Invalid embedding count")
        dimension = None
        for vector in vectors:
            if not isinstance(vector, list) or not vector or any(
                type(x) not in {int, float} or not math.isfinite(x) for x in vector
            ):
                raise ModelError("Invalid embedding vector")
            dimension = dimension or len(vector)
            if len(vector) != dimension or not any(vector):
                raise ModelError("Invalid embedding dimensions or zero vector")
        return vectors
