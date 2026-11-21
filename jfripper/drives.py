"""Optical drives on Linux (ioctls on /dev/srN) and macOS (drutil)."""
import glob
import os
import re
import subprocess
import time

from .config import IS_LINUX, IS_MAC, conf
from .util import log

# linux/cdrom.h
CDROMEJECT = 0x5309
CDROM_SELECT_SPEED = 0x5322
CDROM_LOCKDOOR = 0x5329
CDROM_DRIVE_STATUS = 0x5326
CDROM_DISC_STATUS = 0x5327
CDSL_CURRENT = 0x7FFFFFFF
CDS_DISC_OK = 4
CDS_AUDIO, CDS_MIXED = 100, 105


class LinuxDrive:
    audio_cd_supported = True

    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)

    def __repr__(self):
        return self.path

    def _ioctl(self, request, arg=0):
        import fcntl
        fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            return fcntl.ioctl(fd, request, arg)
        finally:
            os.close(fd)

    def has_disc(self):
        try:
            return self._ioctl(CDROM_DRIVE_STATUS, CDSL_CURRENT) == CDS_DISC_OK
        except OSError:
            return False

    def _udev(self):
        try:
            return subprocess.run(["udevadm", "info", "-q", "property", self.path],
                                  capture_output=True, text=True).stdout
        except OSError:
            return ""

    def is_audio_cd(self):
        """Pressed or burned audio CDs, including enhanced CDs with a data track."""
        try:
            if self._ioctl(CDROM_DISC_STATUS) in (CDS_AUDIO, CDS_MIXED):
                return True
        except OSError:
            pass
        return re.search(r"^ID_CDROM_MEDIA_TRACK_COUNT_AUDIO=[1-9]", self._udev(), re.M) is not None

    def media_kind(self):
        props = self._udev()
        return ("blu-ray" if "ID_CDROM_MEDIA_BD=1" in props
                else "dvd" if "ID_CDROM_MEDIA_DVD=1" in props else "")

    def eject(self):
        try:
            # Long error retries can leave the tray locked ("medium removal prevented").
            try:
                self._ioctl(CDROM_LOCKDOOR, 0)
            except OSError:
                pass
            self._ioctl(CDROMEJECT)
        except OSError as exc:
            log(f"eject failed: {exc!r}")

    def set_speed(self, speed):
        try:
            self._ioctl(CDROM_SELECT_SPEED, speed)
        except OSError as exc:
            log(f"drive speed change to {speed or 'max'} failed: {exc!r}")

    def makemkv_target(self):
        return f"dev:{self.path}"


class MacDrive:
    audio_cd_supported = True

    def __init__(self, index, product=""):
        self.index = int(index)
        self.product = product
        self.name = f"drive{self.index}"
        self.path = f"drive {self.index}" + (f" ({product})" if product else "")

    def __repr__(self):
        return self.path

    def _status(self):
        out = subprocess.run(["drutil", "-drive", str(self.index), "status"],
                             capture_output=True, text=True).stdout
        return parse_drutil_status(out)

    def has_disc(self):
        return bool(self._status()[1])

    def is_audio_cd(self):
        """A CD that macOS mounted as an audio CD (a data CD goes down the video path and is
        turned away there, as on Linux)."""
        return self._status()[0].upper().startswith("CD") and bool(self.mount_point())

    def mount_point(self, wait=20):
        """Where macOS mounted the audio CD (its tracks appear as AIFF files), or ''. The
        mount can lag the insert by a few seconds."""
        disk = self._status()[1]
        deadline = time.time() + wait
        while disk:
            mount = subprocess.run(["mount"], capture_output=True, text=True).stdout
            m = re.search(rf"^{re.escape(disk)}(?:s\d+)? on (.+) \(cddafs", mount, re.M)
            if m:
                return m.group(1)
            if time.time() > deadline:
                break
            time.sleep(2)
        return ""

    def media_kind(self):
        kind = self._status()[0].upper()
        return "blu-ray" if "BD" in kind else "dvd" if "DVD" in kind else ""

    def eject(self):
        res = subprocess.run(["drutil", "-drive", str(self.index), "eject"],
                             capture_output=True, text=True)
        if res.returncode != 0:
            log(f"eject failed: {res.stderr.strip() or res.stdout.strip()}")

    def set_speed(self, speed):
        pass  # macOS has no public call for this; the retry ladder still slows MakeMKV's retries

    def makemkv_target(self):
        """MakeMKV on macOS addresses drives as disc:N; match ours by its /dev/diskN."""
        from . import makemkv
        disk = self._status()[1]
        for index, device in makemkv.drives().items():
            if disk and device.replace("/dev/r", "/dev/") == disk:
                return f"disc:{index}"
        return "disc:0"


def parse_drutil_status(out):
    """drutil status text -> (media type, /dev/diskN or '')."""
    kind = re.search(r"Type:\s*(.+?)(?:\s{2,}|$)", out, re.M)
    disk = re.search(r"Name:\s*(/dev/disk\d+)", out)
    kind = kind.group(1).strip() if kind else ""
    if kind.lower().startswith("no media"):
        return "", ""
    return kind, disk.group(1) if disk else ""


def parse_drutil_list(out):
    """drutil list text -> [(index, product)]."""
    found = []
    for line in out.splitlines():
        m = re.match(r"^\s*(\d+)\s+(.*?)\s*$", line)
        if m:
            found.append((int(m.group(1)), re.sub(r"\s+", " ", m.group(2))))
    return found


def find_drives():
    wanted = conf().rip["drives"]
    if IS_LINUX:
        paths = wanted or sorted(glob.glob("/dev/sr[0-9]*"))
        return [LinuxDrive(p) for p in paths]
    if IS_MAC:
        listed = parse_drutil_list(subprocess.run(["drutil", "list"], capture_output=True,
                                                  text=True).stdout)
        products = dict(listed)
        indexes = [int(w) for w in wanted] if wanted else [i for i, _ in listed]
        return [MacDrive(i, products.get(i, "")) for i in indexes]
    raise SystemExit("JFRipper runs on Linux and macOS only")
