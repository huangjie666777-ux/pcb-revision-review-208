
class GerberError(Exception):
    """Gerber 解析/构建错误，携带原文位置。"""

    def __init__(self, message, line=None, source=None):
        self.message = message
        self.line = line
        self.source = source
        super().__init__(self.__str__())

    def __str__(self):
        loc = ""
        if self.line is not None:
            loc = f"line {self.line}"
            if self.source:
                loc += f" [{self.source.strip()}]"
            loc += ": "
        return loc + self.message

