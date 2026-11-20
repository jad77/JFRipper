"""Talking to makemkvcon: scanning a disc, ripping a title, and the read-error retry ladder."""
import os
import re
import shutil
import subprocess
import time
import urllib.request

from .config import IS_MAC, conf
from .util import log

BETA_KEY_PAGE = "https://forum.makemkv.com/forum/viewtopic.php?t=1053"
LINUX_SETTINGS = os.path.expanduser("~/.MakeMKV/settings.conf")
MAC_DOMAIN = "com.makemkv.MakeMKV"
MAC_APP = "/Applications/MakeMKV.app/Contents/MacOS/makemkvcon"
WATCH_POLL = 15  # seconds between progress checks during a rip


def ladder():
    """A DVD player reads at 1x and skips what it can't read; MakeMKV reads flat out and gives
    up. When a title won't rip, climb this ladder on the same insert:
      (what to call it, drive speed, MakeMKV io_* settings for that try)"""
    slow = int(conf().rip["slow_speed"])
    return [
        ("full speed", 0, {"io_ErrorRetryCount": None, "io_IgnoreReadErrors": None}),
        ("slow speed", slow, {"io_ErrorRetryCount": "64", "io_IgnoreReadErrors": None}),
        ("slow speed, skipping unreadable spots", slow,
         {"io_ErrorRetryCount": "16", "io_IgnoreReadErrors": "1"}),
    ]


def binary():
    configured = conf().rip["makemkvcon"]
    if configured:
        return configured
    for candidate in (shutil.which("makemkvcon"), MAC_APP):
        if candidate and os.path.exists(candidate):
            return candidate
    return "makemkvcon"


def scan_min():
    return int(conf().rip["scan_min_minutes"]) * 60


def stall_limit():
    return int(conf().rip["stall_minutes"]) * 60


def run(*args):
    return subprocess.run([binary(), "-r", f"--minlength={scan_min()}", *args],
                          capture_output=True, text=True).stdout


def drives():
    """MakeMKV's own drive list: {index: device path} for drives it can see."""
    out = subprocess.run([binary(), "-r", "--cache=1", "info", "disc:9999"],
                         capture_output=True, text=True).stdout
    return {int(i): dev for i, dev in
            re.findall(r'^DRV:(\d+),\d+,\d+,\d+,"[^"]*","[^"]*","([^"]+)"', out, re.M)}


def messages(out):
    """MakeMKV MSG lines -> their human-readable text."""
    return [m.group(1) for m in re.finditer(r'^MSG:\d+,\d+,\d+,"((?:[^"\\]|\\.)*)"', out, re.M)]


def read_errors(out):
    return [m for m in messages(out) if "corrupt or invalid" in m or "Read Error" in m]


def run_watched(progress, *args):
    """run(), but a try whose progress file stops changing for stall_minutes is stopped (one
    bad spot can hang for hours). Returns (output, stalled)."""
    log_path = progress + ".out"
    with open(log_path, "w") as out_file:
        proc = subprocess.Popen([binary(), "-r", f"--minlength={scan_min()}", *args],
                                stdout=out_file, stderr=subprocess.DEVNULL, text=True)
        last, changed, stalled = None, time.time(), False
        while proc.poll() is None:
            time.sleep(WATCH_POLL)
            try:
                st = os.stat(progress)
                now = (st.st_size, st.st_mtime)
            except FileNotFoundError:
                now = None
            if now != last:
                last, changed = now, time.time()
            elif time.time() - changed > stall_limit():
                stalled = True
                proc.terminate()
                try:
                    proc.wait(60)
                except subprocess.TimeoutExpired:
                    proc.kill()  # may still wait on the drive's own timeout; don't hang on it
                    try:
                        proc.wait(120)
                    except subprocess.TimeoutExpired:
                        pass
                break
    with open(log_path, errors="replace") as f:
        out = f.read()
    os.remove(log_path)
    return out, stalled


# ------------------------------------------------------------ MakeMKV's own settings

def _read_linux_settings():
    try:
        with open(LINUX_SETTINGS) as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _write_linux_settings(text):
    os.makedirs(os.path.dirname(LINUX_SETTINGS), exist_ok=True)
    with open(LINUX_SETTINGS, "w") as f:
        f.write(text)


def set_settings(settings):
    """Set (value) or clear (None) keys in MakeMKV's settings for the next makemkvcon run.
    Linux keeps them in ~/.MakeMKV/settings.conf, macOS in its preferences domain."""
    if IS_MAC:
        for key, value in settings.items():
            cmd = (["defaults", "delete", MAC_DOMAIN, key] if value is None
                   else ["defaults", "write", MAC_DOMAIN, key, "-string", value])
            subprocess.run(cmd, capture_output=True)
        return
    text = _read_linux_settings()
    for key, value in settings.items():
        text = re.sub(rf"^{key} = .*\n?", "", text, flags=re.M)
        if value is not None:
            text = text.rstrip("\n") + f'\n{key} = "{value}"\n'
    _write_linux_settings(text.lstrip("\n"))


def current_key():
    if IS_MAC:
        return subprocess.run(["defaults", "read", MAC_DOMAIN, "app_Key"],
                              capture_output=True, text=True).stdout.strip()
    m = re.search(r'^app_Key = "(.*)"', _read_linux_settings(), re.M)
    return m.group(1) if m else ""


def refresh_beta_key():
    """MakeMKV's free beta key rotates monthly and is posted on its forum. Opt-in
    (rip.refresh_beta_key); buy a key if you use MakeMKV a lot."""
    if not conf().rip["refresh_beta_key"]:
        return
    try:
        req = urllib.request.Request(BETA_KEY_PAGE, headers={"User-Agent": "Mozilla/5.0"})
        page = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
        key = re.search(r"T-[A-Za-z0-9@_]{60,}", page).group(0)
    except Exception as exc:
        log(f"beta key refresh skipped: {exc!r}")
        return
    if key != current_key():
        set_settings({"app_Key": key})
        log("MakeMKV beta key updated")


# ------------------------------------------------------------ ripping one title

def rip_title(drive, target, title, folder, progress, on_retry, log_path):
    """Rip one title, climbing the ladder on read errors. Returns (mkv path or None, ladder
    step that worked, unreadable spots skipped, MakeMKV output of the last try)."""
    out, stalled = "", False
    steps = ladder()
    try:
        for step, (how, speed, settings) in enumerate(steps):
            if step:
                if not read_errors(out) and not stalled:
                    break  # failed for some other reason; slowing down won't help
                why = (f"no progress for {stall_limit() // 60} min" if stalled
                       else f"{len(read_errors(out))} read errors")
                log(f"{drive}: {why}; trying title {title} again at {how}")
                on_retry(how)
                for f in os.listdir(folder):
                    os.remove(os.path.join(folder, f))
            drive.set_speed(speed)
            set_settings(settings)
            out, stalled = run_watched(progress, f"--progress={progress}", "--decrypt", "mkv",
                                       target, str(title), folder)
            if stalled:
                # Shown to rip_failed() like any other read error if this was the last try.
                out += (f'MSG:0,0,0,"Read Error: no progress for {stall_limit() // 60} minutes; '
                        'gave up","",""\n')
            with open(log_path, "a") as f:
                f.write(f"===== title {title}, {how} =====\n{out}")
            mkvs = [f for f in os.listdir(folder) if f.endswith(".mkv")]
            if mkvs and not stalled:
                skipped = len(read_errors(out)) if settings.get("io_IgnoreReadErrors") else 0
                return os.path.join(folder, mkvs[0]), step, skipped, out
        return None, None, 0, out
    finally:
        drive.set_speed(0)
        set_settings(steps[0][2])


def rip_note(step, skipped):
    """One line for the notification when a title needed the ladder ('' when it ripped normally)."""
    if not step:
        return ""
    if not skipped:
        return f"It needed a second try at {ladder()[step][0]}, but every spot read cleanly."
    spots = f"{skipped} unreadable spot{'s' if skipped != 1 else ''}"
    if skipped <= 5:
        return f"Ripped at slow speed; {spots} skipped, so there may be a split-second glitch."
    return (f"Ripped at slow speed with {spots} skipped: expect visible glitches. The disc "
            "may be worth cleaning and ripping again, or replacing.")
