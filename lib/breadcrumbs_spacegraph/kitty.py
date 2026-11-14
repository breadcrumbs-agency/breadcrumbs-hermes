"""The Kitty graphics protocol: one PNG on the terminal. Kitty, Ghostty and WezTerm read it (cmux is a Ghostty window).

image(png, cols) gives the escape text that puts the PNG at the cursor, scaled to cols cells wide. It
uses image id 1 and placement 1, so the next frame replaces the last one in place (no new image piles up).
Protocol: https://sw.kovidgoyal.net/kitty/graphics-protocol/ (f=100 is PNG; a payload over 4096 bytes goes
in chunks, m=1 on each chunk but the last).
"""
import base64
import os

CHUNK = 4096
TERMS = ("kitty", "ghostty", "wezterm")
MARKERS = ("KITTY_WINDOW_ID", "GHOSTTY_RESOURCES_DIR", "WEZTERM_PANE")  # set by the terminal, and kept by a shell or a tmux server started in it


def image(png: bytes, cols: int = 0, image_id: int = 1) -> str:
    data = base64.standard_b64encode(png).decode("ascii")
    chunks = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)] or [""]
    keys = "a=T,f=100,q=2,i=%d,p=1,C=1" % image_id + (",c=%d" % cols if cols > 0 else "")
    out = []
    for n, chunk in enumerate(chunks):
        more = 1 if n < len(chunks) - 1 else 0
        out.append("\x1b_G%s;%s\x1b\\" % ((keys + ",m=%d" % more) if n == 0 else "m=%d" % more, chunk))
    return "".join(out)


def frame(png: bytes, cols: int = 0) -> str:
    """Cursor home, then the image: each frame draws over the last."""
    return "\x1b[H" + image(png, cols)


def clear() -> str:
    return "\x1b_Ga=d,d=A,q=2\x1b\\"


def supported(env=os.environ) -> bool:
    """True in Kitty, Ghostty (and cmux, which is one) or WezTerm: TERM xterm-kitty / xterm-ghostty, TERM_PROGRAM ghostty or
    WezTerm, or a variable that the terminal sets (MARKERS)."""
    term = (env.get("TERM", "") + " " + env.get("TERM_PROGRAM", "")).lower()
    return any(env.get(m) for m in MARKERS) or any(t in term for t in TERMS)
