import base64
import io
import os
import subprocess
import time
from typing import Optional, List, Dict, Any

# Automatyczne wykrywanie sesji Wayland i D-Bus
_uid = os.getuid()
_runtime_dir = f"/run/user/{_uid}"
if not os.environ.get("XDG_RUNTIME_DIR") or "$" in os.environ.get("XDG_RUNTIME_DIR", ""):
    os.environ["XDG_RUNTIME_DIR"] = _runtime_dir
if not os.environ.get("DBUS_SESSION_BUS_ADDRESS") or "$" in os.environ.get("DBUS_SESSION_BUS_ADDRESS", ""):
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={_runtime_dir}/bus"
if not os.environ.get("WAYLAND_DISPLAY") or "$" in os.environ.get("WAYLAND_DISPLAY", ""):
    _wl = "wayland-0"
    if os.path.exists(_runtime_dir):
        for _f in sorted(os.listdir(_runtime_dir)):
            if _f.startswith("wayland-") and not _f.endswith(".lock"):
                _wl = _f
                break
    os.environ["WAYLAND_DISPLAY"] = _wl
if not os.environ.get("DISPLAY") or "$" in os.environ.get("DISPLAY", ""):
    os.environ["DISPLAY"] = ":0"

import numpy as np
from PIL import Image, ImageDraw
from evdev import UInput, AbsInfo, ecodes as e
from jeepney import DBusAddress, new_method_call
from jeepney.io.blocking import open_dbus_connection
from jeepney.bus_messages import message_bus
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent, ImageContent

mcp = FastMCP("computer-use")

TMP_SCREENSHOT = f"/run/user/{_uid}/mcp_wayland_screen.png"

DEFAULT_DIFF_RADIUS = 65
MAX_CHANGED_PIXEL_RATIO = 0.08
PIXEL_CHANGE_THRESHOLD = 25.0


class ServerState:
    def __init__(self):
        self.last_raw_screenshot: Optional[Image.Image] = None
        self.staged_action: Optional[Dict[str, Any]] = None
        self.screen_w: int = 1920
        self.screen_h: int = 1080
        self.last_x: int = 500
        self.last_y: int = 500
        self.uinput_mouse: Optional[UInput] = None
        self.uinput_kbd: Optional[UInput] = None
        self.dbus_conn = None

state = ServerState()


# =====================================================================
# WARSTWA WAYLAND: CICHY ZRZUT EKRANU PRZEZ D-BUS (ZERO OKIENEK I BŁYSKU)
# =====================================================================

def take_raw_screenshot() -> Image.Image:
    """
    Robi całkowicie cichy zrzut ekranu w GNOME 50 (bez okienka zatwierdzania i bez błysku),
    przejmując na szynie D-Bus nazwę z białej listy kompozytora (org.freedesktop.impl.portal.desktop.gnome)
    i wywołując bezpośrednio org.gnome.Shell.Screenshot (flash=False).
    """
    from jeepney import MessageType

    if os.path.exists(TMP_SCREENSHOT):
        try:
            os.remove(TMP_SCREENSHOT)
        except OSError:
            pass

    try:
        if state.dbus_conn is None:
            state.dbus_conn = open_dbus_connection(bus="SESSION")

        # Przejmujemy nazwę z białej listy GNOME 50 (0x7 = ALLOW_REPLACEMENT | REPLACE_EXISTING | DO_NOT_QUEUE)
        allowed_name = "org.freedesktop.impl.portal.desktop.gnome"
        req = message_bus.RequestName(allowed_name, 0x7)
        rep = state.dbus_conn.send_and_get_reply(req, timeout=2.0)
        owner_status = rep.body[0] if (rep and rep.body) else 0

        # 1 = PRIMARY_OWNER, 4 = ALREADY_OWNER
        if owner_status not in (1, 4):
            subprocess.run(
                ["pkill", "-9", "-f", "xdg-desktop-portal-gnome"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            rep = state.dbus_conn.send_and_get_reply(req, timeout=2.0)

        addr = DBusAddress(
            "/org/gnome/Shell/Screenshot",
            bus_name="org.gnome.Shell.Screenshot",
            interface="org.gnome.Shell.Screenshot"
        )
        # Screenshot(include_cursor=False, flash=False, filename=TMP_SCREENSHOT)
        msg = new_method_call(addr, "Screenshot", "bbs", (False, False, TMP_SCREENSHOT))
        reply = state.dbus_conn.send_and_get_reply(msg, timeout=3.0)

        if reply.header.message_type == MessageType.error:
            raise RuntimeError(f"D-Bus Error: {reply.body}")

        if not os.path.exists(TMP_SCREENSHOT):
            raise RuntimeError(f"Brak pliku zrzutu: {reply.body}")

        with Image.open(TMP_SCREENSHOT) as im:
            img = im.convert("RGB")

        state.screen_w, state.screen_h = img.size
        state.last_raw_screenshot = img.copy()
        return img

    except Exception as ex:
        if state.dbus_conn is not None:
            try:
                state.dbus_conn.close()
            except Exception:
                pass
            state.dbus_conn = None
        raise RuntimeError(f"Błąd bezpośredniego zrzutu D-Bus: {ex}")


def ensure_uinput():
    """Inicjalizuje wirtualną mysz (absolutną) i klawiaturę na poziomie jądra."""
    if state.uinput_mouse is not None and state.uinput_kbd is not None:
        return

    if state.last_raw_screenshot is None:
        take_raw_screenshot()

    mouse_cap = {
        e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE],
        e.EV_ABS: [
            (e.ABS_X, AbsInfo(value=0, min=0, max=state.screen_w, fuzz=0, flat=0, resolution=1)),
            (e.ABS_Y, AbsInfo(value=0, min=0, max=state.screen_h, fuzz=0, flat=0, resolution=1)),
        ],
    }
    state.uinput_mouse = UInput(mouse_cap, name="mcp-virtual-pointer")

    kbd_keys = [
        code for name, code in e.ecodes.items()
        if name.startswith("KEY_") and isinstance(code, int) and code < e.KEY_MAX
    ]
    kbd_cap = {e.EV_KEY: kbd_keys}
    state.uinput_kbd = UInput(kbd_cap, name="mcp-virtual-keyboard")

    time.sleep(0.3)


def human_move_to(target_x: int, target_y: int, duration: float = 0.35):
    ensure_uinput()
    target_x = max(0, min(state.screen_w - 1, int(target_x)))
    target_y = max(0, min(state.screen_h - 1, int(target_y)))

    steps = max(15, int(duration * 60))
    start_x, start_y = state.last_x, state.last_y

    for i in range(1, steps + 1):
        t = i / steps
        t_smooth = t * t * (3 - 2 * t)
        cur_x = int(start_x + (target_x - start_x) * t_smooth)
        cur_y = int(start_y + (target_y - start_y) * t_smooth)

        state.uinput_mouse.write(e.EV_ABS, e.ABS_X, cur_x)
        state.uinput_mouse.write(e.EV_ABS, e.ABS_Y, cur_y)
        state.uinput_mouse.syn()
        time.sleep(duration / steps)

    state.last_x, state.last_y = target_x, target_y
    time.sleep(0.08)


def execute_physical_click(x: int, y: int, button: str = "left", double: bool = False):
    human_move_to(x, y, duration=0.3)
    btn_map = {"left": e.BTN_LEFT, "right": e.BTN_RIGHT, "middle": e.BTN_MIDDLE}
    btn_code = btn_map.get(button.lower(), e.BTN_LEFT)

    clicks = 2 if double else 1
    for _ in range(clicks):
        state.uinput_mouse.write(e.EV_KEY, btn_code, 1)
        state.uinput_mouse.syn()
        time.sleep(0.05)
        state.uinput_mouse.write(e.EV_KEY, btn_code, 0)
        state.uinput_mouse.syn()
        if double:
            time.sleep(0.07)

    time.sleep(0.5)


def execute_physical_drag(start_x: int, start_y: int, end_x: int, end_y: int, button: str = "left"):
    btn_map = {"left": e.BTN_LEFT, "right": e.BTN_RIGHT, "middle": e.BTN_MIDDLE}
    btn_code = btn_map.get(button.lower(), e.BTN_LEFT)

    human_move_to(start_x, start_y, duration=0.25)
    time.sleep(0.15)

    state.uinput_mouse.write(e.EV_KEY, btn_code, 1)
    state.uinput_mouse.syn()
    time.sleep(0.2)

    human_move_to(end_x, end_y, duration=0.65)
    time.sleep(0.2)

    state.uinput_mouse.write(e.EV_KEY, btn_code, 0)
    state.uinput_mouse.syn()
    time.sleep(0.5)


# =====================================================================
# WIZUALIZACJA I WERYFIKACJA ZMIAN OBRAZU (DIFF CHECK)
# =====================================================================

def check_area_changed(
    old_img: Image.Image,
    new_img: Image.Image,
    x: int,
    y: int,
    radius: int = DEFAULT_DIFF_RADIUS
) -> tuple[bool, float]:
    w, h = old_img.size
    left = max(0, x - radius)
    top = max(0, y - radius)
    right = min(w, x + radius)
    bottom = min(h, y + radius)

    crop_old = np.asarray(old_img.crop((left, top, right, bottom)), dtype=np.float32)
    crop_new = np.asarray(new_img.crop((left, top, right, bottom)), dtype=np.float32)

    diff = np.max(np.abs(crop_old - crop_new), axis=2)
    changed_pixels_ratio = float(np.mean(diff > PIXEL_CHANGE_THRESHOLD))

    is_changed = changed_pixels_ratio > MAX_CHANGED_PIXEL_RATIO
    return is_changed, changed_pixels_ratio


def draw_rulers(draw: ImageDraw.ImageDraw, w: int, h: int):
    for x in range(100, w, 100):
        color = (255, 255, 0) if x % 500 == 0 else (180, 180, 180)
        length = 12 if x % 500 == 0 else 6
        draw.line([(x, 0), (x, length)], fill=color, width=2)
        if x % 200 == 0:
            draw.rectangle([x - 18, length + 1, x + 22, length + 14], fill=(0, 0, 0))
            draw.text((x - 15, length + 2), str(x), fill=color)

    for y in range(100, h, 100):
        color = (255, 255, 0) if y % 500 == 0 else (180, 180, 180)
        length = 12 if y % 500 == 0 else 6
        draw.line([(0, y), (length, y)], fill=color, width=2)
        if y % 200 == 0:
            draw.rectangle([length + 1, y - 7, length + 38, y + 7], fill=(0, 0, 0))
            draw.text((length + 3, y - 6), str(y), fill=color)


def draw_point_overlay(
    img: Image.Image,
    points: List[Dict[str, Any]],
    draw_arrow: bool = False,
    radius: int = DEFAULT_DIFF_RADIUS,
    alert_mode: bool = False
) -> Image.Image:
    out = img.copy()
    draw = ImageDraw.Draw(out)
    w, h = out.size

    draw_rulers(draw, w, h)

    if draw_arrow and len(points) >= 2:
        p1, p2 = points[0], points[1]
        line_color = (255, 50, 50) if alert_mode else (0, 200, 255)
        draw.line([(p1["x"], p1["y"]), (p2["x"], p2["y"])], fill=line_color, width=4)

    for idx, pt in enumerate(points):
        x, y = int(pt["x"]), int(pt["y"])
        label = pt.get("label", f"P{idx+1}")
        changed = pt.get("changed", False) or alert_mode

        main_color = (255, 30, 30) if changed else (0, 255, 100)
        box_color = (255, 0, 0) if changed else (255, 200, 0)

        draw.rectangle(
            [x - radius, y - radius, x + radius, y + radius],
            outline=box_color,
            width=2
        )
        draw.ellipse([x - 22, y - 22, x + 22, y + 22], outline=(0, 0, 0), width=4)
        draw.ellipse([x - 22, y - 22, x + 22, y + 22], outline=main_color, width=2)
        draw.line([(x - 32, y), (x + 32, y)], fill=main_color, width=2)
        draw.line([(x, y - 32), (x, y + 32)], fill=main_color, width=2)
        draw.ellipse([x - 3, y - 3, x + 3, y + 3], fill=(255, 0, 255))

        text = f"{label}: ({x}, {y})"
        if changed:
            text += " [ZMIANA OBRAZU!]"
        tx = min(w - 190, max(10, x + 18))
        ty = min(h - 30, max(10, y - 32))
        draw.rectangle([tx - 4, ty - 2, tx + len(text) * 7 + 6, ty + 15], fill=(0, 0, 0))
        draw.text((tx, ty), text, fill=main_color)

    return out


def image_to_mcp_content(img: Image.Image) -> ImageContent:
    """Szybka kompresja JPEG (rozdzielczość 1:1, rozmiar ~200KB zamiast 4MB, czas ~15ms)."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=85)
    b64_data = base64.b64encode(buf.getvalue()).decode("utf-8")
    return ImageContent(type="image", data=b64_data, mimeType="image/jpeg")


# =====================================================================
# OBSŁUGA KLAWIATURY
# =====================================================================

KEY_ALIASES = {
    "ctrl": e.KEY_LEFTCTRL, "control": e.KEY_LEFTCTRL,
    "shift": e.KEY_LEFTSHIFT,
    "alt": e.KEY_LEFTALT,
    "rightalt": e.KEY_RIGHTALT, "altgr": e.KEY_RIGHTALT,
    "super": e.KEY_LEFTMETA, "win": e.KEY_LEFTMETA, "meta": e.KEY_LEFTMETA,
    "enter": e.KEY_ENTER, "return": e.KEY_ENTER,
    "esc": e.KEY_ESC, "escape": e.KEY_ESC,
    "tab": e.KEY_TAB,
    "space": e.KEY_SPACE,
    "backspace": e.KEY_BACKSPACE,
    "delete": e.KEY_DELETE, "del": e.KEY_DELETE,
    "up": e.KEY_UP, "down": e.KEY_DOWN, "left": e.KEY_LEFT, "right": e.KEY_RIGHT,
    "home": e.KEY_HOME, "end": e.KEY_END,
    "pageup": e.KEY_PAGEUP, "pgup": e.KEY_PAGEUP,
    "pagedown": e.KEY_PAGEDOWN, "pgdn": e.KEY_PAGEDOWN,
    "minus": e.KEY_MINUS, "equal": e.KEY_EQUAL,
    "slash": e.KEY_SLASH, "backslash": e.KEY_BACKSLASH,
    "dot": e.KEY_DOT, "comma": e.KEY_COMMA,
}

for c in "abcdefghijklmnopqrstuvwxyz0123456789":
    KEY_ALIASES[c] = getattr(e, f"KEY_{c.upper()}")
for i in range(1, 13):
    KEY_ALIASES[f"f{i}"] = getattr(e, f"KEY_F{i}")

SHIFT_CHARS = {
    "!": e.KEY_1, "@": e.KEY_2, "#": e.KEY_3, "$": e.KEY_4, "%": e.KEY_5,
    "^": e.KEY_6, "&": e.KEY_7, "*": e.KEY_8, "(": e.KEY_9, ")": e.KEY_0,
    "_": e.KEY_MINUS, "+": e.KEY_EQUAL, "{": e.KEY_LEFTBRACE, "}": e.KEY_RIGHTBRACE,
    "|": e.KEY_BACKSLASH, ":": e.KEY_SEMICOLON, '"': e.KEY_APOSTROPHE,
    "<": e.KEY_COMMA, ">": e.KEY_DOT, "?": e.KEY_SLASH, "~": e.KEY_GRAVE
}

NORMAL_CHARS = {
    "-": e.KEY_MINUS, "=": e.KEY_EQUAL, "[": e.KEY_LEFTBRACE, "]": e.KEY_RIGHTBRACE,
    "\\": e.KEY_BACKSLASH, ";": e.KEY_SEMICOLON, "'": e.KEY_APOSTROPHE,
    ",": e.KEY_COMMA, ".": e.KEY_DOT, "/": e.KEY_SLASH, "`": e.KEY_GRAVE,
    " ": e.KEY_SPACE, "\n": e.KEY_ENTER, "\t": e.KEY_TAB
}


def execute_shortcut(shortcut_str: str):
    ensure_uinput()
    parts = [p.strip().lower() for p in shortcut_str.split("+") if p.strip()]
    codes = []
    for p in parts:
        if p not in KEY_ALIASES:
            raise ValueError(f"Nieznany klawisz w skrócie: '{p}'")
        codes.append(KEY_ALIASES[p])

    for code in codes:
        state.uinput_kbd.write(e.EV_KEY, code, 1)
        state.uinput_kbd.syn()
        time.sleep(0.04)

    time.sleep(0.06)

    for code in reversed(codes):
        state.uinput_kbd.write(e.EV_KEY, code, 0)
        state.uinput_kbd.syn()
        time.sleep(0.04)

    time.sleep(0.25)


def execute_typing(text: str):
    ensure_uinput()
    for ch in text:
        if ch.isascii() and (ch.isalnum() or ch in SHIFT_CHARS or ch in NORMAL_CHARS):
            need_shift = ch.isupper() or (ch in SHIFT_CHARS)
            if ch in SHIFT_CHARS:
                key_code = SHIFT_CHARS[ch]
            elif ch in NORMAL_CHARS:
                key_code = NORMAL_CHARS[ch]
            else:
                key_code = KEY_ALIASES[ch.lower()]

            if need_shift:
                state.uinput_kbd.write(e.EV_KEY, e.KEY_LEFTSHIFT, 1)
                state.uinput_kbd.syn()
                time.sleep(0.02)

            state.uinput_kbd.write(e.EV_KEY, key_code, 1)
            state.uinput_kbd.syn()
            time.sleep(0.025)
            state.uinput_kbd.write(e.EV_KEY, key_code, 0)
            state.uinput_kbd.syn()

            if need_shift:
                time.sleep(0.02)
                state.uinput_kbd.write(e.EV_KEY, e.KEY_LEFTSHIFT, 0)
                state.uinput_kbd.syn()

            time.sleep(0.03)
        else:
            subprocess.run(
                ["wl-copy", "--", ch],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                check=True
            )
            time.sleep(0.05)
            execute_shortcut("ctrl+v")
            time.sleep(0.05)


# =====================================================================
# NARZĘDZIA MCP (TOOLS)
# =====================================================================

@mcp.tool()
def screenshot() -> list:
    """Robi aktualny zrzut ekranu z linijką koordynatów."""
    raw = take_raw_screenshot()
    vis = raw.copy()
    draw_rulers(ImageDraw.Draw(vis), state.screen_w, state.screen_h)
    return [
        TextContent(
            type="text",
            text=f"Aktualny zrzut ekranu. Rozdzielczość: {state.screen_w}x{state.screen_h}."
        ),
        image_to_mcp_content(vis)
    ]


@mcp.tool()
def visualize_points(points: List[Dict[str, Any]]) -> list:
    """
    Wizualizuje podane punkty na aktualnym ekranie bez wykonywania kliknięcia.
    Format `points`: [{"x": 250, "y": 400, "label": "Cel1"}]
    """
    raw = take_raw_screenshot()
    vis = draw_point_overlay(raw, points, draw_arrow=False)
    return [
        TextContent(
            type="text",
            text=f"Zwizualizowano {len(points)} punktów na ekranie ({state.screen_w}x{state.screen_h})."
        ),
        image_to_mcp_content(vis)
    ]


@mcp.tool()
def click(
    x: int,
    y: int,
    button: str = "left",
    double: bool = False,
    diff_radius: int = DEFAULT_DIFF_RADIUS
) -> list:
    """
    ETAP 1 KLIKNIĘCIA: Nie klika od razu! Zwraca screenshot z nałożonym punktem (x, y)
    i czeka na wywołanie `approve_click`.
    """
    raw = take_raw_screenshot()
    state.staged_action = {
        "type": "click",
        "x": x,
        "y": y,
        "button": button,
        "double": double,
        "radius": diff_radius,
        "reference_image": raw
    }

    vis = draw_point_overlay(
        raw,
        [{"x": x, "y": y, "label": f"CLICK ({button}{', 2x' if double else ''})"}],
        radius=diff_radius
    )

    return [
        TextContent(
            type="text",
            text=(
                f"[WIZUALIZACJA KLIKNIĘCIA] Punkt: ({x}, {y}), przycisk: {button}, double: {double}.\n"
                f"Sprawdź na obrazie, czy celownik trafia dokładnie w element.\n"
                f"-> Jeśli TAK: wywołaj `approve_click`.\n"
                f"-> Jeśli NIE: wywołaj ponownie `click` ze skorygowanymi koordynatami."
            )
        ),
        image_to_mcp_content(vis)
    ]


@mcp.tool()
def approve_click() -> list:
    """
    ETAP 2 KLIKNIĘCIA: Sprawdza czy obszar wokół punktu się nie zmienił.
    Jeśli się zmienił -> DENY + nowy screenshot z zaznaczonym punktem. Jeśli OK -> wykonuje klik.
    """
    if not state.staged_action or state.staged_action.get("type") != "click":
        return [TextContent(type="text", text="BŁĄD: Najpierw wywołaj narzędzie `click(x, y)`.")]

    action = state.staged_action
    x, y = action["x"], action["y"]
    radius = action["radius"]
    ref_img = action["reference_image"]

    current_raw = take_raw_screenshot()
    changed, ratio = check_area_changed(ref_img, current_raw, x, y, radius=radius)

    if changed:
        state.staged_action = None
        vis_denied = draw_point_overlay(
            current_raw,
            [{"x": x, "y": y, "label": "ODRZUCONY CLICK", "changed": True}],
            radius=radius,
            alert_mode=True
        )
        return [
            TextContent(
                type="text",
                text=(
                    f"❌ [APPROVE_CLICK DENIED] Obszar wokół ({x}, {y}) w promieniu {radius}px "
                    f"zmienił się znacząco ({ratio*100:.1f}% zmienionych pikseli)!\n"
                    f"Kliknięcie zostało ZABLOKOWANE. Poniżej aktualny ekran z niedoszłym punktem ({x}, {y}):"
                )
            ),
            image_to_mcp_content(vis_denied)
        ]

    state.staged_action = None
    execute_physical_click(x, y, button=action["button"], double=action["double"])

    after_raw = take_raw_screenshot()
    vis_after = after_raw.copy()
    draw_rulers(ImageDraw.Draw(vis_after), state.screen_w, state.screen_h)

    return [
        TextContent(
            type="text",
            text=f"✅ Kliknięcie w ({x}, {y}) wykonane. Aktualny ekran po kliknięciu:"
        ),
        image_to_mcp_content(vis_after)
    ]


@mcp.tool()
def drag(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    button: str = "left",
    diff_radius: int = DEFAULT_DIFF_RADIUS
) -> list:
    """
    ETAP 1 PRZECIĄGANIA: Wizualizuje punkty START i END na obecnym ekranie.
    Aby wykonać ruch, wywołaj następnie `approve_drag`.
    """
    raw = take_raw_screenshot()
    state.staged_action = {
        "type": "drag",
        "start_x": start_x,
        "start_y": start_y,
        "end_x": end_x,
        "end_y": end_y,
        "button": button,
        "radius": diff_radius,
        "reference_image": raw
    }

    vis = draw_point_overlay(
        raw,
        [
            {"x": start_x, "y": start_y, "label": "DRAG START"},
            {"x": end_x, "y": end_y, "label": "DRAG END"}
        ],
        draw_arrow=True,
        radius=diff_radius
    )

    return [
        TextContent(
            type="text",
            text=(
                f"[WIZUALIZACJA DRAG] Start: ({start_x}, {start_y}) -> Koniec: ({end_x}, {end_y}).\n"
                f"-> Jeśli punkty są poprawne: wywołaj `approve_drag`.\n"
                f"-> Jeśli wymagają korekty: wywołaj ponownie `drag`."
            )
        ),
        image_to_mcp_content(vis)
    ]


@mcp.tool()
def approve_drag() -> list:
    """
    ETAP 2 PRZECIĄGANIA: Weryfikuje czy obraz wokół START i END się nie zmienił i wykonuje przeciąganie.
    """
    if not state.staged_action or state.staged_action.get("type") != "drag":
        return [TextContent(type="text", text="BŁĄD: Najpierw wywołaj narzędzie `drag`.")]

    action = state.staged_action
    sx, sy = action["start_x"], action["start_y"]
    ex, ey = action["end_x"], action["end_y"]
    radius = action["radius"]
    ref_img = action["reference_image"]

    current_raw = take_raw_screenshot()
    start_changed, start_ratio = check_area_changed(ref_img, current_raw, sx, sy, radius=radius)
    end_changed, end_ratio = check_area_changed(ref_img, current_raw, ex, ey, radius=radius)

    if start_changed or end_changed:
        state.staged_action = None
        vis_denied = draw_point_overlay(
            current_raw,
            [
                {"x": sx, "y": sy, "label": "START", "changed": start_changed},
                {"x": ex, "y": ey, "label": "END", "changed": end_changed}
            ],
            draw_arrow=True,
            radius=radius,
            alert_mode=True
        )
        return [
            TextContent(
                type="text",
                text=(
                    f"❌ [APPROVE_DRAG DENIED] Wykryto zmianę obrazu wokół punktów!\n"
                    f"- START ({sx}, {sy}): {start_ratio*100:.1f}% zmian\n"
                    f"- END ({ex}, {ey}): {end_ratio*100:.1f}% zmian\n"
                    f"Poniżej aktualny ekran z punktami, które miały zostać obsłużone:"
                )
            ),
            image_to_mcp_content(vis_denied)
        ]

    state.staged_action = None
    execute_physical_drag(sx, sy, ex, ey, button=action["button"])

    after_raw = take_raw_screenshot()
    vis_after = after_raw.copy()
    draw_rulers(ImageDraw.Draw(vis_after), state.screen_w, state.screen_h)

    return [
        TextContent(
            type="text",
            text=f"✅ Przeciągnięcie ({sx}, {sy}) -> ({ex}, {ey}) wykonane. Aktualny ekran:"
        ),
        image_to_mcp_content(vis_after)
    ]


@mcp.tool()
def type(
    text: Optional[str] = None,
    shortcut: Optional[str] = None,
    press_enter: bool = False,
    x: Optional[int] = None,
    y: Optional[int] = None,
    diff_radius: int = DEFAULT_DIFF_RADIUS
) -> list:
    """
    Wpisuje tekst lub wykonuje skrót klawiszowy (np. 'ctrl+c', 'alt+tab', 'super'), z opcjonalnym klikiem w (x, y).
    """
    if text is None and shortcut is None:
        return [TextContent(type="text", text="BŁĄD: Podaj `text` lub `shortcut`.")]

    if x is not None and y is not None:
        ref_img = state.last_raw_screenshot
        current_raw = take_raw_screenshot()
        if ref_img is not None:
            changed, ratio = check_area_changed(ref_img, current_raw, x, y, radius=diff_radius)
            if changed:
                vis_denied = draw_point_overlay(
                    current_raw,
                    [{"x": x, "y": y, "label": "ODRZUCONY TYPE+CLICK", "changed": True}],
                    radius=diff_radius,
                    alert_mode=True
                )
                return [
                    TextContent(
                        type="text",
                        text=(
                            f"❌ [TYPE CLICK DENIED] Obszar wokół ({x}, {y}) zmienił się ({ratio*100:.1f}%) "
                            f"od ostatniego zrzutu ekranu! Kliknięcie i pisanie zablokowane."
                        )
                    ),
                    image_to_mcp_content(vis_denied)
                ]

        execute_physical_click(x, y, button="left", double=False)
        time.sleep(0.2)

    actions_done = []
    if text:
        execute_typing(text)
        actions_done.append(f"wpisano tekst ({len(text)} zn.)")
    if shortcut:
        execute_shortcut(shortcut)
        actions_done.append(f"skrót '{shortcut}'")
    if press_enter:
        execute_shortcut("enter")
        actions_done.append("klawisz Enter")

    time.sleep(0.4)
    after_raw = take_raw_screenshot()
    vis_after = after_raw.copy()
    draw_rulers(ImageDraw.Draw(vis_after), state.screen_w, state.screen_h)

    return [
        TextContent(
            type="text",
            text=f"✅ Wykonano: {', '.join(actions_done)}. Aktualny ekran:"
        ),
        image_to_mcp_content(vis_after)
    ]


if __name__ == "__main__":
    mcp.run()
