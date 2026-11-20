"""Tell the owner what happened: ntfy, a webhook, a command, and/or email. All optional."""
import json
import shlex
import smtplib
import socket
import subprocess
import urllib.parse
import urllib.request
from email.message import EmailMessage

from .config import conf
from .util import log


def notify(subject, body):
    n = conf().notify
    url = conf().status["public_url"]
    body = body + (f"\n\nStatus page: {url}" if url else "") + f"\n\n- JFRipper on {socket.gethostname()}"
    log(f"notice: {subject}")
    for name, configured, send in (("ntfy", n["ntfy_url"], _ntfy),
                                   ("webhook", n["webhook_url"], _webhook),
                                   ("command", n["command"], _command),
                                   ("email", n["smtp_host"], _smtp)):
        if not configured:
            continue
        try:
            send(n, subject, body)
        except Exception as exc:
            log(f"{name} notification failed: {exc!r}")


def _post_json(url, payload, headers=None):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    urllib.request.urlopen(req, timeout=20).read()


def _ntfy(n, subject, body):
    # JSON publishing, so movie titles with accents survive (HTTP headers are Latin-1 only).
    u = urllib.parse.urlsplit(n["ntfy_url"])
    server, topic = f"{u.scheme}://{u.netloc}", u.path.strip("/")
    headers = {"Authorization": f"Bearer {n['ntfy_token']}"} if n["ntfy_token"] else {}
    _post_json(server, {"topic": topic, "title": subject, "message": body}, headers)


def _webhook(n, subject, body):
    # title/message for generic receivers; text = Slack, content = Discord.
    text = f"{subject}\n\n{body}"
    _post_json(n["webhook_url"], {"title": subject, "message": body, "text": text,
                                  "content": text[:1990]})


def _command(n, subject, body):
    subprocess.run(shlex.split(n["command"]) + [subject], input=body, text=True,
                   capture_output=True, timeout=60, check=True)


def _smtp(n, subject, body):
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, n["smtp_from"] or n["smtp_user"], n["smtp_to"]
    msg.set_content(body)
    with smtplib.SMTP(n["smtp_host"], int(n["smtp_port"]), timeout=30) as s:
        if n["smtp_starttls"]:
            s.starttls()
        if n["smtp_user"]:
            s.login(n["smtp_user"], n["smtp_password"])
        s.send_message(msg)
