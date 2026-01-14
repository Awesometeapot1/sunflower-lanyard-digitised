# ui.py
from touch_cal import CAL

def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v

def map_range(v, in_a, in_b, out_a, out_b):
    if in_a == in_b:
        return out_a
    return out_a + (v - in_a) * (out_b - out_a) // (in_b - in_a)

class TouchMapper:
    def __init__(self):
        self.cal = CAL

    def raw_to_screen(self, rx, ry):
        c = self.cal

        if c["SWAP_XY"]:
            rx, ry = ry, rx

        x = map_range(rx, c["RAW_X_LEFT"], c["RAW_X_RIGHT"], 0, c["W"] - 1)
        y = map_range(ry, c["RAW_Y_TOP"],  c["RAW_Y_BOT"],   0, c["H"] - 1)

        if c["FLIP_X"]:
            x = (c["W"] - 1) - x
        if c["FLIP_Y"]:
            y = (c["H"] - 1) - y

        x = clamp(x, 0, c["W"] - 1)
        y = clamp(y, 0, c["H"] - 1)
        return x, y

class Button:
    def __init__(self, x, y, w, h, label, on_press):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.label = label
        self.on_press = on_press
        self._pressed = False

    def contains(self, px, py):
        return (self.x <= px < self.x + self.w) and (self.y <= py < self.y + self.h)

    def draw(self, lcd, fg, bg):
        # simple filled rectangle + crude text via dots (label optional)
        for yy in range(self.y, self.y + self.h):
            for xx in range(self.x, self.x + self.w):
                lcd.draw_pixel(xx, yy, bg)

        # tiny “label bar” indicator (no font dependency)
        # draw a small line to show button exists
        for xx in range(self.x + 6, self.x + self.w - 6):
            lcd.draw_pixel(xx, self.y + self.h // 2, fg)

class Screen:
    def __init__(self, name):
        self.name = name
        self.buttons = []

    def add_button(self, btn: Button):
        self.buttons.append(btn)

    def draw(self, lcd, colors):
        # default: clear + draw buttons
        lcd.fill(colors["BG"])
        for b in self.buttons:
            b.draw(lcd, colors["FG"], colors["BTN"])

    def handle_touch(self, x, y, pressed):
        # pressed=True means finger down; pressed=False finger up
        if not pressed:
            # reset press latch so next touch can trigger again
            for b in self.buttons:
                b._pressed = False
            return None

        for b in self.buttons:
            if b.contains(x, y) and not b._pressed:
                b._pressed = True
                if b.on_press:
                    return b.on_press()
        return None

class UI:
    def __init__(self, lcd):
        self.lcd = lcd
        self.screens = {}
        self.current = None
        self.colors = {
            "BG": 0x0000,  # black
            "FG": 0xFFFF,  # white
            "BTN": 0x7BEF, # grey-ish (RGB565)
        }

    def add_screen(self, screen: Screen):
        self.screens[screen.name] = screen

    def show(self, name):
        self.current = self.screens[name]
        self.current.draw(self.lcd, self.colors)
