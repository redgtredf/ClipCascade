Byte = {"LF": "\x0A", "NULL": "\x00"}


class MalformedFrameError(ValueError):
    """A received frame does not follow the STOMP wire format."""


class Frame:

    def __init__(self, command, headers, body):
        self.command = command
        self.headers = headers
        self.body = "" if body is None else body

    def __str__(self):
        lines = [self.command]
        skipContentLength = "content-length" in self.headers
        if skipContentLength:
            del self.headers["content-length"]

        for name in self.headers:
            value = self.headers[name]
            lines.append("" + name + ":" + value)

        if self.body is not None and not skipContentLength:
            lines.append("content-length:" + str(len(self.body)))

        lines.append(Byte["LF"] + self.body)
        return Byte["LF"].join(lines)

    @staticmethod
    def unmarshall_single(data):
        if not isinstance(data, str) or not data:
            raise MalformedFrameError("empty frame")

        lines = data.split(Byte["LF"])

        command = lines[0].strip()
        if not command:
            raise MalformedFrameError("missing command")

        headers = {}

        # get all headers, up to the blank separator line; a frame without
        # one is malformed, not an IndexError
        i = 1
        while i < len(lines) and lines[i] != "":
            header = lines[i]
            if ":" not in header:
                raise MalformedFrameError("header line without ':' separator")
            (key, value) = header.split(":", 1)
            headers[key] = value
            i += 1
        if i >= len(lines):
            raise MalformedFrameError("missing header/body separator")

        # set body to None if there is no body; a missing body section is
        # malformed, not an IndexError
        if i + 1 >= len(lines):
            raise MalformedFrameError("missing body section")
        body = None if lines[i + 1] == Byte["NULL"] else lines[i + 1][:-1]

        return Frame(command, headers, body)

    @staticmethod
    def marshall(command, headers, body):
        return str(Frame(command, headers, body)) + Byte["NULL"]
