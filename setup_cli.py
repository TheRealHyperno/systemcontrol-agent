

def stoat_setup():
    """Optional Stoat bot setup, appended to .env."""
    print()
    print(bold("── (Optional) Step 5: Stoat ───────────────"))
    print(dim("  Stoat is a free, open-source Discord alternative."))
    print(dim("  Skip this if you only want Discord.\n"))
    choice = ask(cyan("Set up Stoat too? (y/n):"), default="n")
    if choice.lower() != "y":
        return

    print(dim("\n  1. Go to stoat.chat → Settings → My Bots → New Bot"))
    print(dim("  2. Copy the bot token"))
    print(dim("  3. Invite the bot to your server"))
    print(dim("  4. Right-click a channel → Copy ID\n"))

    token = ask(cyan("Stoat bot token:"), secret=True)
    if not token:
        print(yellow("No token — skipping Stoat."))
        return
    channels = ask(cyan("Stoat channel ID(s), comma-separated:"))
    mode  = ask(cyan("Mode (WAKEWORD/MENTION/EVERYTHING):"), default="WAKEWORD")
    wake  = "hey claw"
    if mode.upper() == "WAKEWORD":
        wake = ask(cyan("Stoat wake word:"), default="hey claw")

    with open(".env", "a") as f:
        f.write(f"\n# Stoat\n")
        f.write(f"STOAT_BOT_TOKEN={token}\n")
        f.write(f"STOAT_ALLOWED_CHANNEL_IDS={channels}\n")
        f.write(f"STOAT_BOT_MODE={mode.upper()}\n")
        f.write(f"STOAT_WAKE_WORD={wake}\n")

    print(green(bold("✓ Stoat config added to .env")))
