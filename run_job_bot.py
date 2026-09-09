import asyncio
import subprocess
import sys
import time
from pathlib import Path

import yaml

from src.telegram.telegram_manager import TelegramReportSender


ROOT = Path(__file__).resolve().parent
BUNDLE_FILE = ROOT / "config/search_bundle.yaml"
ACTIVE_CONFIG = ROOT / "config/search_config.yaml"

LOOP_DELAY_SECONDS = 3600


async def send_telegram(message):
    try:
        sender = TelegramReportSender()
        await sender.send_test_message(message)
    except Exception as exc:
        print(f"Telegram warning: {exc}")


def notify(message):
    print()
    print("=" * 70)
    print(message)
    print("=" * 70)
    print()

    try:
        asyncio.run(send_telegram(message))
    except Exception as exc:
        print(f"Telegram notification failed: {exc}")


def build_config(bundle, profile):
    config = {}

    for key, value in bundle.items():
        if key != "profiles":
            config[key] = value

    for key, value in profile.items():
        if key != "name":
            config[key] = value

    return config


def run_profile(bundle, profile):
    name = profile.get("name", "Unknown profile")

    config = build_config(bundle, profile)

    ACTIVE_CONFIG.write_text(
        yaml.safe_dump(
            config,
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    notify(f"Starting LinkedIn search: {name}")

    result = subprocess.run(
        [sys.executable, "main.py"],
        cwd=ROOT,
    )

    if result.returncode == 0:
        notify(f"Finished LinkedIn search: {name}")
    else:
        notify(
            f"LinkedIn search failed: {name}. "
            f"Exit code: {result.returncode}"
        )


def main():
    bundle = yaml.safe_load(
        BUNDLE_FILE.read_text(encoding="utf-8")
    )

    profiles = bundle.get("profiles", [])

    if not profiles:
        raise RuntimeError(
            "No search profiles found in config/search_bundle.yaml"
        )

    original_config = None

    if ACTIVE_CONFIG.exists():
        original_config = ACTIVE_CONFIG.read_text(
            encoding="utf-8"
        )

    profile_names = ", ".join(
        profile.get("name", "Unknown")
        for profile in profiles
    )

    notify(
        f"Job bot started. Profiles: {profile_names}"
    )

    cycle = 1

    try:
        while True:
            notify(f"Starting search cycle #{cycle}")

            for profile in profiles:
                run_profile(bundle, profile)

            notify(
                f"Search cycle #{cycle} completed. "
                f"Waiting 1 hour before next cycle."
            )

            cycle += 1

            time.sleep(LOOP_DELAY_SECONDS)

    except KeyboardInterrupt:
        notify("Job bot stopped manually")

    finally:
        if original_config is not None:
            ACTIVE_CONFIG.write_text(
                original_config,
                encoding="utf-8",
            )


if __name__ == "__main__":
    main()
