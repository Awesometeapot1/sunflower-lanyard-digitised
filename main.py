from machine import Pin, SPI
import time

from ili9486 import ILI9486
from xpt2046 import XPT2046
from touch_cal import CAL
from ui import Button

# ----------------------------
# MIC (analog) quiet detector
# ----------------------------
# Put mic_level.py on your Pico filesystem.
# Mic wiring: OUT -> GP26 (ADC0), VCC -> 3V3, GND -> GND
MIC_ENABLED = True
MIC_ADC_PIN = 26  # GP26 / ADC0

# Tune these for your mic + lanyard placement
MIC_SAMPLE_COUNT   = 180
MIC_SAMPLE_US      = 200
MIC_EMA_ALPHA      = 0.15
MIC_QUIET_THRESH   = 0.4   # raise if it never says quiet; lower if always quiet
MIC_HYSTERESIS     = 0.003
MIC_QUIET_HOLD_MS  = 1500

MIC_POLL_MS        = 220     # how often to refresh mic state
MIC_DRAW_THROTTLE  = 250     # min ms between badge redraws

try:
    if MIC_ENABLED:
        from mic_level import MicLevel
        mic = MicLevel(
            adc_pin=MIC_ADC_PIN,
            sample_count=MIC_SAMPLE_COUNT,
            sample_us=MIC_SAMPLE_US,
            ema_alpha=MIC_EMA_ALPHA,
            quiet_threshold=MIC_QUIET_THRESH,
            hysteresis=MIC_HYSTERESIS,
            quiet_hold_ms=MIC_QUIET_HOLD_MS
        )
    else:
        mic = None
except Exception as e:
    print("Mic init failed:", e)
    mic = None

mic_quiet = False
mic_rms = 0.0
_last_mic_poll = 0
_last_badge_draw = 0
_last_badge_state = None

# ============================================================
# Hardware + display config
# ============================================================
SPI_ID = 0
SCK, MOSI, MISO = 18, 19, 16
LCD_CS, LCD_DC, LCD_RST, LCD_BL = 17, 20, 21, 22
TP_CS, TP_IRQ = 15, 14

MADCTL = 0x48
BGR = True
X_OFFSET = 0
Y_OFFSET = 0

# --- colours (RGB565) ---
BLACK = 0x0000
WHITE = 0xFFFF
GREY  = 0x7BEF
DARK  = 0x39E7
YELL  = 0xFFE0
RED   = 0xF800
GREEN = 0x07E0
BLUE  = 0x001F
CYAN  = 0x07FF
MAG   = 0xF81F
ORNG  = 0xFD20

W = CAL.get("W", 480)
H = CAL.get("H", 320)

Pin(LCD_BL, Pin.OUT).value(1)

spi = SPI(SPI_ID, baudrate=2_000_000, polarity=0, phase=0,
          sck=Pin(SCK), mosi=Pin(MOSI), miso=Pin(MISO))

lcd = ILI9486(
    spi,
    cs=LCD_CS,
    dc=LCD_DC,
    rst=LCD_RST,
    width=W,
    height=H,
    madctl=MADCTL,
    bgr=BGR,
    x_offset=X_OFFSET,
    y_offset=Y_OFFSET
)

tp = XPT2046(spi, cs_pin=TP_CS, irq_pin=TP_IRQ)

# ============================================================
# Touch mapping (manual)
# ============================================================
def clamp(v, lo, hi):
    if v < lo: return lo
    if v > hi: return hi
    return v

def map_linear(raw, raw_min, raw_max, out_min, out_max):
    if raw_max == raw_min:
        return out_min
    v = (raw - raw_min) * (out_max - out_min) // (raw_max - raw_min) + out_min
    return clamp(v, min(out_min, out_max), max(out_min, out_max))

def raw_to_screen(rx, ry):
    c = CAL
    if c.get("SWAP_XY", False):
        rx, ry = ry, rx
    if c.get("FLIP_X", False):
        rx = 3800 - rx
    if c.get("FLIP_Y", False):
        ry = 3800 - ry

    x = map_linear(rx, c["RAW_X_LEFT"], c["RAW_X_RIGHT"], 0, W - 1)
    y = map_linear(ry, c["RAW_Y_TOP"],  c["RAW_Y_BOT"],   0, H - 1)
    return x, y

def debug_dot(x, y):
    lcd.fill_rect(x - 2, y - 2, 5, 5, RED)

# ============================================================
# Accessible Theme system (Settings)
# ============================================================
THEMES = [
    ("DARK (HC)",      0xFFFF, 0x0000,   0xFFFF,  0xFFFF, 0x0000,  0xE71C, 0x0000,  0x07E0),
    ("LIGHT (HC)",       0x0000, 0xFFFF,   0x0000,  0x0000, 0xFFFF,  0x4208, 0xFFFF,  0xFFE0),
    ("YELLOW (ACCESS)",   0x001F, 0xFFFF,   0xFFFF,  0xFFFF, 0x0000,  0xC618, 0x0000,  0x001F),
    ("GREEN (ACCESS)", 0x780F, 0xFFFF,   0xFFFF,  0xFFFF, 0x0000,  0xC618, 0x0000,  0x780F),
    ("AMBER (DARK)",    0x0000, 0xFFE0,   0x0000,  0x0000, 0xFFE0,  0x4208, 0xFFE0,  0xFFE0),
]
theme_index = 0

def th():
    (name,
     title_bg, title_fg,
     screen_bg,
     box_bg, box_border,
     btn_bg, btn_fg,
     accent) = THEMES[theme_index]
    return {
        "name": name,
        "title_bg": title_bg,
        "title_fg": title_fg,
        "screen_bg": screen_bg,
        "box_bg": box_bg,
        "box_border": box_border,
        "btn_bg": btn_bg,
        "btn_fg": btn_fg,
        "accent": accent,
    }

# ============================================================
# Drawing helpers
# ============================================================
def draw_border(x, y, w, h, c):
    lcd.fill_rect(x, y, w, 1, c)
    lcd.fill_rect(x, y+h-1, w, 1, c)
    lcd.fill_rect(x, y, 1, h, c)
    lcd.fill_rect(x+w-1, y, 1, h, c)

def draw_mic_badge(force=False):
    """
    Draw a small badge on the title bar showing QUIET OK / LOUD.
    Only draws if mic is present.
    """
    global _last_badge_draw, _last_badge_state
    if mic is None:
        return

    now = time.ticks_ms()
    if (not force) and time.ticks_diff(now, _last_badge_draw) < MIC_DRAW_THROTTLE:
        return

    state = mic_quiet
    if (not force) and state == _last_badge_state:
        _last_badge_draw = now
        return

    t = th()
    # Badge placement: right side of title bar
    bx = W - 132
    by = 10
    bw = 120
    bh = 28

    if state:
        bg = GREEN
        fg = BLACK
        label = "QUIET OK"
    else:
        bg = MAG
        fg = WHITE
        label = "LOUD"

    # Draw badge background + border
    lcd.fill_rect(bx, by, bw, bh, bg)
    draw_border(bx, by, bw, bh, t["box_border"])

    # Centered text
    scale = 2
    tw = len(label) * 8 * scale
    tx = bx + (bw - tw) // 2
    ty = by + (bh - 16) // 2
    lcd.text(label, tx, ty, fg, bg, scale=scale)

    _last_badge_state = state
    _last_badge_draw = now

def draw_title_bar(title):
    t = th()
    lcd.fill_rect(0, 0, W, 48, t["title_bg"])
    lcd.text(title, 12, 14, t["title_fg"], t["title_bg"], scale=2)
    draw_mic_badge(force=True)

def wrap_text(s, max_chars):
    words = s.split(" ")
    lines = []
    cur = ""
    for w_ in words:
        if "\n" in w_:
            parts = w_.split("\n")
            for i, p in enumerate(parts):
                if p:
                    if len(cur) + (1 if cur else 0) + len(p) <= max_chars:
                        cur = (cur + " " + p).strip()
                    else:
                        if cur: lines.append(cur)
                        cur = p
                if i != len(parts)-1:
                    if cur: lines.append(cur)
                    cur = ""
            continue

        if len(cur) + (1 if cur else 0) + len(w_) <= max_chars:
            cur = (cur + " " + w_).strip()
        else:
            if cur: lines.append(cur)
            cur = w_
    if cur: lines.append(cur)
    return lines

def draw_button(btn: Button, pressed=False):
    t = th()
    bg_default = t["btn_bg"]
    fg_default = t["btn_fg"]
    border = t["box_border"]

    bg = getattr(btn, "bg", bg_default)
    fg = getattr(btn, "fg", fg_default)

    if pressed:
        bg, fg = fg, bg

    lcd.fill_rect(btn.x, btn.y, btn.w, btn.h, bg)
    draw_border(btn.x, btn.y, btn.w, btn.h, border)

    scale = 2
    text_w = len(btn.label) * 8 * scale
    tx = btn.x + (btn.w - text_w) // 2
    ty = btn.y + (btn.h - 16) // 2
    lcd.text(btn.label, tx, ty, fg, bg, scale=scale)

def make_btn(x, y, w, h, label, on_press, bg=None, fg=None):
    b = Button(x, y, w, h, label, on_press)
    if bg is not None: b.bg = bg
    if fg is not None: b.fg = fg
    return b

def draw_text_box(text, x, y, w, h, prefer_scale=2):
    t = th()
    lcd.fill_rect(x, y, w, h, t["box_bg"])
    draw_border(x, y, w, h, t["box_border"])

    for scale in (prefer_scale, 1):
        max_chars = (w - 16) // (8 * scale)
        lines = wrap_text(text, max_chars)
        line_h = 8 * scale + 2
        max_lines = (h - 16) // line_h

        if len(lines) <= max_lines or scale == 1:
            tx = x + 8
            ty = y + 8
            shown = 0
            for line in lines:
                if shown >= max_lines:
                    break
                fg = WHITE if t["box_bg"] == BLACK else BLACK
                lcd.text(line, tx, ty, fg, t["box_bg"], scale=scale)
                ty += line_h
                shown += 1
            return

# ============================================================
# Screens
# ============================================================
SCREEN_MENU      = "menu"
SCREEN_GROUND    = "grounding"
SCREEN_CONTACTS  = "contacts"
SCREEN_SETTINGS  = "settings"
SCREEN_COMM_MENU = "comm_menu"
SCREEN_COMM_CARD = "comm_card"
current_screen = SCREEN_MENU

# ============================================================
# Shared bottom nav layout
# ============================================================
NAV_H = 80
NAV_Y = H - (NAV_H + 20)
NAV_X0 = 20
NAV_GAP = 12
NAV_W = (W - 40 - 2*NAV_GAP) // 3

# Helper: draw top-right indicator without colliding with mic badge
def draw_indicator(text):
    t = th()
    scale = 2
    tw = len(text) * 8 * scale
    reserve = 140 if mic is not None else 0  # reserve space for mic badge on right
    x = W - reserve - tw - 12
    lcd.text(text, x, 14, t["title_fg"], t["title_bg"], scale=scale)

# ============================================================
# Grounding
# ============================================================
GROUNDING_PAGES = [
    "5-4-3-2-1 Senses\n\n"
    "Name:\n"
    "5 things you can SEE\n"
    "4 things you can FEEL\n"
    "3 things you can HEAR\n"
    "2 things you can SMELL\n"
    "1 thing you can TASTE",

    "Box Breathing (4-4-4-4)\n\n"
    "Inhale 4\n"
    "Hold   4\n"
    "Exhale 4\n"
    "Hold   4\n\n"
    "Repeat 4 times.",

    "Feet + Body Scan\n\n"
    "Press feet into floor.\n"
    "Notice pressure + texture.\n"
    "Relax jaw, drop shoulders.\n"
    "Unclench hands.\n"
    "Slow exhale."
]
page_index = 0

btn_ground_prev = Button(NAV_X0 + 0*(NAV_W+NAV_GAP), NAV_Y, NAV_W, NAV_H, "PREV", lambda: None)
btn_ground_menu = Button(NAV_X0 + 1*(NAV_W+NAV_GAP), NAV_Y, NAV_W, NAV_H, "MENU", lambda: None)
btn_ground_next = Button(NAV_X0 + 2*(NAV_W+NAV_GAP), NAV_Y, NAV_W, NAV_H, "NEXT", lambda: None)

def draw_grounding():
    t = th()
    lcd.fill(t["screen_bg"])
    draw_title_bar("GROUNDING")

    box_x = 16
    box_y = 60
    box_w = W - 32
    box_h = H - 60 - (NAV_H + 30)

    indicator = f"{page_index+1}/{len(GROUNDING_PAGES)}"
    draw_indicator(indicator)

    draw_text_box(GROUNDING_PAGES[page_index], box_x, box_y, box_w, box_h, prefer_scale=2)

    draw_button(btn_ground_prev)
    draw_button(btn_ground_menu)
    draw_button(btn_ground_next)

def show_grounding():
    global current_screen
    current_screen = SCREEN_GROUND
    draw_grounding()

def grounding_prev():
    global page_index
    if page_index > 0:
        page_index -= 1
        draw_grounding()

def grounding_next():
    global page_index
    if page_index < len(GROUNDING_PAGES) - 1:
        page_index += 1
        draw_grounding()

btn_ground_prev.on_press = grounding_prev
btn_ground_next.on_press = grounding_next

# ============================================================
# Contacts
# ============================================================
CONTACT_TEXT = (
    "CONTACT DETAILS\n\n"
    "Name: -------\n"
    "Pronouns: She/Her\n"
    "Phone: -------\n\n"
    "Emergency Contact\n"
    "Name: ------\n"
    "Relationship: -------\n"
    "Phone: ----------\n\n"
    "Medical Notes\n"
    "- Please be patient.\n"
    "- Prefer text / yes-no questions.\n"
    "- Sensory overload: noise/crowds.\n"
    "- Needs space + quiet to regulate.\n"
)

btn_contacts_menu = Button(NAV_X0 + 1*(NAV_W+NAV_GAP), NAV_Y, NAV_W, NAV_H, "MENU", lambda: None)

def show_contacts():
    global current_screen
    t = th()
    current_screen = SCREEN_CONTACTS
    lcd.fill(t["screen_bg"])
    draw_title_bar("CONTACTS")

    box_x = 16
    box_y = 60
    box_w = W - 32
    box_h = H - 60 - (NAV_H + 30)

    draw_text_box(CONTACT_TEXT, box_x, box_y, box_w, box_h, prefer_scale=2)
    draw_button(btn_contacts_menu)

# ============================================================
# Settings (theme picker)
# ============================================================
settings_buttons = []
btn_settings_menu = Button(NAV_X0 + 1*(NAV_W+NAV_GAP), NAV_Y, NAV_W, NAV_H, "MENU", lambda: None)

def apply_theme(idx):
    global theme_index
    theme_index = idx
    show_settings()

def show_settings():
    global current_screen, settings_buttons
    t = th()
    current_screen = SCREEN_SETTINGS
    lcd.fill(t["screen_bg"])
    draw_title_bar("SETTINGS")

    settings_buttons = []
    grid_x = 20
    grid_y = 70
    grid_w = W - 40
    grid_h = H - 70 - (NAV_H + 30)

    cols = 2
    rows = (len(THEMES) + 1) // 2
    gapx = 12
    gapy = 12
    bw = (grid_w - gapx) // 2
    bh = (grid_h - (rows-1)*gapy) // rows

    for i, theme_row in enumerate(THEMES):
        name = theme_row[0]
        title_bg = theme_row[1]
        title_fg = theme_row[2]
        c = i % cols
        r = i // cols
        x = grid_x + c * (bw + gapx)
        y = grid_y + r * (bh + gapy)

        def make_apply(idx=i):
            def _go():
                apply_theme(idx)
            return _go

        b = make_btn(x, y, bw, bh, name, make_apply(), bg=title_bg, fg=title_fg)
        settings_buttons.append(b)
        draw_button(b)

    draw_button(btn_settings_menu)

# ============================================================
# Communication Cards
# ============================================================
def speak(text):
    print("SPEAK:", text)

CAT_NEEDS = [
    ("!", "I NEED HELP", RED, WHITE),
    ("~", "PLEASE WAIT", ORNG, BLACK),
    ("~", "I NEED SPACE", ORNG, BLACK),
    ("~", "I NEED A BREAK", YELL, BLACK),
    ("~", "WATER PLEASE", CYAN, BLACK),
    ("~", "HUNGRY", GREEN, BLACK),
    ("~", "TIRED", GREY, BLACK),
    ("!", "I NEED TO GO HOME", ORNG, BLACK),
]
CAT_SENSORY = [
    ("!", "TOO LOUD", MAG, WHITE),
    ("!", "TOO MANY PEOPLE", MAG, WHITE),
    ("!", "TOO BRIGHT", MAG, WHITE),
    ("~", "I NEED QUIET", BLUE, WHITE),
    ("~", "I NEED DIM LIGHTS", BLUE, WHITE),
]
CAT_RESPONSES = [
    ("?", "YES", GREEN, BLACK),
    ("?", "NO", RED, WHITE),
    ("~", "MAYBE", YELL, BLACK),
    ("~", "I DON'T KNOW", GREY, BLACK),
    ("~", "PLEASE TEXT ME", CYAN, BLACK),
    ("~", "I CAN'T SPEAK RIGHT NOW", CYAN, BLACK),
]
CAT_FEELINGS = [
    ("*", "I FEEL OVERWHELMED", ORNG, BLACK),
    ("*", "I FEEL SICK", RED, WHITE),
    ("*", "I FEEL ANXIOUS", ORNG, BLACK),
    ("*", "I AM OKAY", GREEN, BLACK),
]
FAV_CARDS = [
    ("!", "PLEASE WAIT", ORNG, BLACK),
    ("!", "I NEED SPACE", ORNG, BLACK),
    ("!", "TOO LOUD", MAG, WHITE),
    ("!", "TOO MANY PEOPLE", MAG, WHITE),
    ("!", "I NEED TO GO HOME", ORNG, BLACK),
    ("~", "WATER PLEASE", CYAN, BLACK),
]

COMM_CATEGORIES = [
    ("FAVOURITES", FAV_CARDS, YELL, BLACK),
    ("NEEDS",      CAT_NEEDS, GREEN, BLACK),
    ("SENSORY",    CAT_SENSORY, MAG, WHITE),
    ("RESPONSES",  CAT_RESPONSES, CYAN, BLACK),
    ("FEELINGS",   CAT_FEELINGS, ORNG, BLACK),
]

comm_menu_buttons = []
comm_cards = FAV_CARDS
comm_cat_name = "FAVOURITES"
comm_card_index = 0

btn_commmenu_menu = Button(NAV_X0 + 1*(NAV_W+NAV_GAP), NAV_Y, NAV_W, NAV_H, "MENU", lambda: None)

def open_category(idx):
    global comm_cards, comm_cat_name, comm_card_index
    comm_cat_name, comm_cards, _, _ = COMM_CATEGORIES[idx]
    comm_card_index = 0
    show_comm_card()

def build_comm_menu_buttons():
    global comm_menu_buttons
    comm_menu_buttons = []

    cols = 2
    rows = (len(COMM_CATEGORIES) + 1) // 2

    grid_x = 20
    grid_y = 70
    grid_w = W - 40
    grid_h = H - 70 - (NAV_H + 30)

    gapx = 12
    gapy = 12
    bw = (grid_w - gapx) // 2
    bh = (grid_h - (rows-1)*gapy) // rows

    for i, (name, cards, bg, fg) in enumerate(COMM_CATEGORIES):
        c = i % cols
        r = i // cols
        x = grid_x + c * (bw + gapx)
        y = grid_y + r * (bh + gapy)

        def make_open(idx=i):
            def _open():
                open_category(idx)
            return _open

        b = make_btn(x, y, bw, bh, name, make_open(), bg=bg, fg=fg)
        comm_menu_buttons.append(b)

def show_comm_menu():
    global current_screen
    t = th()
    current_screen = SCREEN_COMM_MENU
    lcd.fill(t["screen_bg"])
    draw_title_bar("COMM CARDS")
    build_comm_menu_buttons()
    for b in comm_menu_buttons:
        draw_button(b)
    draw_button(btn_commmenu_menu)

btn_comm_prev = Button(NAV_X0 + 0*(NAV_W+NAV_GAP), NAV_Y, NAV_W, NAV_H, "PREV", lambda: None)
btn_comm_cats = Button(NAV_X0 + 1*(NAV_W+NAV_GAP), NAV_Y, NAV_W, NAV_H, "CATS", lambda: None)
btn_comm_next = Button(NAV_X0 + 2*(NAV_W+NAV_GAP), NAV_Y, NAV_W, NAV_H, "NEXT", lambda: None)

ACT_H = 44
ACT_Y = 48 + 8
ACT_X0 = 20
ACT_GAP = 12
ACT_W = (W - 40 - ACT_GAP) // 2

btn_comm_speak = make_btn(ACT_X0, ACT_Y, ACT_W, ACT_H, "SPEAK", lambda: None, bg=BLUE, fg=WHITE)
btn_comm_menu  = make_btn(ACT_X0 + ACT_W + ACT_GAP, ACT_Y, ACT_W, ACT_H, "MENU", lambda: None)

def draw_comm_card():
    t = th()
    lcd.fill(t["screen_bg"])
    draw_title_bar(comm_cat_name)

    draw_button(btn_comm_speak)
    draw_button(btn_comm_menu)

    card_x = 16
    card_y = ACT_Y + ACT_H + 10
    card_w = W - 32
    card_h = H - card_y - (NAV_H + 30)

    icon, phrase, bg, fg = comm_cards[comm_card_index]

    lcd.fill_rect(card_x, card_y, card_w, card_h, bg)
    draw_border(card_x, card_y, card_w, card_h, t["box_border"])

    indicator = f"{comm_card_index+1}/{len(comm_cards)}"
    draw_indicator(indicator)

    icon_scale = 4
    icon_w = len(icon) * 8 * icon_scale
    ix = card_x + (card_w - icon_w) // 2
    iy = card_y + 18
    lcd.text(icon, ix, iy, fg, bg, scale=icon_scale)

    scale = 3
    max_chars = (card_w - 20) // (8 * scale)
    if max_chars < 6:
        scale = 2
        max_chars = (card_w - 20) // (8 * scale)
    if max_chars < 6:
        scale = 1
        max_chars = (card_w - 20) // (8 * scale)

    lines = wrap_text(phrase, max_chars)
    line_h = 8 * scale + 6
    total_h = len(lines) * line_h

    start_y = iy + 8*icon_scale + 18
    rem_h = (card_y + card_h) - start_y - 12
    y = start_y + (rem_h - total_h) // 2

    for line in lines:
        text_w = len(line) * 8 * scale
        x = card_x + (card_w - text_w) // 2
        lcd.text(line, x, y, fg, bg, scale=scale)
        y += line_h

    draw_button(btn_comm_prev)
    draw_button(btn_comm_cats)
    draw_button(btn_comm_next)

def show_comm_card():
    global current_screen
    current_screen = SCREEN_COMM_CARD
    draw_comm_card()

def comm_prev():
    global comm_card_index
    if comm_card_index > 0:
        comm_card_index -= 1
        draw_comm_card()

def comm_next():
    global comm_card_index
    if comm_card_index < len(comm_cards) - 1:
        comm_card_index += 1
        draw_comm_card()

btn_comm_prev.on_press = comm_prev
btn_comm_next.on_press = comm_next
btn_comm_cats.on_press = show_comm_menu
btn_comm_speak.on_press = lambda: speak(comm_cards[comm_card_index][1])

# ============================================================
# MENU ITEMS
# ============================================================
MENU_ALL_ITEMS = [
    ("GROUNDING TECHNIQUES", show_grounding),
    ("COMMUNICATION CARDS",  show_comm_menu),
    ("CONTACT DETAILS",      show_contacts),
    ("SETTINGS",             show_settings),
]

# ============================================================
# MENU (PAGED) - 2 items per page, AUTO LAYOUT, NO OVERLAP
# ============================================================
MENU_ITEMS_PER_PAGE = 2
menu_page = 0
menu_buttons = []

btn_menu_prev = Button(NAV_X0 + 0*(NAV_W+NAV_GAP), NAV_Y, NAV_W, NAV_H, "PREV", lambda: None)
btn_menu_next = Button(NAV_X0 + 2*(NAV_W+NAV_GAP), NAV_Y, NAV_W, NAV_H, "NEXT", lambda: None)

def menu_total_pages():
    return (len(MENU_ALL_ITEMS) + MENU_ITEMS_PER_PAGE - 1) // MENU_ITEMS_PER_PAGE

def build_menu_buttons():
    global menu_buttons

    X0 = 20
    TOP = 60
    BOTTOM = NAV_Y - 10
    AREA_H = BOTTOM - TOP

    GAP = 14
    BTN_W = W - 2*X0
    BTN_H = (AREA_H - GAP) // 2
    if BTN_H < 40:
        BTN_H = 40

    start = menu_page * MENU_ITEMS_PER_PAGE
    end = min(start + MENU_ITEMS_PER_PAGE, len(MENU_ALL_ITEMS))

    menu_buttons = []
    y = TOP
    for i in range(start, end):
        label, fn = MENU_ALL_ITEMS[i]
        menu_buttons.append(Button(X0, y, BTN_W, BTN_H, label, fn))
        y += BTN_H + GAP

def draw_menu():
    t = th()
    lcd.fill(t["screen_bg"])
    draw_title_bar("MENU")

    build_menu_buttons()
    for b in menu_buttons:
        draw_button(b)

    pages = menu_total_pages()
    indicator = f"{menu_page+1}/{pages}"
    draw_indicator(indicator)

    if pages > 1:
        draw_button(btn_menu_prev)
        draw_button(btn_menu_next)

def show_menu():
    global current_screen
    current_screen = SCREEN_MENU
    draw_menu()

def menu_prev():
    global menu_page
    if menu_page > 0:
        menu_page -= 1
        draw_menu()

def menu_next():
    global menu_page
    if menu_page < menu_total_pages() - 1:
        menu_page += 1
        draw_menu()

btn_menu_prev.on_press = menu_prev
btn_menu_next.on_press = menu_next

# ============================================================
# Wire MENU buttons on screens
# ============================================================
btn_ground_menu.on_press    = show_menu
btn_contacts_menu.on_press  = show_menu
btn_settings_menu.on_press  = show_menu
btn_commmenu_menu.on_press  = show_menu
btn_comm_menu.on_press      = show_menu

# ============================================================
# Touch loop
# ============================================================
was_down = False
last_tap_ms = 0

def screen_buttons():
    if current_screen == SCREEN_MENU:
        btns = list(menu_buttons)
        if menu_total_pages() > 1:
            btns += [btn_menu_prev, btn_menu_next]
        return btns

    if current_screen == SCREEN_GROUND:
        return [btn_ground_prev, btn_ground_menu, btn_ground_next]

    if current_screen == SCREEN_CONTACTS:
        return [btn_contacts_menu]

    if current_screen == SCREEN_SETTINGS:
        return settings_buttons + [btn_settings_menu]

    if current_screen == SCREEN_COMM_MENU:
        return comm_menu_buttons + [btn_commmenu_menu]

    if current_screen == SCREEN_COMM_CARD:
        return [btn_comm_speak, btn_comm_menu, btn_comm_prev, btn_comm_cats, btn_comm_next]

    return []

def poll_mic_and_update_badge():
    global mic_quiet, mic_rms, _last_mic_poll
    if mic is None:
        return

    now = time.ticks_ms()
    if time.ticks_diff(now, _last_mic_poll) < MIC_POLL_MS:
        return
    _last_mic_poll = now

    info = mic.update()
    mic_quiet = bool(info["quiet"])
    mic_rms = float(info["rms"])

    # If badge state changed, redraw just the badge area
    draw_mic_badge(force=False)

# start
show_menu()

while True:
    now = time.ticks_ms()

    # Mic update (throttled)
    poll_mic_and_update_badge()

    t = tp.read(samples=9, delay_us=120) if tp.touched() else None
    down = False
    sx = sy = None

    if t:
        rx, ry, p = t
        if p <= CAL.get("P_MAX", 1200):
            sx, sy = raw_to_screen(rx, ry)
            debug_dot(sx, sy)
            down = True

    if down and not was_down:
        if time.ticks_diff(now, last_tap_ms) > 160:
            last_tap_ms = now

            for b in screen_buttons():
                if b.contains(sx, sy):
                    draw_button(b, pressed=True)
                    time.sleep_ms(70)
                    draw_button(b, pressed=False)
                    b.on_press()
                    break

    was_down = down
    time.sleep_ms(12)
