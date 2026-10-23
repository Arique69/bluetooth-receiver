"""Bluetooth audio receiver: iPhone -> laptop speakers (A2DP sink) + now playing / controls (Apple Media Service over BLE).
Needs Windows 10 2004+ and:
pip install winrt-runtime winrt-Windows.Media.Audio winrt-Windows.Devices.Enumeration winrt-Windows.Devices.Bluetooth
    winrt-Windows.Devices.Bluetooth.GenericAttributeProfile winrt-Windows.Storage.Streams winrt-Windows.Foundation
    winrt-Windows.Foundation.Collections pillow
"""
import asyncio
import colorsys
import io
import json
import os
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
import uuid
import zlib
from tkinter import font as tkfont

from PIL import Image, ImageDraw, ImageTk

from winrt.windows.devices.bluetooth import BluetoothCacheMode, BluetoothConnectionStatus, BluetoothLEDevice
from winrt.windows.devices.bluetooth.genericattributeprofile import (
    GattClientCharacteristicConfigurationDescriptorValue as Cccd,
    GattServiceProvider,
    GattServiceProviderAdvertisingParameters,
)
from winrt.windows.devices.enumeration import DeviceInformation
from winrt.windows.media.audio import AudioPlaybackConnection
from winrt.windows.storage.streams import DataWriter

# Apple Media Service: https://developer.apple.com/library/archive/documentation/CoreBluetooth/Reference/AppleMediaService_Reference/
AMS_SERVICE = uuid.UUID("89D3502B-0F36-433A-8EF4-C502AD55F8DC")
AMS_REMOTE_COMMAND = uuid.UUID("9B3C81D8-57B1-4A8A-B8DF-0E56F7CA51C2")
AMS_ENTITY_UPDATE = uuid.UUID("2F7CABCE-808D-411F-9A0C-BB92BA96C102")
AMS_ENTITY_ATTRIBUTE = uuid.UUID("C6B2F38C-23AB-46D8-A6AB-A3A870BBD5D7")
CMD_TOGGLE, CMD_NEXT, CMD_PREV = 2, 3, 4
PLAYER, TRACK = 0, 2  # entity ids
PLAYER_PLAYBACK_INFO = 1  # "state,rate,elapsed"; state 1 = playing
TRACK_ARTIST, TRACK_TITLE, TRACK_DURATION = 0, 2, 3
# Any UUID works; we only advertise so the iPhone opens a BLE connection to us.
ADVERT_SERVICE = uuid.UUID("7c3b1f2e-1d2a-4b8e-9a51-3e6f0c2d9b11")

BG, CARD, FG, MUTED, TRACKBAR, GREEN = "#121212", "#282828", "#ffffff", "#b3b3b3", "#4d4d4d", "#1db954"
FONT = "Segoe UI"
W = 360  # content width
COVERS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "covers")  # downloaded art, so repeats work offline

loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()


async def _await(aw):
    return await aw


def run(aw, done=None):
    fut = asyncio.run_coroutine_threadsafe(_await(aw), loop)  # WinRT ops are awaitable but not coroutines
    if done:
        fut.add_done_callback(lambda f: root.after(0, done, f))


def buf(data):
    w = DataWriter()
    w.write_bytes(bytes(data))
    return w.detach_buffer()


def fetch_cover(title, artist):
    """Album art bytes from the local cache or the iTunes Search API, None if not found / offline."""
    path = os.path.join(COVERS, f"{zlib.crc32(f'{title}|{artist}'.encode()):08x}.jpg")
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read()
    for term in (f"{title} {artist}", title):  # artist names often don't match, so retry with title only
        url = "https://itunes.apple.com/search?" + urllib.parse.urlencode({"term": term, "entity": "song", "limit": 1})
        try:
            results = json.load(urllib.request.urlopen(url, timeout=8))["results"]
            if not results:
                continue
            art = results[0]["artworkUrl100"].replace("100x100bb", "600x600bb")
            data = urllib.request.urlopen(art, timeout=8).read()
        except Exception:
            return None
        os.makedirs(COVERS, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return data
    return None


def smooth(w, h, draw, bg=BG, ss=4):
    """Draw at ss x size then downscale, since Tk canvas shapes aren't anti-aliased. draw(d, k) gets a scale fn."""
    img = Image.new("RGB", (w * ss, h * ss), bg)
    draw(ImageDraw.Draw(img), lambda *xy: [v * ss for v in xy])
    return ImageTk.PhotoImage(img.resize((w, h), Image.LANCZOS))


def skip_icon(color, forward):
    def draw(d, k):
        if forward:
            d.polygon(k(7, 7, 7, 25, 22, 16), fill=color)
            d.rounded_rectangle(k(23, 7, 26, 25), radius=k(1)[0], fill=color)
        else:
            d.polygon(k(25, 7, 25, 25, 10, 16), fill=color)
            d.rounded_rectangle(k(6, 7, 9, 25), radius=k(1)[0], fill=color)
    return smooth(32, 32, draw)


def play_icon(playing):
    def draw(d, k):
        d.ellipse(k(0, 0, 56, 56), fill=FG)
        if playing:
            d.rounded_rectangle(k(19, 17, 25, 39), radius=k(1.5)[0], fill="#000")
            d.rounded_rectangle(k(31, 17, 37, 39), radius=k(1.5)[0], fill="#000")
        else:
            d.polygon(k(22, 16, 22, 40, 41, 28), fill="#000")
    return smooth(56, 56, draw)


def mmss(sec):
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def clip(text, fnt, width):
    """Cut text with … so it fits in `width` pixels."""
    if fnt.measure(text) <= width:
        return text
    while text and fnt.measure(text + "…") > width:
        text = text[:-1]
    return text + "…"


class App:
    def __init__(self, root):
        self.root = root
        self.devices = []
        self.conn = None  # must keep a reference or Windows drops the audio link
        self.cmd_char = None  # AMS remote command characteristic, None until the iPhone connects over BLE
        self.track = {"title": "", "artist": "", "playing": False, "duration": 0.0, "elapsed": 0.0, "rate": 0.0, "at": 0.0}

        root.title("Bluetooth Receiver")
        root.configure(bg=BG, padx=24, pady=20)
        root.resizable(False, False)
        self.f_title = tkfont.Font(family=FONT, size=16, weight="bold")
        self.f_body = tkfont.Font(family=FONT, size=10)
        self.f_small = tkfont.Font(family=FONT, size=9)

        # top bar: device picker + connect pill
        top = tk.Frame(root, bg=BG)
        top.pack(fill="x")
        self.device_var = tk.StringVar(value="No devices")
        self.device_menu = tk.OptionMenu(top, self.device_var, "")
        self.device_menu.config(bg=CARD, fg=FG, activebackground=TRACKBAR, activeforeground=FG, relief="flat",
                                highlightthickness=0, bd=0, font=self.f_body, padx=12, pady=4, cursor="hand2",
                                indicatoron=False, textvariable="")
        self.device_var.trace_add("write", lambda *_: self.device_menu.config(text=f"{self.device_var.get()}  ▾"))
        self.device_menu["menu"].config(bg=CARD, fg=FG, activebackground=GREEN, bd=0)
        self.device_menu.pack(side="left")
        self.refresh_btn = self.text_button(top, "⟳", self.refresh, size=13)
        self.refresh_btn.pack(side="left", padx=6)
        root.bind("<FocusIn>", lambda e: e.widget is root and self.refresh())
        self.connect_btn = tk.Label(top, text="Connect", bg=GREEN, fg="#000", font=(FONT, 10, "bold"),
                                    padx=16, pady=4, cursor="hand2")
        self.connect_btn.bind("<Button-1>", lambda e: self.toggle_connect())
        self.connect_btn.pack(side="right")

        # cover: gradient square, hue picked from the song title
        self.cover = tk.Canvas(root, width=W, height=W, bg=CARD, highlightthickness=0)
        self.cover.pack(pady=(20, 18))
        self.cover_key = None
        self.cover_ready = None  # (key, jpeg bytes) handed over from the download thread

        self.title = tk.Label(root, text="Nothing playing", bg=BG, fg=FG, font=self.f_title, anchor="w")
        self.title.pack(fill="x")
        self.artist = tk.Label(root, text="", bg=BG, fg=MUTED, font=self.f_body, anchor="w")
        self.artist.pack(fill="x")

        # progress bar
        self.bar = tk.Canvas(root, width=W, height=14, bg=BG, highlightthickness=0)
        self.bar.pack(pady=(14, 0))
        times = tk.Frame(root, bg=BG)
        times.pack(fill="x")
        self.t_elapsed = tk.Label(times, text="0:00", bg=BG, fg=MUTED, font=self.f_small)
        self.t_elapsed.pack(side="left")
        self.t_total = tk.Label(times, text="0:00", bg=BG, fg=MUTED, font=self.f_small)
        self.t_total.pack(side="right")

        # controls
        ctrl = tk.Frame(root, bg=BG)
        ctrl.pack(pady=(6, 10))
        self.icons = {"play": play_icon(False), "pause": play_icon(True)}
        self.skip_button(ctrl, CMD_PREV).pack(side="left", padx=18)
        self.play = tk.Label(ctrl, image=self.icons["play"], bg=BG, bd=0, cursor="hand2")
        self.play.bind("<Button-1>", lambda e: self.media(CMD_TOGGLE))
        self.play.pack(side="left", padx=18)
        self.skip_button(ctrl, CMD_NEXT).pack(side="left", padx=18)

        self.status = tk.Label(root, text="", bg=BG, fg=MUTED, font=self.f_small, anchor="w")
        self.status.pack(fill="x")

        run(self.ams_loop())
        self.refresh()
        self.render()

    def text_button(self, parent, text, cmd, size):
        b = tk.Label(parent, text=text, bg=BG, fg=MUTED, font=(FONT, size), cursor="hand2")
        b.bind("<Button-1>", lambda e: cmd())
        b.bind("<Enter>", lambda e: b.config(fg=FG))
        b.bind("<Leave>", lambda e: b.config(fg=MUTED))
        return b

    def skip_button(self, parent, cmd):
        fwd = cmd == CMD_NEXT
        normal, hover = skip_icon(MUTED, fwd), skip_icon(FG, fwd)
        b = tk.Label(parent, image=normal, bg=BG, bd=0, cursor="hand2")
        b.images = (normal, hover)  # keep references or Tk drops the images
        b.bind("<Button-1>", lambda e: self.media(cmd))
        b.bind("<Enter>", lambda e: b.config(image=hover))
        b.bind("<Leave>", lambda e: b.config(image=normal))
        return b

    # --- audio connection (A2DP) ---
    def refresh(self):
        async def find():
            sel = AudioPlaybackConnection.get_device_selector()
            return list(await DeviceInformation.find_all_async_aqs_filter(sel))

        def done(f):
            self.devices = f.result()
            menu = self.device_menu["menu"]
            menu.delete(0, "end")
            for d in self.devices:
                menu.add_command(label=d.name, command=lambda n=d.name: self.device_var.set(n))
            menu.add_separator()
            # pairing a new phone: hand off to Windows' own Bluetooth page, list refreshes when we get focus back
            menu.add_command(label="+  Add device…", command=lambda: os.startfile("ms-settings:bluetooth"))
            if self.devices and self.device_var.get() not in [d.name for d in self.devices]:
                self.device_var.set(self.devices[0].name)

        run(find(), done)

    def toggle_connect(self):
        if self.conn:
            self.conn.close()
            self.conn = None
            self.set_connected(False)
            return
        dev = next((d for d in self.devices if d.name == self.device_var.get()), None)
        if not dev:
            return
        self.connect_btn.config(text="Connecting…")

        async def connect():
            conn = AudioPlaybackConnection.try_create_from_id(dev.id)
            if conn is None:
                raise RuntimeError("device doesn't support audio playback")
            await conn.start_async()
            result = await conn.open_async()
            if result.status != 0:  # 0 = Success
                conn.close()
                raise RuntimeError(f"open failed (status {result.status}, {result.extended_error})")
            return conn

        def done(f):
            try:
                self.conn = f.result()
                self.set_connected(True)
            except Exception as e:
                self.set_connected(False)
                self.status.config(text=f"Failed: {e}", fg="#f15e6c")

        run(connect(), done)

    def set_connected(self, on):
        self.status.config(text="", fg=MUTED)
        if on:
            self.connect_btn.config(text="Disconnect", bg=CARD, fg=FG)
        else:
            self.connect_btn.config(text="Connect", bg=GREEN, fg="#000")

    # --- now playing / controls (Apple Media Service over BLE) ---
    async def ams_loop(self):
        # Advertise so the bonded iPhone connects to us over BLE, then use AMS on that link.
        r = await GattServiceProvider.create_async(ADVERT_SERVICE)
        self.provider = r.service_provider  # keep alive, advertising stops if collected
        params = GattServiceProviderAdvertisingParameters()
        params.is_connectable = params.is_discoverable = True
        self.provider.start_advertising_with_parameters(params)

        sel = BluetoothLEDevice.get_device_selector_from_pairing_state(True)
        while True:
            if self.cmd_char is None:
                for info in await DeviceInformation.find_all_async_aqs_filter(sel):
                    dev = await BluetoothLEDevice.from_id_async(info.id)
                    if dev and dev.connection_status == BluetoothConnectionStatus.CONNECTED:
                        try:
                            await self.ams_setup(dev)
                            break
                        except Exception as e:
                            print("AMS setup failed:", e)
            await asyncio.sleep(2)

    async def ams_setup(self, dev):
        res = await dev.get_gatt_services_for_uuid_with_cache_mode_async(AMS_SERVICE, BluetoothCacheMode.UNCACHED)
        if res.status != 0 or not res.services.size:
            return
        svc = res.services[0]
        chars = {}
        for u in (AMS_REMOTE_COMMAND, AMS_ENTITY_UPDATE, AMS_ENTITY_ATTRIBUTE):
            cr = await svc.get_characteristics_for_uuid_async(u)
            chars[u] = cr.characteristics[0]
        update = chars[AMS_ENTITY_UPDATE]
        update.add_value_changed(self.on_entity_update)
        await update.write_client_characteristic_configuration_descriptor_async(Cccd.NOTIFY)
        await update.write_value_async(buf([PLAYER, PLAYER_PLAYBACK_INFO]))
        await update.write_value_async(buf([TRACK, TRACK_ARTIST, TRACK_TITLE, TRACK_DURATION]))
        # Updates only arrive on change, so read the current values once.
        attr = chars[AMS_ENTITY_ATTRIBUTE]
        for entity, a in ((PLAYER, PLAYER_PLAYBACK_INFO), (TRACK, TRACK_ARTIST), (TRACK, TRACK_TITLE), (TRACK, TRACK_DURATION)):
            await attr.write_value_async(buf([entity, a]))
            r = await attr.read_value_with_cache_mode_async(BluetoothCacheMode.UNCACHED)
            self.apply(entity, a, bytes(r.value).decode("utf-8", "replace"))

        def on_status(d, _):
            if d.connection_status != BluetoothConnectionStatus.CONNECTED:
                self.cmd_char = None

        dev.add_connection_status_changed(on_status)
        self._keep = (dev, svc, update)  # WinRT objects must stay referenced or notifications stop
        self.cmd_char = chars[AMS_REMOTE_COMMAND]

    def on_entity_update(self, _, args):
        data = bytes(args.characteristic_value)
        self.apply(data[0], data[1], data[3:].decode("utf-8", "replace"))

    def apply(self, entity, attr, value):
        t = self.track
        if entity == TRACK and attr == TRACK_TITLE:
            t["title"] = value
        elif entity == TRACK and attr == TRACK_ARTIST:
            t["artist"] = value
        elif entity == TRACK and attr == TRACK_DURATION:
            t["duration"] = float(value or 0)
        elif entity == PLAYER and attr == PLAYER_PLAYBACK_INFO:
            state, rate, elapsed = (value.split(",") + ["0", "0", "0"])[:3]
            t["playing"] = state == "1"
            t["rate"] = float(rate or 0)
            t["elapsed"] = float(elapsed or 0)
            t["at"] = time.monotonic()

    def media(self, cmd):
        if self.cmd_char:
            run(self.cmd_char.write_value_async(buf([cmd])))

    # --- drawing ---
    def draw_cover(self, key):
        c = self.cover
        c.delete("all")
        hue = (zlib.crc32(key.encode()) % 360) / 360
        steps = 60
        for i in range(steps):  # vertical gradient: song colour -> dark
            r, g, b = colorsys.hsv_to_rgb(hue, 0.65, 0.75 - 0.55 * i / steps)
            y = W * i / steps
            c.create_rectangle(0, y, W, y + W / steps + 1, fill=f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}", width=0)
        c.create_text(W / 2, W / 2, text="♪", fill="#ffffff", font=(FONT, 110))

    def render(self):
        t = self.track
        key = f"{t['title']}|{t['artist']}"
        if key != self.cover_key:
            self.cover_key = key
            self.draw_cover(key)
            if t["title"]:
                threading.Thread(target=lambda k=key, ti=t["title"], ar=t["artist"]:
                                 setattr(self, "cover_ready", (k, fetch_cover(ti, ar))), daemon=True).start()
            self.title.config(text=clip(t["title"] or "Nothing playing", self.f_title, W))
            self.artist.config(text=clip(t["artist"], self.f_body, W))
        if self.cover_ready:
            k, data = self.cover_ready
            self.cover_ready = None
            if k == key and data:
                self.cover_img = ImageTk.PhotoImage(Image.open(io.BytesIO(data)).resize((W, W), Image.LANCZOS))
                self.cover.delete("all")
                self.cover.create_image(0, 0, image=self.cover_img, anchor="nw")
        self.play.config(image=self.icons["pause" if t["playing"] else "play"])

        # iPhone only reports elapsed at state changes; extrapolate with playback rate between them
        elapsed = t["elapsed"] + (time.monotonic() - t["at"]) * t["rate"] if t["playing"] else t["elapsed"]
        dur = t["duration"]
        elapsed = min(elapsed, dur) if dur else 0
        x = W * elapsed / dur if dur else 0
        b = self.bar
        def draw_bar(d, k):
            d.rounded_rectangle(k(0, 5, W, 9), radius=k(2)[0], fill=TRACKBAR)
            if x >= 4:
                d.rounded_rectangle(k(0, 5, x, 9), radius=k(2)[0], fill=FG)
        self.bar_img = smooth(W, 14, draw_bar)
        b.delete("all")
        b.create_image(0, 0, image=self.bar_img, anchor="nw")
        self.t_elapsed.config(text=mmss(elapsed))
        self.t_total.config(text=mmss(dur))

        self.root.after(250, self.render)


root = tk.Tk()
App(root)
root.update()
try:  # dark title bar (Windows 11 / 10 20H1+)
    import ctypes
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)
except Exception:
    pass
root.mainloop()
