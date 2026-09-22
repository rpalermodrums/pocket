"""Strict packet-framed Opus decode; generated media only, no network."""
import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from pocket_music import acquisition as acq


@pytest.fixture
def media(tmp_path):
    ff, probe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ff or not probe:
        pytest.skip('Optional FFmpeg tools unavailable')
    wav = tmp_path / 'source.wav'
    signal = .12 * np.sin(2 * np.pi * 431 * np.arange(48000) / 48000)
    sf.write(wav, np.column_stack((signal, signal)), 48000, subtype='FLOAT')
    webm = tmp_path / 'source.webm'
    result = subprocess.run([ff, '-v', 'error', '-n', '-i', str(wav), '-c:a', 'libopus',
                             str(webm)], capture_output=True, text=True, check=False)
    if result.returncode:
        pytest.skip('Optional libopus encoder unavailable')
    return ff, probe, webm, wav


def executable(tmp_path, source):
    target = tmp_path / ('extractor-' + source.suffix[1:])
    info = {'id': 'synthetic', 'format_id': 'synthetic', 'duration': 1, 'title': 'Generated fixture'}
    target.write_text(f'''#!{sys.executable}
import json, shutil, sys
from pathlib import Path
args = sys.argv[1:]
info = {info!r}
if '--version' in args:
    print('synthetic-extractor')
elif '--dump-single-json' in args:
    print(json.dumps(info))
elif '--write-info-json' in args:
    folder = Path(args[args.index('-o') + 1]).parent
    shutil.copyfile({str(source)!r}, folder / {('original' + source.suffix)!r})
    (folder / 'original.info.json').write_text(json.dumps(info))
else:
    raise SystemExit(2)
''')
    target.chmod(0o700)
    return target


def arguments(tmp_path, media, source):
    ff, probe, _, _ = media
    plan = acq.plan_acquisition('https://example.invalid/generated', str(tmp_path / 'plan'),
                                version_note='Synthetic media', authorization_note='Generated test',
                                expected_source_id='synthetic')
    return {'plan_dir': plan['plan_dir'], 'expected_plan_sha256': plan['sha256'],
            'yt_dlp_executable': str(executable(tmp_path, source)),
            'ffmpeg_executable': ff, 'ffprobe_executable': probe}


def test_real_webm_provider_cli_stdio_parity(media, tmp_path):
    args = arguments(tmp_path, media, media[2])
    before = media[2].read_bytes()
    direct = acq.acquire_source(**args, output_dir=str(tmp_path / 'direct'))
    spec = tmp_path / 'args.json'
    spec.write_text(json.dumps(args))
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1] / 'src')}
    cli = subprocess.run([sys.executable, '-m', 'pocket_music.cli', 'acquire', 'run',
                          '--spec', str(spec), '--output', str(tmp_path / 'cli')],
                         capture_output=True, text=True, env=env, timeout=60, check=False)
    assert cli.returncode == 0, cli.stderr

    async def call():
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        async with stdio_client(StdioServerParameters(command=sys.executable,
                                args=['-m', 'pocket_music.mcp_server'], env=env)) as (r, w), ClientSession(r, w) as session:
            await asyncio.wait_for(session.initialize(), 60)
            reply = await asyncio.wait_for(session.call_tool('acquire_source',
                {**args, 'output_dir': str(tmp_path / 'mcp')}), 60)
            assert not reply.isError, reply
            return json.loads(reply.content[0].text)

    results = [direct, json.loads(cli.stdout), asyncio.run(call())]
    for result in results:
        assert result['status'] == 'completed', result
        assert result['decode_configuration'] == 'packet_framed_matroska_opus_v1'
        cmd = result['decode_command']
        assert cmd[cmd.index('-fflags') + 1] == '+noparse+nofillin'
        assert cmd.index('-fflags') < cmd.index('-i')
        assert result['decode_diagnostic'] == '' and result['decode_exit_code'] == 0
        assert result['decoded']['frames'] == 48000
        assert result['decoded']['sha256'] == direct['decoded']['sha256']
        assert result['original']['sha256'] == direct['original']['sha256']
    assert media[2].read_bytes() == before


def test_different_codec_preserves_default_strict_decode(media, tmp_path):
    args = arguments(tmp_path, media, media[3])
    result = acq.acquire_source(**args, output_dir=str(tmp_path / 'result'))
    assert result['status'] == 'completed'
    assert result['decode_configuration'] == 'strict_default_v1'
    assert '-fflags' not in result['decode_command']


@pytest.mark.parametrize('mutation', ['invalid_packet', 'truncated'])
def test_corrupt_webm_never_published(media, tmp_path, mutation):
    raw = media[2].read_bytes()
    if mutation == 'truncated':
        damaged = raw[:-300]
    else:
        # Locate a real packet by ffprobe's demuxed hex dump, then change its
        # Opus frame-count code to a value exceeding the codec's 120ms bound.
        probe = subprocess.run([media[1], '-v', 'error', '-show_packets', '-show_data',
                                '-of', 'json', str(media[2])], capture_output=True, text=True, check=True)
        packets = json.loads(probe.stdout)['packets']
        packet = None
        for row in packets:
            hexes = ''.join(line.split(':', 1)[1].split('  ')[0].replace(' ', '')
                            for line in row['data'].strip().splitlines())
            candidate = bytes.fromhex(hexes)
            if len(candidate) > 100 and raw.count(candidate) == 1:
                packet = candidate
                break
        assert packet is not None
        offset = raw.index(packet)
        damaged = raw[:offset] + b'\xff\x3f' + raw[offset + 2:]
    source = tmp_path / 'damaged.webm'
    source.write_bytes(damaged)
    args = arguments(tmp_path, media, source)
    result = acq.acquire_source(**args, output_dir=str(tmp_path / 'result'))
    assert result['status'] == 'failed', result
    assert not result['ready_for_analysis']
    assert not (tmp_path / 'result' / 'decoded.wav').exists()
    assert (tmp_path / 'result' / '.staging').is_dir()
    assert media[2].read_bytes() == raw


def test_ogg_opus_does_not_enable_matroska_configuration(media, tmp_path):
    ogg = tmp_path / 'source.ogg'
    subprocess.run([media[0], '-v', 'error', '-n', '-i', str(media[2]), '-c:a', 'copy',
                    str(ogg)], capture_output=True, check=True)
    args = arguments(tmp_path, media, ogg)
    result = acq.acquire_source(**args, output_dir=str(tmp_path / 'result'))
    assert result['decode_configuration'] == 'strict_default_v1'
    assert '-fflags' not in result['decode_command']


def test_multiple_opus_streams_refuse_before_decode(media, tmp_path):
    multi = tmp_path / 'multiple.webm'
    subprocess.run([media[0], '-v', 'error', '-n', '-i', str(media[2]), '-map', '0:a:0',
                    '-map', '0:a:0', '-c:a', 'copy', str(multi)], capture_output=True, check=True)
    args = arguments(tmp_path, media, multi)
    result = acq.acquire_source(**args, output_dir=str(tmp_path / 'result'))
    assert result['status'] == 'failed'
    assert 'unambiguous audio stream' in result['reason']
    assert 'decode_command' not in result
