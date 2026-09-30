import traceback

if __name__ == "__main__":
    try:
        from borasuki.app import main
        main()
    except Exception:
        import ctypes
        error = traceback.format_exc()
        from borasuki.storage import DATA
        log = DATA / "startup-error.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(error, encoding="utf-8")
        ctypes.windll.user32.MessageBoxW(None, error[-3000:], "Borasuki", 0x10)
