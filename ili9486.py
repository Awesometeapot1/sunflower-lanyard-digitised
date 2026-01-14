from machine import Pin, SPI
import time
import framebuf

# ILI9486 SPI driver (RGB565).
# Adds:
# - madctl (rotation/mirroring)
# - bgr (colour order)
# - x_offset / y_offset (remove black bars / panel shift)
# - fill_rect + blit_buffer + simple text()

class ILI9486:
    def __init__(self, spi: SPI, cs: int, dc: int, rst: int,
                 width=320, height=480,
                 madctl=0x48, bgr=True,
                 x_offset=0, y_offset=0):
        self.spi = spi
        self.cs = Pin(cs, Pin.OUT, value=1)
        self.dc = Pin(dc, Pin.OUT, value=0)
        self.rst = Pin(rst, Pin.OUT, value=1)

        self.width = int(width)
        self.height = int(height)

        self.x_offset = int(x_offset)
        self.y_offset = int(y_offset)

        # MADCTL BGR bit = bit 3
        if bgr:
            madctl |= 0x08
        else:
            madctl &= ~0x08
        self.madctl = madctl & 0xFF

        self.reset()
        self.init_display()

    def reset(self):
        self.rst.value(1); time.sleep_ms(50)
        self.rst.value(0); time.sleep_ms(50)
        self.rst.value(1); time.sleep_ms(120)

    def write_cmd(self, cmd: int):
        self.cs.value(0)
        self.dc.value(0)
        self.spi.write(bytearray([cmd & 0xFF]))
        self.cs.value(1)

    def write_data(self, data: bytes):
        self.cs.value(0)
        self.dc.value(1)
        self.spi.write(data)
        self.cs.value(1)

    def _set_window(self, x0, y0, x1, y1):
        # Apply panel offsets (for removing black bars)
        x0 += self.x_offset; x1 += self.x_offset
        y0 += self.y_offset; y1 += self.y_offset

        self.write_cmd(0x2A)  # CASET
        self.write_data(bytearray([x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF]))

        self.write_cmd(0x2B)  # PASET
        self.write_data(bytearray([y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF]))

        self.write_cmd(0x2C)  # RAMWR

    def init_display(self):
        self.write_cmd(0x01)      # SWRESET
        time.sleep_ms(150)

        self.write_cmd(0x11)      # SLPOUT
        time.sleep_ms(120)

        self.write_cmd(0x3A)      # COLMOD
        self.write_data(b"\x55")  # 16-bit RGB565

        self.write_cmd(0x36)      # MADCTL
        self.write_data(bytearray([self.madctl]))

        self.write_cmd(0x29)      # DISPON
        time.sleep_ms(50)

    def fill(self, color565: int):
        self.fill_rect(0, 0, self.width, self.height, color565)

    def fill_rect(self, x, y, w, h, color565: int):
        if w <= 0 or h <= 0:
            return

        # clip
        if x < 0:
            w += x; x = 0
        if y < 0:
            h += y; y = 0
        if x + w > self.width:
            w = self.width - x
        if y + h > self.height:
            h = self.height - y
        if w <= 0 or h <= 0:
            return

        self._set_window(x, y, x + w - 1, y + h - 1)

        hi = (color565 >> 8) & 0xFF
        lo = color565 & 0xFF

        chunk_pixels = 1024
        buf = bytearray(chunk_pixels * 2)
        for i in range(0, len(buf), 2):
            buf[i] = hi
            buf[i + 1] = lo

        total_pixels = w * h
        full_chunks = total_pixels // chunk_pixels
        remainder = total_pixels % chunk_pixels

        self.cs.value(0)
        self.dc.value(1)
        for _ in range(full_chunks):
            self.spi.write(buf)
        if remainder:
            self.spi.write(buf[:remainder * 2])
        self.cs.value(1)

    def draw_pixel(self, x, y, color565: int):
        if not (0 <= x < self.width and 0 <= y < self.height):
            return
        self._set_window(x, y, x, y)
        self.write_data(bytearray([(color565 >> 8) & 0xFF, color565 & 0xFF]))

    def blit_buffer(self, buf, x, y, w, h, source_little_endian=True):
        """Push RGB565 buffer to display. framebuf.RGB565 is little-endian."""
        if w <= 0 or h <= 0:
            return

        # clip (simple)
        if x < 0 or y < 0 or x + w > self.width or y + h > self.height:
            # keep it simple: don’t blit off-screen in this helper
            return

        self._set_window(x, y, x + w - 1, y + h - 1)

        self.cs.value(0)
        self.dc.value(1)

        if not source_little_endian:
            self.spi.write(buf)
            self.cs.value(1)
            return

        mv = memoryview(buf)
        chunk = 2048  # bytes
        tmp = bytearray(chunk)

        for i in range(0, len(mv), chunk):
            part = mv[i:i + chunk]
            n = len(part)
            if n != chunk:
                tmp = bytearray(n)

            for j in range(0, n, 2):
                tmp[j] = part[j + 1]
                tmp[j + 1] = part[j]

            self.spi.write(tmp)

        self.cs.value(1)

    def text(self, s, x, y, color565=0xFFFF, bg565=None, scale=2):
        """Small helper for labels using MicroPython’s built-in 8x8 font."""
        if not s:
            return

        bw = 8 * len(s)
        bh = 8
        buf = bytearray(bw * bh * 2)
        fb = framebuf.FrameBuffer(buf, bw, bh, framebuf.RGB565)

        if bg565 is None:
            fb.fill(0)
        else:
            fb.fill(bg565)

        fb.text(s, 0, 0, color565)

        if scale <= 1:
            self.blit_buffer(buf, x, y, bw, bh, source_little_endian=True)
            return

        w = bw * scale
        h = bh * scale
        out = bytearray(w * h * 2)
        outfb = framebuf.FrameBuffer(out, w, h, framebuf.RGB565)
        if bg565 is None:
            outfb.fill(0)
        else:
            outfb.fill(bg565)

        for yy in range(bh):
            for xx in range(bw):
                idx = (yy * bw + xx) * 2
                lo = buf[idx]
                hi = buf[idx + 1]
                pix = (hi << 8) | lo
                if bg565 is None and pix == 0:
                    continue
                for sy in range(scale):
                    for sx in range(scale):
                        outfb.pixel(xx * scale + sx, yy * scale + sy, pix)

        self.blit_buffer(out, x, y, w, h, source_little_endian=True)
