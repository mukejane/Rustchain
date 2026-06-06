# SPDX-License-Identifier: MIT
import importlib.util
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MINER_PATH = ROOT / "miners" / "windows" / "rustchain_windows_miner.py"


def _load_windows_miner():
    spec = importlib.util.spec_from_file_location("windows_miner_under_test", MINER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ensure_ready_emits_attest_and_enroll_success_events(monkeypatch):
    module = _load_windows_miner()
    miner = module.RustChainMiner("RTC02811ff5e2bb4bb4b95eee44c5429cd9525496e7")
    events = []

    def fake_attest():
        miner.attestation_valid_until = time.time() + 580
        return True

    def fake_enroll():
        miner.enrolled = True
        miner.last_enroll = time.time()
        return True

    monkeypatch.setattr(miner, "attest", fake_attest)
    monkeypatch.setattr(miner, "enroll", fake_enroll)

    assert miner._ensure_ready(events.append)
    assert [event["type"] for event in events] == ["attest", "enroll"]
    assert events[0]["message"] == "Attestation submitted"
    assert events[0]["miner_id"] == miner.miner_id
    assert events[0]["attestation_ttl_seconds"] > 0
    assert events[1]["message"] == "Epoch enrollment succeeded"
    assert events[1]["miner_id"] == miner.miner_id


def test_ready_status_and_headless_format_include_lifecycle_details():
    module = _load_windows_miner()
    miner = module.RustChainMiner("RTC02811ff5e2bb4bb4b95eee44c5429cd9525496e7")
    miner.enrolled = True
    miner.attestation_valid_until = time.time() + 60
    events = []

    miner._emit_ready_status(events.append)

    assert events[0]["type"] == "status"
    assert events[0]["enrolled"] is True
    assert "Miner ready" in module._format_headless_event(events[0])
    assert "enrolled=yes" in module._format_headless_event(events[0])
    assert module._format_headless_event({
        "type": "attest",
        "message": "Attestation submitted",
        "miner_id": "windows_abc123",
        "attestation_ttl_seconds": 580,
    }) == "[attest] Attestation submitted miner_id=windows_abc123 ttl=580s"
    assert module._format_headless_event({
        "type": "enroll",
        "message": "Epoch enrollment succeeded",
        "miner_id": "windows_abc123",
    }) == "[enroll] Epoch enrollment succeeded miner_id=windows_abc123"


def test_ensure_ready_surfaces_attestation_diagnostics(monkeypatch):
    module = _load_windows_miner()
    miner = module.RustChainMiner("RTC02811ff5e2bb4bb4b95eee44c5429cd9525496e7")
    miner.last_attestation_error = (
        "submit rejected: HTTP 409 code=DUPLICATE_HARDWARE "
        "error=hardware_already_bound"
    )
    events = []

    monkeypatch.setattr(miner, "attest", lambda: False)

    assert not miner._ensure_ready(events.append)
    assert events == [{
        "type": "error",
        "message": (
            "Attestation failed: submit rejected: HTTP 409 "
            "code=DUPLICATE_HARDWARE error=hardware_already_bound"
        ),
    }]


def test_response_diagnostic_includes_safe_json_fields():
    module = _load_windows_miner()
    miner = module.RustChainMiner("RTC02811ff5e2bb4bb4b95eee44c5429cd9525496e7")

    class Response:
        status_code = 409

        def json(self):
            return {
                "code": "DUPLICATE_HARDWARE",
                "error": "hardware_already_bound",
                "message": "This hardware is already registered",
            }

    assert miner._response_diagnostic(Response()) == (
        "HTTP 409 code=DUPLICATE_HARDWARE error=hardware_already_bound "
        "message=This hardware is already registered"
    )


def test_windows_miner_uses_attested_wallet_for_eligibility(monkeypatch):
    module = _load_windows_miner()
    miner = module.RustChainMiner("RTC02811ff5e2bb4bb4b95eee44c5429cd9525496e7")
    miner.node_url = "https://node.example"
    calls = []

    class Response:
        ok = True

        def json(self):
            return {"eligible": False, "reason": "not_your_turn", "slot": 42}

    def fake_get(url, params=None, timeout=None):
        calls.append({"url": url, "params": params, "timeout": timeout})
        return Response()

    monkeypatch.setattr(module.requests, "get", fake_get)

    assert miner.check_eligibility() is False
    assert calls == [{
        "url": "https://node.example/lottery/eligibility",
        "params": {"miner_id": miner.wallet_address},
        "timeout": 10,
    }]
    assert miner.last_eligibility["slot"] == 42


def test_windows_miner_generates_signed_header_for_attested_wallet(monkeypatch):
    module = _load_windows_miner()
    monkeypatch.setattr(module.time, "time", lambda: 1234)
    miner = module.RustChainMiner("RTC02811ff5e2bb4bb4b95eee44c5429cd9525496e7")
    miner.last_eligibility = {"slot": 42}
    miner._pow_proof = {"chain": "zephyr"}

    header = miner.generate_header()

    assert header["miner_id"] == miner.wallet_address
    assert header["header"]["miner"] == miner.wallet_address
    assert header["header"]["slot"] == 42
    assert header["header"]["pow_proof"] == {"chain": "zephyr"}
    assert bytes.fromhex(header["message"]).decode() == (
        f"slot:42:miner:{miner.wallet_address}:ts:1234"
    )
    assert header["pubkey"] == miner.wallet_address
    assert header["signature"]


def test_windows_miner_submits_header_to_configured_node(monkeypatch):
    module = _load_windows_miner()
    miner = module.RustChainMiner("RTC02811ff5e2bb4bb4b95eee44c5429cd9525496e7")
    miner.node_url = "https://node.example"
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {"ok": True}

    def fake_post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return Response()

    monkeypatch.setattr(module.requests, "post", fake_post)
    header = {"miner_id": miner.wallet_address, "header": {}, "message": "00"}

    assert miner.submit_header(header) is True
    assert calls == [{
        "url": "https://node.example/headers/ingest_signed",
        "json": header,
        "timeout": 15,
    }]
