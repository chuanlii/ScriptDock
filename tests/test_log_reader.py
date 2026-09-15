import io

from core.log_reader import read_pipe


def test_streams_and_replacement():
    lines = []
    read_pipe(io.BytesIO('第一行\n第二行'.encode('utf-8') + b'\xff'), lines.append)
    assert lines == ['第一行', '第二行�']


def test_unbroken_output_is_bounded():
    lines = []
    read_pipe(io.BytesIO(b'x' * 100000), lines.append)
    assert sum(map(len, lines)) == 100000
    assert max(map(len, lines)) <= 4096


def test_empty_pipe():
    lines = []
    read_pipe(io.BytesIO(), lines.append)
    assert not lines
