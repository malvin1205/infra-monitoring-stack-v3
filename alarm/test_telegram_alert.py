import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

from telegram_notifier import (
    get_telegram_config,
    test_telegram_connection as verify_telegram_connection,
    send_telegram_raw,
    build_alert_message,
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
