import os
import sys
import time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from telegram_notifier import (
    get_telegram_config,
    test_telegram_connection as verify_telegram_connection,
    send_telegram_raw,
    build_alert_message,
    _async_send_worker,
)

def test_telegram_message_builder():
    """Unit test for Telegram HTML message generation (runs in pytest)."""
    firing_msg = build_alert_message(
        name="TargetDown",
        severity="critical",
        instance="101.101.101.101",
        summary="Service unreachable",
        job="blackbox",
        event_time=1787584676,
        is_now_firing=True,
        latency_ms=5001.6
    )
    assert "🔴 InfraWatch — Service Down" in firing_msg
    assert "Target     101.101.101.101" in firing_msg
    assert "Job        blackbox" in firing_msg
    assert "Status     UNREACHABLE" in firing_msg
    assert "Latency    5001.6 ms" in firing_msg
    assert "Investigate host availability." in firing_msg

    resolved_msg = build_alert_message(
        name="TargetDown",
        severity="critical",
        instance="156.154.71.1",
        summary="Service restored",
        job="blackbox",
        event_time=1787584676,
        is_now_firing=False,
        duration_seconds=15.0,
        latency_ms=18.4
    )
    assert "🟢 InfraWatch — Service Restored" in resolved_msg
    assert "Target     156.154.71.1" in resolved_msg
    assert "Job        blackbox" in resolved_msg
    assert "Status     OPERATIONAL" in resolved_msg
    assert "Downtime   15s" in resolved_msg
    assert "Latency    18.4 ms" in resolved_msg

def _fake_config(min_severity):
    return {
        "enabled": True, "bot_token": "x", "chat_id": "y",
        "send_firing": True, "send_resolved": True,
        "min_severity": min_severity
    }


def test_default_min_severity_holds_back_warnings():
    # Was "warning" (i.e. everything passes) despite being a documented gate
    # that nothing ever enforced — now that it's actually enforced, the
    # code-level default (no saved telegram_config.json, or one predating
    # this field) must match the app's stated policy: no Telegram push for
    # warning-severity alerts (SlowResponse) until a human opts back in.
    # Isolated from this machine's real telegram_config.json (a live,
    # gitignored file that may have its own saved min_severity) — this
    # checks the fallback default, not whatever's on disk right now.
    with patch("telegram_notifier.CONFIG_FILE", "/nonexistent/telegram_config.json"):
        config = get_telegram_config()
    assert config["min_severity"] == "critical"


def test_min_severity_gate_blocks_below_threshold():
    with patch("telegram_notifier.get_telegram_config", return_value=_fake_config("critical")), \
         patch("telegram_notifier.send_telegram_raw") as mock_send:
        _async_send_worker(
            name="SlowResponse", severity="warning", instance="host-a", summary="degraded",
            job="blackbox", event_time=1.0, is_now_firing=True)
        mock_send.assert_not_called()


def test_min_severity_gate_allows_at_threshold():
    with patch("telegram_notifier.get_telegram_config", return_value=_fake_config("critical")), \
         patch("telegram_notifier.send_telegram_raw", return_value=(True, "ok")) as mock_send:
        _async_send_worker(
            name="TargetDown", severity="critical", instance="host-a", summary="down",
            job="blackbox", event_time=1.0, is_now_firing=True)
        mock_send.assert_called_once()


def test_min_severity_is_user_configurable_to_let_warnings_through():
    # The whole point of fixing this as a real setting instead of a
    # hardcoded name check: an operator who WANTS warning-severity pushes
    # (this one, or any future Alertmanager warning rule) can just turn it
    # back on via PUT /api/telegram, no code change needed.
    with patch("telegram_notifier.get_telegram_config", return_value=_fake_config("warning")), \
         patch("telegram_notifier.send_telegram_raw", return_value=(True, "ok")) as mock_send:
        _async_send_worker(
            name="SlowResponse", severity="warning", instance="host-a", summary="degraded",
            job="blackbox", event_time=1.0, is_now_firing=True)
        mock_send.assert_called_once()


def run_tests():
    print("=" * 60)
    print("  InfraWatch v3 - Telegram Notification Test Suite")
    print("=" * 60)

    config = get_telegram_config()
    bot_token = config.get("bot_token", "")
    chat_id = config.get("chat_id", "")

    masked_token = bot_token[:8] + "..." + bot_token[-6:] if len(bot_token) > 14 else "(kosong)"
    print(f"Bot Token : {masked_token}")
    print(f"Chat ID   : {chat_id}")
    print(f"Enabled   : {config.get('enabled')}")
    print("-" * 60)

    if not bot_token or not chat_id:
        print("[!] Error: Bot token atau Chat ID belum dikonfigurasi di telegram_config.json")
        return

    # 1. Test Connection Message
    print("\n[1] Mengirim Pesan Uji Koneksi...")
    ok, msg = verify_telegram_connection(bot_token, chat_id)
    if ok:
        print("  [OK] Sukses: Pesan uji koneksi terkirim!")
    else:
        print(f"  [X] Gagal: {msg}")
        return

    # 2. Test Firing Alert (Simulasi Nginx / PostgreSQL Down)
    print("\n[2] Mengirim Simulasi Alert FIRING (PostgreSQL Down)...")
    firing_msg = build_alert_message(
        name="TargetDown",
        severity="critical",
        instance="192.168.9.16:5432",
        summary="Connection refused (PostgreSQL Server Down)",
        job="postgres_db",
        event_time=time.time(),
        is_now_firing=True,
        latency_ms=12.4
    )
    ok, msg = send_telegram_raw(bot_token, chat_id, firing_msg)
    if ok:
        print("  [OK] Sukses: Alert FIRING terkirim ke Telegram!")
    else:
        print(f"  [X] Gagal: {msg}")

    time.sleep(1)

    # 3. Test Resolved Alert (Simulasi Service Recovered)
    print("\n[3] Mengirim Simulasi Alert RESOLVED (PostgreSQL Recovered)...")
    resolved_msg = build_alert_message(
        name="TargetDown",
        severity="critical",
        instance="192.168.9.16:5432",
        summary="PostgreSQL service restored",
        job="postgres_db",
        event_time=time.time(),
        is_now_firing=False,
        duration_seconds=135.0,  # 2m 15s
        latency_ms=4.8
    )
    ok, msg = send_telegram_raw(bot_token, chat_id, resolved_msg)
    if ok:
        print("  [OK] Sukses: Alert RESOLVED terkirim ke Telegram!")
    else:
        print(f"  [X] Gagal: {msg}")

    print("\n" + "=" * 60)
    print("  Semua pengujian selesai! Silakan periksa grup Telegram Anda.")
    print("=" * 60)

if __name__ == "__main__":
    run_tests()
