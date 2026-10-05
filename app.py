"""A local gateway lab: isolated caches, atomic quota reservation, fallback, circuits."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import uuid

from model import ModelError, Ollama


class QuotaError(RuntimeError):
    pass


@dataclass(frozen=True)
class Policy:
    name: str
    input_credits: int = 1
    output_credits: int = 2
    max_output_tokens: int = 128

    def __post_init__(self):
        if (not self.name or type(self.input_credits) is not int or self.input_credits < 0
            or type(self.output_credits) is not int or self.output_credits < 0
            or type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 4096):
            raise ValueError("Invalid provider policy")


class Ledger:
    def __init__(self, path: Path):
        self.path = str(path)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts(tenant TEXT PRIMARY KEY, cap INTEGER NOT NULL, used INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY, tenant TEXT NOT NULL, amount INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, expires REAL NOT NULL, result TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def create_account(self, tenant: str, cap: int):
        if not tenant.strip() or type(cap) is not int or cap < 0:
            raise ValueError("Tenant and nonnegative integer quota required")
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO accounts VALUES(?,?,0)", (tenant, cap))

    def reserve(self, tenant: str, amount: int) -> str:
        if type(amount) is not int or amount < 0:
            raise ValueError("Invalid reservation amount")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            account = db.execute("SELECT cap, used FROM accounts WHERE tenant=?", (tenant,)).fetchone()
            pending = db.execute("SELECT COALESCE(SUM(amount),0) FROM reservations WHERE tenant=?", (tenant,)).fetchone()[0]
            if not account or account[1] + pending + amount > account[0]:
                raise QuotaError("Tenant quota exhausted or account missing")
            reservation = uuid.uuid4().hex
            db.execute("INSERT INTO reservations VALUES(?,?,?)", (reservation, tenant, amount))
            return reservation

    def settle(self, reservation: str, actual: int | None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT tenant, amount FROM reservations WHERE id=?", (reservation,)).fetchone()
            if not row:
                raise ValueError("Unknown or already settled reservation")
            tenant, reserved = row
            if actual is not None and (type(actual) is not int or not 0 <= actual <= reserved):
                raise ValueError("Actual charge exceeds reservation")
            # Unknown provider outcome consumes the full reservation, conservatively.
            db.execute("UPDATE accounts SET used=used+? WHERE tenant=?", (reserved if actual is None else actual, tenant))
            db.execute("DELETE FROM reservations WHERE id=?", (reservation,))

    def balance(self, tenant: str) -> dict:
        with self.connect() as db:
            cap, used = db.execute("SELECT cap, used FROM accounts WHERE tenant=?", (tenant,)).fetchone()
            pending = db.execute("SELECT COALESCE(SUM(amount),0) FROM reservations WHERE tenant=?", (tenant,)).fetchone()[0]
            return {"cap": cap, "used": used, "reserved": pending, "available": cap - used - pending}

    def get_cache(self, key: str, now: float):
        with self.connect() as db:
            row = db.execute("SELECT result FROM cache WHERE key=? AND expires>?", (key, now)).fetchone()
            return json.loads(row[0]) if row else None

    def put_cache(self, key: str, result: dict, expires: float):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?)", (key, expires, json.dumps(result)))


class DemoProvider:
    """Deterministic test double. Its output is not an LLM quality benchmark."""
    def __init__(self, name: str, fail: bool = False):
        self.model, self.fail, self.calls = name, fail, 0

    def complete(self, prompt: str, *, max_tokens: int):
        self.calls += 1
        if self.fail:
            raise ModelError("Injected provider failure")
        text = "Demo response: " + prompt[:80]
        return {"text": text, "input_tokens": max(1, len(prompt.encode()) // 4),
                "output_tokens": min(max_tokens, max(1, len(text.encode()) // 4))}


class Gateway:
    def __init__(self, ledger: Ledger, providers: dict, policies: dict[str, Policy],
                 *, clock=time.time, cache_ttl: float = 60, circuit_cooldown: float = 30):
        if set(providers) != set(policies) or not {"fast", "quality"}.issubset(providers):
            raise ValueError("Providers and policies must include fast and quality")
        if cache_ttl < 0 or circuit_cooldown < 0:
            raise ValueError("Invalid timeout")
        self.ledger, self.providers, self.policies = ledger, providers, policies
        self.clock, self.cache_ttl, self.cooldown = clock, cache_ttl, circuit_cooldown
        self.blocked_until, self.circuit_lock = {}, threading.Lock()

    def request(self, tenant: str, task: str, prompt: str) -> dict:
        if task not in {"classify", "reason"} or not prompt.strip() or len(prompt.encode()) > 8000:
            raise ValueError("Task must be classify/reason; prompt must be 1 to 8000 bytes")
        routes = ["fast", "quality"] if task == "classify" else ["quality", "fast"]
        config = [(name, self.providers[name].model, vars(self.policies[name])) for name in routes]
        cache_key = hashlib.sha256(json.dumps(["policy-v1", tenant, task, prompt, config], sort_keys=True).encode()).hexdigest()
        cached = self.ledger.get_cache(cache_key, self.clock())
        if cached:
            return {**cached, "cache_hit": True, "charged_credits": 0, "trace": [{"step": "cache_hit"}]}
        trace, total_charge = [], 0
        for name in routes:
            with self.circuit_lock:
                if self.blocked_until.get(name, 0) > self.clock():
                    trace.append({"step": "circuit_open", "provider": name})
                    continue
            policy = self.policies[name]
            # Lab estimate, not a universal tokenizer bound or a financial billing guarantee.
            input_ceiling = len(prompt.encode()) + 256
            reserved = input_ceiling * policy.input_credits + policy.max_output_tokens * policy.output_credits
            try:
                reservation = self.ledger.reserve(tenant, reserved)
            except QuotaError:
                trace.append({"step": "quota_denied", "provider": name})
                continue
            started = time.perf_counter()
            try:
                result = self.providers[name].complete(prompt, max_tokens=policy.max_output_tokens)
                if (not isinstance(result, dict) or not isinstance(result.get("text"), str)
                    or not result["text"].strip()
                    or type(result.get("input_tokens")) is not int or not 0 <= result["input_tokens"] <= input_ceiling
                    or type(result.get("output_tokens")) is not int or not 0 <= result["output_tokens"] <= policy.max_output_tokens):
                    raise ModelError("Invalid output or usage exceeds admitted limits")
            except (ModelError, TimeoutError, OSError):
                self.ledger.settle(reservation, None)
                total_charge += reserved
                with self.circuit_lock:
                    self.blocked_until[name] = self.clock() + self.cooldown
                trace.append({"step": "provider_failure", "provider": name, "charged_credits": reserved})
                continue
            actual = result["input_tokens"] * policy.input_credits + result["output_tokens"] * policy.output_credits
            self.ledger.settle(reservation, actual)
            total_charge += actual
            with self.circuit_lock:
                self.blocked_until.pop(name, None)
            trace.append({"step": "provider_success", "provider": name,
                          "latency_ms": round((time.perf_counter() - started) * 1000, 3)})
            output = {**result, "provider": name, "model": self.providers[name].model,
                      "cache_hit": False, "charged_credits": total_charge, "trace": trace}
            self.ledger.put_cache(cache_key, output, self.clock() + self.cache_ttl)
            return output
        raise QuotaError("No provider succeeded within the available quota and circuit policy")


def demo() -> dict:
    with tempfile.TemporaryDirectory() as directory:
        ledger = Ledger(Path(directory) / "gateway.db")
        ledger.create_account("tenant-a", 10000)
        ledger.create_account("tenant-b", 10000)
        providers = {"fast": DemoProvider("fixture-fast", fail=True), "quality": DemoProvider("fixture-quality")}
        policies = {"fast": Policy("fast"), "quality": Policy("quality", 2, 3)}
        gateway = Gateway(ledger, providers, policies)
        requests = [gateway.request(tenant, "classify", "Investigate an editor latency regression.")
                    for tenant in ["tenant-a", "tenant-a", "tenant-b"]]
        return {"mode": "simulation", "note": "Credits are illustrative quota units, not money or current provider pricing.",
                "requests": requests, "balances": {t: ledger.balance(t) for t in ["tenant-a", "tenant-b"]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["demo", "request"])
    parser.add_argument("--model", help="Installed Ollama model; required for real requests")
    parser.add_argument("--fallback-model")
    parser.add_argument("--prompt", default="Explain how to investigate an editor latency regression.")
    parser.add_argument("--task", choices=["classify", "reason"], default="reason")
    parser.add_argument("--tenant", default="local-demo")
    parser.add_argument("--db", type=Path, default=Path(".runtime/gateway.db"))
    args = parser.parse_args()
    try:
        if args.command == "demo":
            result = demo()
        else:
            if not args.model:
                raise ValueError("Provide --model to explicitly enable local model requests")
            args.db.parent.mkdir(parents=True, exist_ok=True)
            ledger = Ledger(args.db)
            ledger.create_account(args.tenant, 100000)
            providers = {"fast": Ollama(args.model), "quality": Ollama(args.fallback_model or args.model)}
            gateway = Gateway(ledger, providers, {"fast": Policy("fast"), "quality": Policy("quality", 2, 3)})
            result = gateway.request(args.tenant, args.task, args.prompt)
        print(json.dumps(result, indent=2))
    except (ValueError, QuotaError, ModelError) as error:
        parser.exit(2, f"Error: {error}\n")


if __name__ == "__main__":
    main()
