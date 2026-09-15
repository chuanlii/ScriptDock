"""Blocking pipe readers run only on background threads."""
import codecs


def read_pipe(pipe, emit):
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    try:
        while chunk := pipe.read(4096):
            pending += decoder.decode(chunk)
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                emit(line.rstrip("\r"))
            # Preserve CRLF across read boundaries. Bound very long lines.
            while len(pending) >= 4096:
                emit(pending[:4096])
                pending = pending[4096:]
        tail = decoder.decode(b"", final=True)
        if pending or tail:
            emit(pending + tail)
    except (OSError, ValueError):
        pass
    finally:
        pipe.close()
