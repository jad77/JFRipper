"""Raw rip -> library file.

MPEG-2 / VC-1 (DVDs, some older Blu-rays) are converted to H.264 on the best encoder this
machine has; H.264 / HEVC (most Blu-rays) are copied untouched. All audio and disc subtitles
are kept. Then the file is named and filed (movies) or parked for naming (TV discs).
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import jobs, sorter
from .config import IS_MAC, conf
from .notify import notify
from .util import log, under

CODEC_NAMES = {"h264": "H.264", "hevc": "HEVC", "mpeg2video": "MPEG-2", "vc1": "VC-1"}
ENCODER_NAMES = {"qsv": "Quick Sync", "vaapi": "VA-API", "nvenc": "NVENC",
                 "videotoolbox": "VideoToolbox", "x264": "x264 (software)"}
JELLYFIN_FFMPEG = "/usr/lib/jellyfin-ffmpeg"
DEINTERLACE = "bwdif=mode=send_frame:parity=auto:deint=interlaced"  # only frames flagged interlaced

# Phones and browsers can't play these, and Jellyfin's live conversion of DTS can break the
# stream timing (iPhone spins forever, web gives up), so such discs get a Dolby Digital
# track made from the main one, set as default. The original track is kept after it.
NEEDS_COMPAT_AUDIO = ("dts", "truehd", "mlp", "pcm_bluray", "flac")

_detected = None
_detect_lock = threading.Lock()


def tool(name):
    configured = conf().encode[name]
    if configured:
        return configured
    bundled = os.path.join(JELLYFIN_FFMPEG, name)
    if os.path.exists(bundled):
        return bundled
    return shutil.which(name) or name


def encoder_args(name):
    """(global options, filter chain, codec options) for one encoder."""
    q = int(conf().encode["quality"])
    dev = conf().encode["render_device"]
    if name == "qsv":
        return (["-init_hw_device", f"qsv=hw:{dev}", "-filter_hw_device", "hw"],
                DEINTERLACE + ",format=nv12,hwupload=extra_hw_frames=64",
                ["-c:v", "h264_qsv", "-preset", "slower", "-global_quality", str(q),
                 "-profile:v", "high"])
    if name == "vaapi":
        return (["-init_hw_device", f"vaapi=hw:{dev}", "-filter_hw_device", "hw"],
                DEINTERLACE + ",format=nv12,hwupload",
                ["-c:v", "h264_vaapi", "-rc_mode", "CQP", "-qp", str(q), "-profile:v", "high"])
    if name == "nvenc":
        return ([], DEINTERLACE + ",format=yuv420p",
                ["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr",
                 "-cq", str(q), "-b:v", "0", "-profile:v", "high"])
    if name == "videotoolbox":
        # VideoToolbox quality runs 1-100, higher = better; ~66 lines up with CRF 21.
        vt_q = max(1, min(100, round(100 - q * 1.6)))
        return ([], DEINTERLACE + ",format=yuv420p",
                ["-c:v", "h264_videotoolbox", "-q:v", str(vt_q), "-profile:v", "high"])
    if name == "x264":
        return ([], DEINTERLACE + ",format=yuv420p",
                ["-c:v", "libx264", "-preset", "slow", "-crf", str(q), "-profile:v", "high"])
    raise ValueError(f"unknown encoder {name!r}; use one of {', '.join(ENCODER_NAMES)}")


def encoder_works(name):
    """Encode a few frames of a test pattern to see if this encoder runs here."""
    pre, vf, args = encoder_args(name)
    cmd = [tool("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", *pre,
           "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25", "-frames:v", "10",
           "-vf", vf, *args, "-f", "null", "-"]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def candidates():
    return ["videotoolbox", "x264"] if IS_MAC else ["qsv", "vaapi", "nvenc", "x264"]


def encoder():
    """The configured encoder, or the first that works here (checked once)."""
    global _detected
    with _detect_lock:
        if _detected is None:
            wanted = conf().encode["encoder"]
            if wanted != "auto":
                encoder_args(wanted)  # validates the name
                if not encoder_works(wanted):
                    log(f"encoder {wanted} failed a test encode; using it anyway as configured")
                _detected = wanted
            else:
                _detected = next((e for e in candidates() if encoder_works(e)), "x264")
            log(f"video encoder: {ENCODER_NAMES[_detected]}")
        return _detected


def probe(path, select, entries):
    out = subprocess.run([tool("ffprobe"), "-v", "error", "-select_streams", select,
                          "-show_entries", f"stream={entries}", "-of", "csv=p=0", path],
                         capture_output=True, text=True).stdout
    return [line.strip(" ,").split(",") for line in out.split("\n") if line.strip(" ,")]


def video_res(path):
    rows = probe(path, "v:0", "width,height")
    return "x".join(rows[0]) if rows else ""


def video_codec(path):
    rows = probe(path, "v:0", "codec_name")
    return rows[0][0] if rows else ""


def audio_sub_args(src):
    """-map/-c/-disposition args for audio and subtitles."""
    auds = probe(src, "a", "codec_name,channels")
    subs = probe(src, "s", "codec_name")
    maps, args = ["-map", "0:a?"], ["-c:a", "copy"]
    if auds and auds[0][0] in NEEDS_COMPAT_AUDIO:
        try:
            ch = min(6, int(auds[0][1]))
        except (IndexError, ValueError):
            ch = 6
        maps = ["-map", "0:a:0", "-map", "0:a"]
        args += ["-c:a:0", "ac3", "-b:a:0", "640k" if ch > 2 else "224k", "-ac:a:0", str(ch),
                 "-metadata:s:a:0", "title=Dolby Digital", "-disposition:a:0", "default"]
        args += [x for i in range(len(auds)) for x in (f"-disposition:a:{i + 1}", "0")]
    # Discs flag the full subtitles as default, which makes Jellyfin burn them into the
    # picture (a full re-encode) on every play. Leave subtitles off unless chosen.
    args += [x for i in range(len(subs)) for x in (f"-disposition:s:{i}", "0")]
    return maps + ["-map", "0:s?"], args + ["-c:s", "copy"]


def encode_cmd(src, out, progress):
    """(ffmpeg command, source codec, copying?, encoder used) for one title."""
    codec = video_codec(src)
    pre, used = [], None
    if codec in ("h264", "hevc"):
        video = ["-c:v", "copy"]  # Blu-ray H.264/HEVC already plays directly
    else:
        used = encoder()
        pre, vf, codec_args = encoder_args(used)
        video = ["-vf", vf, *codec_args]
    maps, stream_args = audio_sub_args(src)
    cmd = [tool("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-progress", progress,
           "-stats_period", "5", *pre,
           # Some DVDs change a colour tag mid-film (e.g. at the credits); ffmpeg would rebuild
           # the filter chain, and a hardware upload can't be rebuilt mid-stream.
           "-reinit_filter", "0",
           "-i", src, "-map", "0:v:0", *maps, *video, *stream_args, out]
    return cmd, codec, used is None, used


def describe_output(codec, copying, used):
    return {"out_codec": CODEC_NAMES.get(codec, codec) if copying else "H.264",
            "out_note": "copied, no re-encode" if copying else ENCODER_NAMES[used]}


def encode_failed(stamp, label, name, work, stderr, log_name="ffmpeg.log"):
    with open(os.path.join(work, log_name), "w") as f:
        f.write(stderr)
    log(f"encode '{label}' failed; rip kept in {work}")
    jobs.update(stamp, stage="failed", finished=time.time(), message="Encode failed")
    notify(f"Encode failed: {name}",
           f"The rip worked but the encode didn't. The raw rip is kept at {work}.\n\n"
           + "\n".join(stderr.strip().split("\n")[-5:]))


def name_hint(kind, stamp):
    if kind == "tv":
        return ("Name them on the status page, or run:\n"
                f'  jfripper name tv {stamp} --show "Show Name" --season 1 --first 5')
    return f'Name it on the status page, or run:\n  jfripper name movie {stamp} "Title" YEAR'


def encode(work):
    with open(os.path.join(work, "meta.json")) as f:
        meta = json.load(f)
    if meta.get("kind") == "tv":
        encode_tv(work, meta)
        return
    src, label = meta["source"], meta["label"]
    note = meta.get("rip_note") or ""
    stamp = meta["stamp"]
    name = meta["name"] or f"Unknown disc {stamp}"
    out = os.path.join(work, f"{name}.mkv")
    progress = os.path.join(work, "ffmpeg-progress.txt")
    cmd, codec, copying, used = encode_cmd(src, out, progress)
    jobs.update(stamp, stage="encoding", progress_file=progress, codec=codec,
                src_res=video_res(src), **describe_output(codec, copying, used))
    log(f"encode '{label}': {codec} -> {'copy' if copying else used}")
    start = time.time()
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        encode_failed(stamp, label, name, work, res.stderr)
        return
    mb = lambda p: os.path.getsize(p) // 2**20
    log(f"encode '{label}' done in {(time.time() - start) / 60:.0f} min "
        f"({mb(src)} MB -> {mb(out)} MB)")
    jobs.update(stamp, stage="filing", size_in=os.path.getsize(src),
                size_out=os.path.getsize(out), encode_secs=time.time() - start)

    # A disc with no usable label can't be looked up, so it goes straight to review.
    if meta["name"]:
        landed = str(sorter.handle_video(Path(out), force_type="movie"))
    else:
        landed = str(sorter.move_to_review(Path(out), "no disc label"))
    shutil.rmtree(work)

    if under(landed, conf().movies):
        title = os.path.splitext(os.path.basename(landed))[0]
        log(f"'{label}' filed as {landed}")
        jobs.update(stamp, stage="done", finished=time.time(), title=title, path=landed)
        notify(f"{title} is in Jellyfin",
               f"Ripped and filed: {landed}\n\n" + (f"{note}\n\n" if note else "")
               + "Poster and details fill in on Jellyfin's next library scan.")
    else:
        log(f"'{label}' needs a name: {landed}")
        jobs.update(stamp, stage="needs_name", finished=time.time(), path=landed)
        notify(f"Ripped '{label or 'disc'}', needs a name",
               f"I couldn't match the disc name '{label}' to a movie, so it's waiting at\n"
               f"{landed}\n\n" + (f"{note}\n\n" if note else "") + name_hint("movie", stamp))


def encode_tv(work, meta):
    """Encode each episode, then park them in the review folder until they're named."""
    stamp, label = meta["stamp"], meta["label"]
    name = meta["name"] or label or "TV disc"
    sources, durations = meta["sources"], meta["durations"]
    outs, start = [], time.time()
    for n, (src, secs) in enumerate(zip(sources, durations), 1):
        out = os.path.join(work, f"Episode {n:02d}.mkv")
        progress = os.path.join(work, f"ffmpeg-progress-{n:02d}.txt")
        cmd, codec, copying, used = encode_cmd(src, out, progress)
        jobs.update(stamp, stage="encoding", episode=n, duration=secs, progress_file=progress,
                    codec=codec, src_res=video_res(src), **describe_output(codec, copying, used))
        log(f"encode '{label}' episode {n}/{len(sources)}: {codec} -> {'copy' if copying else used}")
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            encode_failed(stamp, label, name, work, res.stderr, f"ffmpeg-{n:02d}.log")
            return
        outs.append(out)
    size_in = sum(os.path.getsize(s) for s in sources)
    size_out = sum(os.path.getsize(o) for o in outs)
    log(f"encode '{label}' done: {len(outs)} episodes in {(time.time() - start) / 60:.0f} min "
        f"({size_in // 2**20} MB -> {size_out // 2**20} MB)")

    folder = re.sub(r'[<>:"/\\|?*]', "", f"{name} (TV disc {stamp})").strip()
    dest = os.path.join(conf().review, folder)
    os.makedirs(dest)
    for o in outs:
        shutil.move(o, os.path.join(dest, os.path.basename(o)))
    shutil.rmtree(work)
    jobs.update(stamp, stage="needs_name", finished=time.time(), path=dest, episode=None,
                duration=sum(durations), size_in=size_in, size_out=size_out,
                encode_secs=time.time() - start)
    lengths = ", ".join(str(d // 60) for d in durations)
    from .rip import tv_hint
    hint = tv_hint(label)
    log(f"'{label}' TV disc: {len(outs)} episodes need a name: {dest}")
    notify(f"Ripped '{label or 'TV disc'}', needs a name",
           f"This looks like a TV disc: {len(outs)} episodes ({lengths} min), in disc order, "
           f"waiting at\n{dest}\n\n"
           + (f"The disc label suggests {hint}.\n\n" if hint else "")
           + (f"{meta['rip_note']}\n\n" if meta.get("rip_note") else "")
           + name_hint("tv", stamp))
