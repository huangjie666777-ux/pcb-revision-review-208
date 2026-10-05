
"""ASCII Gerber 子集解析器。

支持: FSLA(前导零省略/绝对坐标), MO(MM/IN), AD(C/R 光圈),
Dnn 选光圈, D01/D02/D03, G01 直线, G75 下 G02/G03 圆弧,
G36/G37 直线闭环区域, G04 注释, M02 结束, LPD/LPC 极性。
输出低层事件流, 由 geometry 模块消费。
"""
import re
from dataclasses import dataclass, field

from .errors import GerberError

_WORD_RE = re.compile(r"([A-Za-z])([+-]?[0-9.]+)")


@dataclass
class Aperture:
    kind: str            # 'C' 或 'R'
    params: tuple        # C: (diameter,)  R: (x, y)


@dataclass
class Event:
    kind: str            # select_aperture/polarity/flash/draw/region_draw/
                         # region_start/region_end/eof
    line: int
    source: str
    data: dict = field(default_factory=dict)


class Parser:
    def __init__(self, text):
        self.text = text
        self.x_int = self.x_dec = None
        self.y_int = self.y_dec = None
        self.unit = None               # 毫米缩放因子
        self.apertures = {}            # Dnn -> Aperture
        self.current_aperture = None
        self.interpolation = "G01"
        self.multi_quadrant = False
        self.in_region = False
        self.region_line = None
        self.region_source = None
        self.pos = {"X": None, "Y": None}
        self.operation = None          # 模态 D01/D02/D03
        self.events = []
        self.ended = False

    # ---- 坐标解码: 前导零省略, 绝对坐标 ----
    def _coord(self, axis, raw, line, source):
        if self.x_dec is None:
            raise GerberError("坐标出现在 %FS 格式声明之前", line, source)
        if self.unit is None:
            raise GerberError("坐标出现在 %MO 单位声明之前", line, source)
        dec = self.x_dec if axis in ("X", "I") else self.y_dec
        m = re.fullmatch(r"([+-]?)(\d+)", raw)
        if not m:
            raise GerberError("非法坐标数值 " + repr(raw), line, source)
        sign, digits = m.groups()
        value = int(digits) / (10 ** dec)
        if sign == "-":
            value = -value
        return value * self.unit

    def parse(self):
        # 1) 按 '*' 分块并记录起始行与是否带结束符
        raw_blocks = []
        line = 1
        start_line = 1
        started = False
        buf = ""
        for ch in self.text:
            if ch == "*":
                raw_blocks.append((start_line, buf, True))
                buf = ""
                started = False
            else:
                if ch == "\n":
                    line += 1
                if not started and not ch.isspace():
                    started = True
                    start_line = line
                buf += ch
        if buf.strip():
            raw_blocks.append((start_line, buf, False))
        if raw_blocks and not raw_blocks[-1][2]:
            tail_line, tail = raw_blocks[-1][0], raw_blocks[-1][1].strip()
            raise GerberError(
                "指令块缺少 * 结束符(末尾 M02 也须以 * 收尾)",
                tail_line, tail)

        # 2) 状态机扫描: '%' 之间为扩展指令, 其余为普通指令块
        extended = False
        ext_buf = ""
        ext_line = 1
        for blk_line, body, _terminated in raw_blocks:
            src = body
            cur_line = blk_line
            while src:
                if extended:
                    j = src.find("%")
                    if j == -1:
                        ext_buf += src
                        src = ""
                    else:
                        ext_buf += src[:j]
                        self._extended(ext_buf.strip(), ext_line, ext_buf.strip())
                        cur_line += src[:j + 1].count("\n")
                        ext_buf = ""
                        extended = False
                        src = src[j + 1:]
                else:
                    j = src.find("%")
                    chunk = src if j == -1 else src[:j]
                    if chunk.strip():
                        off = len(chunk) - len(chunk.lstrip())
                        cmd_line = cur_line + chunk[:off].count("\n")
                        self._block(chunk.strip(), cmd_line, chunk.strip())
                    if j == -1:
                        src = ""
                    else:
                        cur_line += src[:j + 1].count("\n")
                        extended = True
                        ext_line = blk_line
                        ext_buf = ""
                        src = src[j + 1:]
        if extended:
            raise GerberError("扩展指令未以 % 结束", ext_line, ext_buf)
        if self.in_region:
            raise GerberError("文件截断: G36 区域缺少 G37 结束",
                              self.region_line, self.region_source)
        if not self.ended:
            last_line = self.text.count("\n") + 1
            last_src = (self.text.rstrip("\n").split("\n")[-1].strip()
                        if self.text.strip() else None)
            raise GerberError("文件截断: 缺少 M02 结束指令",
                              last_line if self.text.strip() else None,
                              last_src)
        return self.events

    # ---- 扩展指令 ----
    def _extended(self, body, line, source):
        body = body.strip()
        m = re.fullmatch(r"FS([LT])([AI])X(\d)(\d)Y(\d)(\d)", body)
        if m:
            lead, absol, xi, xd, yi, yd = m.groups()
            if lead != "L":
                raise GerberError("仅支持前导零省略(FSL)", line, source)
            if absol != "A":
                raise GerberError("仅支持绝对坐标(FSA)", line, source)
            self.x_int, self.x_dec = int(xi), int(xd)
            self.y_int, self.y_dec = int(yi), int(yd)
            return
        m = re.fullmatch(r"MO(MM|IN)", body)
        if m:
            self.unit = 1.0 if m.group(1) == "MM" else 25.4
            return
        m = re.fullmatch(r"AD(D\d+)([A-Za-z])(.*)", body)
        if m:
            code, kind, rest = m.groups()
            num = int(code[1:])
            if num < 10:
                raise GerberError("光圈编号 D%d 非法(需 >= 10)" % num, line, source)
            if kind not in ("C", "R"):
                raise GerberError("不支持的光圈类型 " + repr(kind) + "(仅支持 C/R)",
                                  line, source)
            parts = rest.lstrip(",").split("X") if rest else []
            try:
                params = tuple(float(p) for p in parts if p != "")
            except ValueError:
                raise GerberError("光圈参数非法", line, source)
            need = 1 if kind == "C" else 2
            if len(params) != need or any(p <= 0 for p in params):
                raise GerberError("%s 光圈需要 %d 个正数参数" % (kind, need),
                                  line, source)
            if self.unit is None:
                raise GerberError("AD 出现在 %MO 单位声明之前", line, source)
            self.apertures[num] = Aperture(
                kind, tuple(p * self.unit for p in params))
            return
        m = re.fullmatch(r"LP(D|C)", body)
        if m:
            self.events.append(Event("polarity", line, source,
                                     {"dark": m.group(1) == "D"}))
            return
        raise GerberError("不支持的扩展指令 %" + body + "%", line, source)

    # ---- 普通指令块 ----
    def _block(self, chunk, line, source):
        if chunk.startswith("G04"):
            return  # 注释
        pos = 0
        gcodes = []
        words = {}
        for m in _WORD_RE.finditer(chunk):
            letter, val = m.group(1).upper(), m.group(2)
            if m.start() != pos:
                raise GerberError("无法解析的指令块 " + repr(chunk), line, source)
            pos = m.end()
            if letter == "G":
                gcodes.append(int(val))
            elif letter == "M":
                if int(val) == 2:
                    self.ended = True
                    self.events.append(Event("eof", line, source, {}))
                else:
                    raise GerberError("不支持的 M%02d 指令" % int(val), line, source)
            elif letter == "D":
                words["D"] = int(float(val))
            elif letter in "XYIJ":
                words[letter] = val
            else:
                raise GerberError("不支持的指令字母 " + repr(letter), line, source)
        if pos != len(chunk):
            raise GerberError("无法解析的指令块 " + repr(chunk), line, source)

        for g in gcodes:
            if g in (1, 2, 3):
                self.interpolation = "G%02d" % g
            elif g == 36:
                if self.in_region:
                    raise GerberError("区域嵌套(G36 内再次出现 G36)", line, source)
                self.in_region = True
                self.region_line = line
                self.region_source = source
                self.events.append(Event("region_start", line, source, {}))
            elif g == 37:
                if not self.in_region:
                    raise GerberError("G37 之前没有 G36", line, source)
                self.in_region = False
                self.events.append(Event("region_end", line, source, {}))
            elif g == 75:
                self.multi_quadrant = True
            elif g == 74:
                raise GerberError("不支持单象限圆弧(G74), 仅支持 G75 多象限",
                                  line, source)
            elif g == 90:
                pass  # 绝对坐标, 与 FSA 一致
            elif g == 4:
                pass
            else:
                raise GerberError("不支持的 G%02d 指令" % g, line, source)

        d = words.get("D")
        if d is not None and d >= 10 and not any(k in words for k in "XYIJ"):
            if d not in self.apertures:
                raise GerberError("未定义的光圈 D%d" % d, line, source)
            self.current_aperture = d
            self.events.append(Event("select_aperture", line, source, {"d": d}))
            return

        has_xy = "X" in words or "Y" in words
        if has_xy or d in (1, 2, 3):
            self._draw(words, d, line, source)

    def _draw(self, words, d, line, source):
        x = (self._coord("X", words["X"], line, source) if "X" in words
             else self.pos["X"])
        y = (self._coord("Y", words["Y"], line, source) if "Y" in words
             else self.pos["Y"])
        op = d if d in (1, 2, 3) else self.operation
        if op is None:
            raise GerberError("缺少 D01/D02/D03 操作码", line, source)
        if x is None or y is None:
            raise GerberError("首个坐标缺少 X 或 Y", line, source)

        if op == 2:
            pass  # 移动
        elif op == 3:
            if self.current_aperture is None:
                raise GerberError("闪光前未选择光圈", line, source)
            if self.in_region:
                raise GerberError("区域内不允许闪光(D03)", line, source)
            self.events.append(Event("flash", line, source, {
                "x": x, "y": y, "aperture": self.current_aperture}))
        elif op == 1:
            if self.pos["X"] is None:
                raise GerberError("D01 绘制前未定义当前位置", line, source)
            if not self.in_region and self.current_aperture is None:
                raise GerberError("绘制前未选择光圈", line, source)
            data = {"x1": self.pos["X"], "y1": self.pos["Y"],
                    "x2": x, "y2": y, "aperture": self.current_aperture}
            if self.interpolation in ("G02", "G03"):
                if self.in_region:
                    raise GerberError("区域仅支持直线(G01)闭环", line, source)
                i = (self._coord("I", words["I"], line, source)
                     if "I" in words else 0.0)
                j = (self._coord("J", words["J"], line, source)
                     if "J" in words else 0.0)
                data.update({"arc": self.interpolation, "i": i, "j": j})
            self.events.append(Event("region_draw" if self.in_region else "draw",
                                     line, source, data))
        if d in (1, 2, 3):
            self.operation = d
        self.pos["X"], self.pos["Y"] = x, y


def parse(text):
    return Parser(text).parse()
