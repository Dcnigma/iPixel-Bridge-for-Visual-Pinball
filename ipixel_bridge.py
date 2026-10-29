#!/usr/bin/env python3
"""
ipixel_bridge.py - Rick and Morty VPX  <->  iPixel Color LED display (64x16, Bluetooth LE)

The table script cannot talk Bluetooth, so this small program keeps the BLE connection open and
listens on a local web port.  The table sends short commands, e.g.
    http://127.0.0.1:8765/text?msg=JACKPOT&color=ff2020
and the bridge draws them on the display.  Protocol from sdolphin-JP/ipixel-ctrl and yyewolf/go-ipxl.

Install (once):   pip3 install bleak pillow
Find the display: python3 ipixel_bridge.py scan
Tests (Mac / PC): python3 ipixel_bridge.py --addr <ADDR> info
                  python3 ipixel_bridge.py --addr <ADDR> test
                  python3 ipixel_bridge.py --addr <ADDR> text "WUBBA LUBBA" --color 00ff60
                  python3 ipixel_bridge.py --addr <ADDR> bench
Run for the table: python3 ipixel_bridge.py --addr <ADDR> serve
   (Mac: ADDR is the UUID from 'scan', Windows: the MAC address from 'scan' on the cabinet)

Web commands (GET, answer is immediate, the drawing happens in the background - newest wins):
  /text?msg=..&color=rrggbb&bg=rrggbb&size=big|small   one or two lines ("|" = new line), tighter spacing / scroll only if too long
  /score?v=12345678&color=..                           big digits with commas, never scrolls
  /image?file=C:\\path\\pic.png                         a picture (scaled to 64x16)
  /gif?file=..&slot=1                                   upload a GIF into a device slot (plays on its own)
  /slot?n=1                                             show stored slot 1..9
  /brightness?v=1..100     /power?on=1|0     /clear     /status
  /dmd?on=1|0                                           DMD mirror on/off (only with serve --dmd)

DMD mirror (any VPinMAME / FlexDMD / Future Pinball table, through freezy's dmdext):
  python3 ipixel_bridge.py --addr <ADDR> serve --dmd
  and in DmdDevice.ini:   [networkstream]  enabled = true   url = ws://127.0.0.1:8766/dmd   retry = true
  The 128x32 DMD is shrunk to 64x16 (every 2x2 dots -> 1 LED).  Commands from the Rick and Morty table
  (/text, /score ...) always win; the mirror pauses --dmd-pause seconds after the last one.
How will it look?  python3 ipixel_bridge.py dmdpreview screenshot.png   (no display needed, writes a preview)
"""
import argparse, asyncio, base64, hashlib, io, json, struct, sys, threading, time, zlib, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from bleak import BleakScanner, BleakClient
except ImportError:
    print("Please install bleak first:  pip3 install bleak pillow"); sys.exit(1)
from PIL import Image, ImageDraw

W, H = 64, 16
WRITE_UUID = "0000fa02-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000fa03-0000-1000-8000-00805f9b34fb"

# ------------------------------------------------------------------ 5x7 pixel font
FONT = {
 'A':["01110","10001","10001","11111","10001","10001","10001"],'B':["11110","10001","10001","11110","10001","10001","11110"],
 'C':["01110","10001","10000","10000","10000","10001","01110"],'D':["11110","10001","10001","10001","10001","10001","11110"],
 'E':["11111","10000","10000","11110","10000","10000","11111"],'F':["11111","10000","10000","11110","10000","10000","10000"],
 'G':["01110","10001","10000","10111","10001","10001","01111"],'H':["10001","10001","10001","11111","10001","10001","10001"],
 'I':["01110","00100","00100","00100","00100","00100","01110"],'J':["00111","00010","00010","00010","00010","10010","01100"],
 'K':["10001","10010","10100","11000","10100","10010","10001"],'L':["10000","10000","10000","10000","10000","10000","11111"],
 'M':["10001","11011","10101","10101","10001","10001","10001"],'N':["10001","10001","11001","10101","10011","10001","10001"],
 'O':["01110","10001","10001","10001","10001","10001","01110"],'P':["11110","10001","10001","11110","10000","10000","10000"],
 'Q':["01110","10001","10001","10001","10101","10010","01101"],'R':["11110","10001","10001","11110","10100","10010","10001"],
 'S':["01111","10000","10000","01110","00001","00001","11110"],'T':["11111","00100","00100","00100","00100","00100","00100"],
 'U':["10001","10001","10001","10001","10001","10001","01110"],'V':["10001","10001","10001","10001","10001","01010","00100"],
 'W':["10001","10001","10001","10101","10101","10101","01010"],'X':["10001","10001","01010","00100","01010","10001","10001"],
 'Y':["10001","10001","01010","00100","00100","00100","00100"],'Z':["11111","00001","00010","00100","01000","10000","11111"],
 '0':["01110","10001","10011","10101","11001","10001","01110"],'1':["00100","01100","00100","00100","00100","00100","01110"],
 '2':["01110","10001","00001","00010","00100","01000","11111"],'3':["11111","00010","00100","00010","00001","10001","01110"],
 '4':["00010","00110","01010","10010","11111","00010","00010"],'5':["11111","10000","11110","00001","00001","10001","01110"],
 '6':["00110","01000","10000","11110","10001","10001","01110"],'7':["11111","00001","00010","00100","01000","01000","01000"],
 '8':["01110","10001","10001","01110","10001","10001","01110"],'9':["01110","10001","10001","01111","00001","00010","01100"],
 ' ':["00000"]*7,'!':["00100","00100","00100","00100","00100","00000","00100"],'?':["01110","10001","00001","00010","00100","00000","00100"],
 '.':["00000","00000","00000","00000","00000","01100","01100"],',':["00000","00000","00000","00000","01100","00100","01000"],
 ':':["00000","01100","01100","00000","01100","01100","00000"],'-':["00000","00000","00000","11111","00000","00000","00000"],
 '+':["00000","00100","00100","11111","00100","00100","00000"],"'":["00100","00100","01000","00000","00000","00000","00000"],
 '#':["01010","01010","11111","01010","11111","01010","01010"],'%':["11000","11001","00010","00100","01000","10011","00011"],
 '/':["00000","00001","00010","00100","01000","10000","00000"],'X2':["00000"]*7,
}

def glyph_cols(ch):
    """Proportional font: the columns a character really uses (a space is 3 wide)."""
    if ch == ' ':
        return 0, 3
    g = FONT.get(ch, FONT['?'])
    used = [c for c in range(5) if any(row[c] == '1' for row in g)]
    if not used:
        return 0, 3
    return used[0], used[-1] - used[0] + 1

def text_width(s, sx=1, gap=1):
    w = 0
    for ch in s.upper():
        w += (glyph_cols(ch)[1] + gap) * sx
    return max(0, w - gap * sx)

def draw_text(img, s, x, y, col, sx=1, sy=1, gap=1):
    px = img.load()
    for ch in s.upper():
        g = FONT.get(ch, FONT['?'])
        first, width = glyph_cols(ch)
        if ch != ' ':
            for r, row in enumerate(g):
                for c in range(first, first + width):
                    if row[c] == '1':
                        for dx in range(sx):
                            for dy in range(sy):
                                X, Y = x + (c - first) * sx + dx, y + r * sy + dy
                                if 0 <= X < W and 0 <= Y < H:
                                    px[X, Y] = col
        x += (width + gap) * sx

def fit_gap(s):
    """1 pixel between the characters if it fits, else 0 (closer together); None = too long."""
    for gap in (1, 0):
        if text_width(s, 1, gap) <= W:
            return gap
    return None

def hexcol(h, default):
    try:
        h = (h or default).lstrip('#')
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except Exception:
        return hexcol(default, "ffffff")

def frames_for_text(msg, col, bg, size, scroll=True):
    """Returns a list of frames (images). One frame if it fits, else a scroll.
    Two lines ("|"): small font on top of each other, each line centred.  A line that is a bit
    too wide is drawn with the characters closer together instead of scrolling."""
    lines = msg.split('|')
    if len(lines) >= 2 or size == 'small':
        gaps = [fit_gap(l) for l in lines[:2]]
        if all(g is not None for g in gaps):
            img = Image.new('RGB', (W, H), bg)
            if len(lines) == 1:
                l = lines[0]
                draw_text(img, l, (W - text_width(l, 1, gaps[0])) // 2, 4, col, gap=gaps[0])
            else:
                for i, l in enumerate(lines[:2]):
                    draw_text(img, l, (W - text_width(l, 1, gaps[i])) // 2, 0 if i == 0 else 9, col, gap=gaps[i])
            return [img]
        msg = " ".join(lines)
    # big: 5x7 font stretched to 5x14
    g = fit_gap(msg)
    if g is not None or not scroll:
        g = 0 if g is None else g
        tw = text_width(msg, 1, g)
        img = Image.new('RGB', (W, H), bg)
        draw_text(img, msg, max(0, (W - tw) // 2), 1, col, 1, 2, gap=g)
        return [img]
    tw = text_width(msg)
    frames = []
    for off in range(W, -tw - 1, -2):
        img = Image.new('RGB', (W, H), bg)
        draw_text(img, msg, off, 1, col, 1, 2)
        frames.append(img)
    return frames

def frames_for_score(v, col):
    """The score never scrolls: big digits with commas, closer together when needed; above that
    the commas go, and if even that does not fit, two lines in the small font."""
    txt = "{:,}".format(v)
    if fit_gap(txt) is not None:
        return frames_for_text(txt, col, (0, 0, 0), 'big')
    txt = str(v)
    if fit_gap(txt) is not None:
        return frames_for_text(txt, col, (0, 0, 0), 'big')
    s = "{:,}".format(v)
    cut = s.rfind(',', 0, len(s) // 2 + 2)
    return frames_for_text(s[:cut + 1] + '|' + s[cut + 1:], col, (0, 0, 0), 'small', scroll=False)

# ------------------------------------------------------------------ packets
def packet(cmd, data=b""):
    n = 4 + len(data)
    return n.to_bytes(2, 'little') + cmd.to_bytes(2, 'little') + data

def frame_packet(img, bright=100):
    """go-ipxl 'camera' frame: raw RGB of the whole screen, type 0,0"""
    rgb = bytearray(img.convert('RGB').resize((W, H)).tobytes())
    if bright != 100:
        for i in range(len(rgb)):
            rgb[i] = rgb[i] * bright // 100
    n = len(rgb) + 9
    head = bytes([n & 255, (n >> 8) & 255, 0, 0, 0]) + (1024).to_bytes(4, 'little')
    return head + bytes(rgb)

# ---- DIY mode (what the iPhone app uses for live drawing): enter once, then coloured pixel lists
DIY_ENTER_CLEAR, DIY_QUIT_KEEP, DIY_ENTER_KEEP = 1, 2, 3

def diy_mode_packet(mode):
    return bytes([5, 0, 4, 1, mode])          # go-ipxl SwitchToDiyFunMode

def diy_pixel_packets(changes):
    """changes: {(x, y): (r, g, b)} -> packets [len, 0, 5, 1, 0, R, G, B, x, y, x, y ...] (max 120 pixels each)"""
    bycol = {}
    for xy, c in changes.items():
        bycol.setdefault(c, []).append(xy)
    out = []
    for c, pts in bycol.items():
        for i in range(0, len(pts), 120):
            part = pts[i:i + 120]
            body = bytes(v for xy in part for v in xy)
            head = bytes([8 + len(body), 0, 5, 1, 0, c[0], c[1], c[2]])
            out.append(head + body)
    return out

def frame_diff(old, img):
    new = {}
    px = img.convert('RGB').resize((W, H)).load()
    for y in range(H):
        for x in range(W):
            c = px[x, y]
            if old.get((x, y), (0, 0, 0)) != c:
                new[(x, y)] = c
    return new

def file_packet(cmd, raw, slot):
    """ipixel-ctrl send_png_data (0x0002) / send_gif_data (0x0003)"""
    d = bytes([0]) + len(raw).to_bytes(4, 'little') + (zlib.crc32(raw) & 0xFFFFFFFF).to_bytes(4, 'little') + bytes([0, slot]) + raw
    return packet(cmd, d)

def gif_for_device(path):
    src = Image.open(path)
    frames, durs = [], []
    try:
        while True:
            f = src.convert('RGB').resize((W, H))
            frames.append(f); durs.append(src.info.get('duration', 100))
            src.seek(src.tell() + 1)
    except EOFError:
        pass
    out = io.BytesIO()
    frames[0].save(out, 'GIF', save_all=True, append_images=frames[1:], duration=durs, loop=0)
    return out.getvalue()

# ------------------------------------------------------------------ BLE link
class Link:
    def __init__(self, addr, chunk, gap, response):
        self.addr, self.chunk, self.gap, self.response = addr, chunk, gap, response
        self.client = None
        self.reply = None
        self.screen = {}            # what the display shows now (DIY mode)
        self.diy = False
        self.lock = None            # one connection attempt at a time
        self.wlock = None           # one packet on the air at a time
        self.dlock = None           # one picture at a time (DMD mirror and table commands share the display)
        self.last_send = 0.0

    async def connect(self):
        if self.lock is None:
            self.lock, self.wlock, self.dlock = asyncio.Lock(), asyncio.Lock(), asyncio.Lock()
        async with self.lock:                   # only ONE connection attempt at a time
            await self._connect()

    async def _connect(self):
        if self.client and self.client.is_connected:
            return
        print("connecting to", self.addr, "...")
        self.client = BleakClient(self.addr, timeout=20)
        await self.client.connect()
        try:
            await self.client.start_notify(NOTIFY_UUID, self._notify)
        except Exception as e:
            print("  (no notifications:", e, ")")
        mtu = getattr(self.client, "mtu_size", 23) or 23
        if self.chunk <= 0:
            self.chunk = max(20, mtu - 3)
        print("connected, MTU", mtu, "-> chunks of", self.chunk, "bytes")
        self.diy = False
        self.screen = {}

    def _notify(self, _h, data):
        self.reply = bytes(data)

    async def send(self, data, wait=0.0):
        await self.connect()
        async with self.wlock:
            for i in range(0, len(data), self.chunk):
                await self.client.write_gatt_char(WRITE_UUID, data[i:i + self.chunk], response=self.response)
                if self.gap:
                    await asyncio.sleep(self.gap)
            self.last_send = time.time()
        if wait:
            await asyncio.sleep(wait)

    async def show(self, img, mode='diy', bright=100):
        """draw a 64x16 picture: DIY pixels (only what changed) or one raw frame"""
        if bright != 100:
            img = Image.eval(img.convert('RGB'), lambda v: v * bright // 100)
        await self.connect()
        async with self.dlock:
            return await self._show(img, mode)

    async def _show(self, img, mode):
        if mode == 'frame':
            await self.send(frame_packet(img))
            return 1
        if not self.diy:
            await self.send(diy_mode_packet(DIY_ENTER_CLEAR), 0.4)
            self.diy = True
            self.screen = {}
        ch = frame_diff(self.screen, img)
        pk = diy_pixel_packets(ch)
        for p in pk:
            await self.send(p)
        for xy, c in ch.items():
            self.screen[xy] = c
        return len(pk)

    async def info(self):
        t = time.localtime()
        self.reply = None
        await self.send(bytes([8, 0, 1, 128, t.tm_hour, t.tm_min, t.tm_sec, 0]), 1.5)
        return self.reply

# ------------------------------------------------------------------ bridge worker (newest command wins)
class Bridge:
    def __init__(self, link, fps, bright, mode='diy'):
        self.link, self.fps, self.bright, self.mode = link, fps, bright, mode
        self.loop = asyncio.new_event_loop()
        self.job, self.job_id = None, 0
        self.status = "starting"
        self.http_active, self.last_http, self.dmd_pause = 0, 0.0, 4.0
        self.dmd = None             # DmdMirror when started with --dmd

    def submit(self, job, http=True):
        """http=True: a command from the table - the DMD mirror pauses while it runs (+ dmd_pause s)"""
        self.job_id += 1
        jid = self.job_id
        if http:
            self.last_http = time.time()
        self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._run(jid, job, http)))

    def http_busy(self):
        return self.http_active > 0 or time.time() - self.last_http < self.dmd_pause

    async def _run(self, jid, job, http=True):
        if http:
            self.http_active += 1
        try:
            kind = job[0]
            if kind == 'frames':
                frames, repeat = job[1], job[2]
                n = 0
                while jid == self.job_id:
                    for f in frames:
                        if jid != self.job_id:
                            return
                        t0 = time.time()
                        await self.link.show(f, self.mode, self.bright)
                        dt = 1.0 / self.fps - (time.time() - t0)
                        if len(frames) > 1 and dt > 0:
                            await asyncio.sleep(dt)
                    n += 1
                    if len(frames) == 1 or (repeat and n >= repeat):
                        return
            elif kind == 'raw':
                await self.link.send(job[1], 0.3)
            self.status = "ok"
        except Exception as e:
            self.status = "error: %s" % e
            print("BLE error:", e)
            self.link.client = None
        finally:
            if http:
                self.http_active -= 1
                self.last_http = time.time()

    async def keepalive(self):
        # the display falls back to its own stored program when it is idle in DIY mode:
        # every 10 s without traffic it is told again to stay in DIY mode (keep the picture)
        while True:
            await asyncio.sleep(5)
            try:
                if self.link.diy and time.time() - self.link.last_send > 10:
                    await self.link.send(diy_mode_packet(DIY_ENTER_KEEP))
            except Exception as e:
                print("keepalive:", e)
                self.link.client = None

    def run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self.keepalive())
        if self.dmd:
            try:
                self.loop.run_until_complete(self.dmd.start())
            except Exception as e:
                print("DMD mirror could not start:", e)
        try:
            self.loop.run_until_complete(self.link.connect())
            self.status = "ok"
        except Exception as e:
            self.status = "error: %s" % e
            print("could not connect yet:", e)
        self.loop.run_forever()

def make_handler(bridge):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass
        def _ok(self, body="ok"):
            self.send_response(200); self.send_header("Content-Type", "text/plain"); self.end_headers()
            self.wfile.write(body.encode())
        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            p = u.path.strip('/')
            try:
                if p == 'text':
                    fr = frames_for_text(q.get('msg', ''), hexcol(q.get('color'), 'ffffff'), hexcol(q.get('bg'), '000000'), q.get('size', 'big'))
                    bridge.submit(('frames', fr, int(q.get('repeat', 2))))
                elif p == 'score':
                    v = int(float(q.get('v', '0')))
                    bridge.submit(('frames', frames_for_score(v, hexcol(q.get('color'), 'ffd000')), 1))
                elif p == 'image':
                    bridge.submit(('frames', [Image.open(q['file']).convert('RGB').resize((W, H))], 1))
                elif p == 'gif':
                    bridge.submit(('raw', file_packet(0x0003, gif_for_device(q['file']), int(q.get('slot', 1)))))
                elif p == 'slot':
                    bridge.submit(('raw', packet(0x8007, bytes([int(q.get('n', 1))]))))
                elif p == 'brightness':
                    bridge.submit(('raw', packet(0x8004, bytes([max(1, min(100, int(q.get('v', 50))))]))))
                elif p == 'power':
                    bridge.submit(('raw', packet(0x0107, bytes([1 if q.get('on', '1') == '1' else 0]))))
                elif p == 'clear':
                    bridge.submit(('frames', [Image.new('RGB', (W, H))], 1))
                elif p == 'dmd':
                    if not bridge.dmd:
                        return self._ok("DMD mirror not running (start with: serve --dmd)")
                    bridge.dmd.enabled = q.get('on', '1') == '1'
                    bridge.dmd.redraw = True
                    return self._ok("dmd " + ("on" if bridge.dmd.enabled else "off"))
                elif p == 'status':
                    return self._ok(bridge.status + (("; " + bridge.dmd.state()) if bridge.dmd else ""))
                else:
                    return self._ok("unknown command")
                self._ok()
            except Exception as e:
                self._ok("error: %s" % e)
    return H

# ------------------------------------------------------------------ DMD mirror (freezy's dmdext "network stream")
# VPinMAME (and FlexDMD, Future Pinball through "dmdext mirror") hands every DMD frame to DmdDevice.dll = dmdext.
# With [networkstream] enabled in DmdDevice.ini, dmdext sends the frames over a WebSocket.  This bridge is that
# WebSocket server.  Message format from LibDmd/Output/Network/WebsocketSerializer.cs:
#   name (ASCII) + 0x00 + data, numbers little endian
#   dimensions: w(int32) h(int32)        color: 0xRRGGBB(int32)       palette: n(int32) + n x 0xRRGGBB
#   gray2Planes / gray4Planes: time(uint32) + bit planes             clearColor / clearPalette / gameName
#   coloredGray2 / coloredGray4: time(uint32) + n(int32) + n colours + bit planes      rgb24: time + RGB bytes
# Bit planes: plane 0 (lowest bit) first, each w*h/8 bytes, row by row, bit i of a byte = pixel x+i.
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DMD_DEFAULT_TINT = (255, 48, 0)          # the classic orange (dmdext's default colour)
_EXP = [tuple((b >> i) & 1 for i in range(8)) for b in range(256)]

def _int_rgb(v):
    return ((v >> 16) & 255, (v >> 8) & 255, v & 255)

def _unmask(data, mask):
    n = len(data)
    m = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, 'little') ^ int.from_bytes(m, 'little')).to_bytes(n, 'little')

def dmd_planes_to_values(w, h, bits, planes):
    ps = w * h // 8
    vals = None
    for i in range(bits):
        flat = [bit for byte in planes[i * ps:(i + 1) * ps] for bit in _EXP[byte]]
        vals = flat if vals is None else [v | (b << i) for v, b in zip(vals, flat)]
    return vals

def dmd_to_rgb(frame, tint, gray_palette=None):
    """a received frame -> (w, h, [(r, g, b) ...]) at the DMD's own resolution"""
    if frame[0] == 'rgb24':
        _, w, h, data = frame
        return w, h, [tuple(data[i:i + 3]) for i in range(0, w * h * 3, 3)]
    _, w, h, bits, planes, pal = frame
    vals = dmd_planes_to_values(w, h, bits, planes)
    n = 1 << bits
    pal = pal or gray_palette
    if not pal:
        pal = [tuple(c * k // (n - 1) for c in tint) for k in range(n)]
    elif len(pal) != n:
        pal = [pal[k * (len(pal) - 1) // (n - 1)] for k in range(n)]
    return w, h, [pal[v] for v in vals]

def dmd_quant(c, levels):
    """few brightness steps and few hues = few colour groups = few BLE packets"""
    peak = max(c)
    q = round(min(1.0, peak / 255.0) * (levels - 1))
    if q <= 0:
        return (0, 0, 0)
    return tuple(int(round(v / peak * 4) / 4 * 255 * q / (levels - 1)) for v in c)

def dmd_downscale(w, h, rgb, scale='avg', gain=1.5, levels=4):
    """DMD (128x32, 192x64, ...) -> 64x16.  avg = mean of the dots under one LED (thin lines get dimmer,
    gain brightens them again), max = brightest dot (thin lines stay bright, text gets bolder)."""
    img = Image.new('RGB', (W, H))
    px = img.load()
    for oy in range(H):
        y0 = oy * h // H
        y1 = max(y0 + 1, (oy + 1) * h // H)
        for ox in range(W):
            x0 = ox * w // W
            x1 = max(x0 + 1, (ox + 1) * w // W)
            box = [rgb[y * w + x] for y in range(y0, y1) for x in range(x0, x1)]
            if scale == 'max':
                c = max(box, key=lambda p: p[0] * 3 + p[1] * 6 + p[2])
                c = tuple(min(255, v * gain) for v in c)
            else:
                k = gain / len(box)
                c = tuple(min(255, sum(p[i] for p in box) * k) for i in range(3))
            px[ox, oy] = dmd_quant(c, levels)
    return img

class DmdMirror:
    def __init__(self, bridge, host='127.0.0.1', port=8766, fps=10, scale='avg', gain=None, levels=4, color=None):
        self.bridge, self.host, self.port, self.fps = bridge, host, port, fps
        self.scale, self.levels = scale, max(2, levels)
        self.gain = gain if gain is not None else (1.5 if scale == 'avg' else 1.0)
        self.fixed_tint = hexcol(color, 'ff3000') if color else None
        self.tint, self.palette = self.fixed_tint or DMD_DEFAULT_TINT, None
        self.dims, self.game = (128, 32), ""
        self.latest, self.seq, self.shown_seq, self.last_key = None, 0, 0, None
        self.clients, self.idle_since, self.enabled, self.redraw = 0, time.time(), True, False
        self.n_in = self.n_out = 0

    def state(self):
        if not self.enabled:
            return "dmd off"
        if not self.clients:
            return "dmd: waiting for dmdext on ws://%s:%d/dmd" % (self.host, self.port)
        return "dmd: %s %dx%d" % (self.game or "connected", self.dims[0], self.dims[1])

    async def start(self):
        await asyncio.start_server(self._client, self.host, self.port)
        print("DMD mirror: waiting for dmdext on ws://%s:%d/dmd" % (self.host, self.port))
        asyncio.ensure_future(self._draw_loop())
        asyncio.ensure_future(self._stats())

    # ---- WebSocket server (tiny, no extra package needed)
    async def _client(self, reader, writer):
        counted = False
        try:
            head = (await reader.readuntil(b'\r\n\r\n')).decode('latin1')
            key = next((l.split(':', 1)[1].strip() for l in head.split('\r\n')
                        if l.lower().startswith('sec-websocket-key:')), None)
            if not key:
                writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            acc = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
            writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          "Sec-WebSocket-Accept: %s\r\n\r\n" % acc).encode())
            await writer.drain()
            self.clients += 1
            counted = True
            print("DMD: dmdext connected")
            msg = bytearray()
            while True:
                b0, b1 = await reader.readexactly(2)
                op, ln = b0 & 0x0F, b1 & 0x7F
                if ln == 126:
                    ln = struct.unpack('>H', await reader.readexactly(2))[0]
                elif ln == 127:
                    ln = struct.unpack('>Q', await reader.readexactly(8))[0]
                if ln > 8 * 1024 * 1024:
                    raise ValueError("message too big")
                mask = await reader.readexactly(4) if b1 & 0x80 else None
                data = await reader.readexactly(ln) if ln else b''
                if mask:
                    data = _unmask(data, mask)
                if op == 8:                                   # close
                    writer.write(b'\x88\x00')
                    await writer.drain()
                    break
                if op == 9:                                   # ping -> pong
                    writer.write(bytes([0x8A, len(data)]) + data)
                    await writer.drain()
                    continue
                if op == 10:
                    continue
                msg += data                                   # dmdext splits big frames into fragments
                if b0 & 0x80:
                    try:
                        self._message(bytes(msg))
                    except Exception as e:
                        print("DMD: bad message:", e)
                    msg = bytearray()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception as e:
            print("DMD: connection error:", e)
        finally:
            writer.close()
            if counted:
                self.clients -= 1
                self.idle_since = time.time()
                print("DMD: dmdext disconnected")

    def _message(self, data):
        name, _, rest = data.partition(b'\0')
        name = name.decode('ascii', 'replace')
        if name == 'dimensions':
            self.dims = struct.unpack('<ii', rest[:8])
        elif name == 'color':
            if not self.fixed_tint:
                self.tint = _int_rgb(struct.unpack('<i', rest[:4])[0])
        elif name == 'clearColor':
            if not self.fixed_tint:
                self.tint = DMD_DEFAULT_TINT
        elif name == 'palette':
            n = struct.unpack('<i', rest[:4])[0]
            self.palette = [_int_rgb(v) for v in struct.unpack('<%di' % n, rest[4:4 + 4 * n])]
        elif name == 'clearPalette':
            self.palette = None
        elif name == 'gameName':
            self.game = rest.split(b'\0')[0].decode('ascii', 'replace')
            print("DMD: game", self.game)
        elif name in ('gray2Planes', 'gray4Planes'):
            self._frame(int(name[4]), rest[4:], None)
        elif name in ('coloredGray2', 'coloredGray4', 'coloredGray6'):
            bits = int(name[-1])
            n = struct.unpack('<i', rest[4:8])[0]
            pal = [_int_rgb(v) for v in struct.unpack('<%di' % n, rest[8:8 + 4 * n])]
            self._frame(bits, rest[8 + 4 * n:], pal)
        elif name == 'rgb24':
            w, h = self.dims
            if len(rest) - 4 >= w * h * 3:
                self._set(('rgb24', w, h, rest[4:4 + w * h * 3]))

    def _frame(self, bits, planes, pal):
        w, h = self.dims
        ps = w * h // 8
        if len(planes) == bits * ps + 24:                 # coloredGray6 carries 24 extra bytes
            planes = planes[24:]
        if len(planes) != bits * ps:                      # dimensions not announced: guess the width
            if not h or (len(planes) // bits * 8) % h:
                return
            w = len(planes) // bits * 8 // h
        self._set(('planes', w, h, bits, planes, pal))

    def _set(self, frame):
        self.latest = frame
        self.seq += 1
        self.n_in += 1

    def render(self, frame):
        w, h, rgb = dmd_to_rgb(frame, self.tint, self.palette)
        return dmd_downscale(w, h, rgb, self.scale, self.gain, self.levels)

    # ---- drawing: always the NEWEST frame, as fast as BLE allows (frames in between are skipped)
    async def _draw_loop(self):
        was_busy = False
        while True:
            await asyncio.sleep(1.0 / self.fps)
            try:
                if not self.enabled:
                    continue
                if self.clients == 0:
                    if self.latest is not None and time.time() - self.idle_since > 2:
                        self.latest, self.last_key = None, None   # table closed: blank the display once
                        if not self.bridge.http_busy():
                            await self.bridge.link.show(Image.new('RGB', (W, H)), self.bridge.mode)
                    continue
                if self.bridge.http_busy():                       # the table's own texts / score win
                    was_busy = True
                    continue
                force = was_busy or self.redraw
                was_busy = self.redraw = False
                frame, seq = self.latest, self.seq
                if frame is None or (seq == self.shown_seq and not force):
                    continue
                self.shown_seq = seq
                img = self.render(frame)
                key = img.tobytes()
                if key == self.last_key and not force:
                    continue
                await self.bridge.link.show(img, self.bridge.mode, self.bridge.bright)
                self.last_key = key
                self.n_out += 1
            except Exception as e:
                print("DMD draw error:", e)
                self.bridge.link.client = None
                await asyncio.sleep(1)

    async def _stats(self):
        while True:
            await asyncio.sleep(10)
            if self.clients and self.n_in:
                print("DMD %s: %.1f frames/s from dmdext, %.1f frames/s on the display"
                      % (self.game or "", self.n_in / 10.0, self.n_out / 10.0))
            self.n_in = self.n_out = 0

def dmd_preview(path, scale='avg', gain=None, levels=4, color=None, out=None):
    """How will a DMD look on 64x16?  Takes a DMD picture (128x32, or a screenshot of a DMD window, PNG/GIF)
    and writes a side-by-side preview: the DMD on top, the iPixel LEDs below.  Needs no display."""
    gain = gain if gain is not None else (1.5 if scale == 'avg' else 1.0)
    tint = hexcol(color, 'ff3000') if color else None
    src = Image.open(path)
    sheets, durs = [], []
    try:
        while True:
            f = src.convert('RGB')
            if f.size not in ((128, 32), (192, 64), (256, 64), (128, 16)):
                f = f.resize((128, 32), Image.BOX)        # screenshot -> one value per DMD dot
                mx = max(f.tobytes()) or 1
                if mx < 255:                              # the gaps between the dots made it darker
                    f = Image.eval(f, lambda v: min(255, v * 255 // mx))
            if tint:                                      # grey picture -> DMD colour
                f = Image.merge('RGB', [f.convert('L').point(lambda v, c=c: v * c // 255) for c in tint])
            b = f.tobytes()
            led = dmd_downscale(f.width, f.height, list(zip(b[0::3], b[1::3], b[2::3])), scale, gain, levels)
            sheets.append(preview_sheet(f, led))
            durs.append(src.info.get('duration', 100))
            src.seek(src.tell() + 1)
    except EOFError:
        pass
    if not out:
        base = path.rsplit('.', 1)[0]
        out = base + ('_ipixel.gif' if len(sheets) > 1 else '_ipixel.png')
    if len(sheets) > 1:
        sheets[0].save(out, save_all=True, append_images=sheets[1:], duration=durs, loop=0)
    else:
        sheets[0].save(out)
    return out

def preview_sheet(dmd, led, width=768):
    """the DMD as round dots on top, the 64x16 LEDs (same physical width) below"""
    def dots(img, cell):
        out = Image.new('RGB', (img.width * cell, img.height * cell), (12, 12, 12))
        d = ImageDraw.Draw(out)
        px = img.load()
        r = cell * 0.42
        for y in range(img.height):
            for x in range(img.width):
                c = px[x, y]
                if max(c) < 10:
                    c = (28, 22, 20)                      # an unlit dot is still a little visible
                cx, cy = x * cell + cell / 2, y * cell + cell / 2
                d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=c)
        return out
    a = dots(dmd, max(1, width // dmd.width))
    b = dots(led, width // W)
    sheet = Image.new('RGB', (max(a.width, b.width), a.height + b.height + 16), (0, 0, 0))
    sheet.paste(a, (0, 0))
    sheet.paste(b, (0, a.height + 16))
    return sheet

# ------------------------------------------------------------------ command line
async def cli(a):
    if a.cmd == 'scan':
        for d in await BleakScanner.discover(timeout=8):
            if d.name and d.name.startswith('LED_BLE_'):
                print("ADDR =", d.address, " NAME =", d.name)
        return
    link = Link(a.addr, a.chunk, a.gap, not a.fast)
    await link.connect()
    if a.cmd == 'info':
        r = await link.info()
        print("reply:", r.hex(" ") if r else None)
        if r and len(r) > 4:
            sizes = {131: "64x16", 132: "96x16", 129: "32x32", 130: "32x16", 128: "64x64", 138: "64x32"}
            print("display type byte", r[4], "->", sizes.get(r[4], "unknown"))
    elif a.cmd == 'test':
        img = Image.new('RGB', (W, H))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W - 1, H - 1], outline=(255, 0, 0))
        d.line([0, 0, W - 1, H - 1], fill=(0, 255, 0)); d.line([0, H - 1, W - 1, 0], fill=(0, 0, 255))
        n = await link.show(img, a.mode, a.bright)
        print("sent (%s mode, %d packets): red border with a green and a blue diagonal" % (a.mode, n))
        await asyncio.sleep(a.hold)
    elif a.cmd == 'text':
        fr = frames_for_text(a.msg, hexcol(a.color, 'ffffff'), (0, 0, 0), a.size)
        for f in fr:
            await link.show(f, a.mode, a.bright); await asyncio.sleep(1.0 / a.fps)
        print("sent", len(fr), "frame(s)")
        await asyncio.sleep(a.hold)
    elif a.cmd == 'bench':
        # realistic: a scrolling text, every frame only the changed pixels
        fr = frames_for_text("WUBBA LUBBA DUB DUB", (0, 255, 90), (0, 0, 0), 'big')[:40]
        t0 = time.time(); pk = 0
        for f in fr:
            pk += await link.show(f, a.mode, a.bright)
        dt = time.time() - t0
        print("%d scroll frames (%d packets) in %.1f s = %.1f frames per second" % (len(fr), pk, dt, len(fr) / dt))
        await asyncio.sleep(a.hold)
    elif a.cmd == 'erase':
        # ipixel-ctrl erase-data --all: delete EVERY stored program (also the app text and the
        # black picture from 'blank' in the previous version, which the display showed as garbage)
        await link.send(packet(0x0102, (0x00FF).to_bytes(2, 'little') + bytes(range(1, 0xFF))), 2)
        print("all stored programs erased")
    elif a.cmd == 'reset':
        await link.send(packet(0x8003), 2)       # set_default_mode
        print("display back in its default mode")
    elif a.cmd == 'raw':
        await link.send(bytes.fromhex(a.msg), 1)
        print("reply:", link.reply.hex(" ") if link.reply else None)
    await link.client.disconnect()

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--addr', help='display address (scan)')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--host', default='127.0.0.1', help='0.0.0.0 to allow other computers (testing)')
    ap.add_argument('--fps', type=float, default=5, help='scroll speed (frames per second)')
    ap.add_argument('--bright', type=int, default=100, help='frame brightness 1-100')
    ap.add_argument('--chunk', type=int, default=0, help='BLE write size (0 = from the MTU)')
    ap.add_argument('--gap', type=float, default=0.003, help='pause between BLE writes (s)')
    ap.add_argument('--fast', action='store_true', help='write WITHOUT response (faster, but the display can lose packets = garbage)')
    ap.add_argument('--mode', default='diy', choices=['diy', 'frame'], help='diy = live pixels like the app (default), frame = one raw frame')
    ap.add_argument('--hold', type=float, default=3, help='seconds to keep the connection after a test')
    ap.add_argument('--color', default='ffffff')
    ap.add_argument('--size', default='big')
    ap.add_argument('--dmd', action='store_true', help='serve: also mirror the DMD from dmdext (network stream)')
    ap.add_argument('--dmd-port', type=int, default=8766, help='WebSocket port for dmdext (url = ws://127.0.0.1:PORT/dmd)')
    ap.add_argument('--dmd-fps', type=float, default=10, help='max DMD frames per second to the display')
    ap.add_argument('--dmd-scale', default='avg', choices=['avg', 'max'], help='avg = softer, max = bolder')
    ap.add_argument('--dmd-gain', type=float, default=None, help='brighten thin lines (default 1.5 avg, 1.0 max)')
    ap.add_argument('--dmd-levels', type=int, default=4, help='brightness steps incl. black (fewer = faster)')
    ap.add_argument('--dmd-color', default=None, help='DMD colour (default: the colour dmdext sends)')
    ap.add_argument('--dmd-pause', type=float, default=4, help='seconds the mirror waits after a table command')
    ap.add_argument('--out', default=None, help='dmdpreview: output file')
    ap.add_argument('cmd', choices=['scan', 'info', 'test', 'text', 'bench', 'erase', 'reset', 'raw', 'serve', 'dmdpreview'])
    ap.add_argument('msg', nargs='?', default='WUBBA LUBBA DUB DUB')
    a = ap.parse_args()
    if a.cmd == 'dmdpreview':
        out = dmd_preview(a.msg, a.dmd_scale, a.dmd_gain, a.dmd_levels, a.dmd_color, a.out)
        print("preview written to", out)
        return
    if a.cmd != 'scan' and not a.addr:
        ap.error('--addr is needed (use: scan)')
    if a.cmd == 'serve':
        link = Link(a.addr, a.chunk, a.gap, not a.fast)
        b = Bridge(link, a.fps, a.bright, a.mode)
        b.dmd_pause = a.dmd_pause
        if a.dmd:
            b.dmd = DmdMirror(b, a.host, a.dmd_port, a.dmd_fps, a.dmd_scale, a.dmd_gain, a.dmd_levels, a.dmd_color)
        threading.Thread(target=b.run, daemon=True).start()
        srv = ThreadingHTTPServer((a.host, a.port), make_handler(b))
        print("iPixel bridge on http://%s:%d  (Ctrl+C to stop)" % (a.host, a.port))
        b.submit(('frames', frames_for_text("RICK AND MORTY", (0, 255, 90), (0, 0, 0), 'big'), 1), http=False)
        srv.serve_forever()
    else:
        asyncio.run(cli(a))

if __name__ == '__main__':
    main()
