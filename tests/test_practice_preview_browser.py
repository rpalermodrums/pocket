"""Optional decoder qualification: Chromium recovers the exact PCM16 integers in declared previews.

This establishes the encoded input a browser player receives for each declared
rate. It does not establish device output, other browsers or human listening.
Skipped unless the optional Playwright package and a Chromium build are present.
"""
import base64
import importlib.util

import numpy as np
import pytest

from pocket_music.practice_previews import SUPPORTED_RATES, pcm16_wav

pytestmark = pytest.mark.skipif(importlib.util.find_spec('playwright') is None,
                                reason='Optional browser qualification requires Playwright')

DECODE = """async ({data, rate, channels, frames}) => {
  const bytes = Uint8Array.from(atob(data), c => c.charCodeAt(0));
  const context = new OfflineAudioContext(channels, frames, rate);
  const buffer = await context.decodeAudioData(bytes.buffer);
  const out = [];
  for (let c = 0; c < buffer.numberOfChannels; c++) out.push(Array.from(buffer.getChannelData(c)));
  const element = new Audio('data:audio/wav;base64,' + data);
  const playable = await new Promise(resolve => {
    element.addEventListener('loadedmetadata', () => resolve(element.duration), {once: true});
    element.addEventListener('error', () => resolve(null), {once: true});
  });
  return {rate: buffer.sampleRate, length: buffer.length, channels: buffer.numberOfChannels, out, playable};
}"""


@pytest.fixture(scope='module')
def page():
    from playwright.sync_api import Error, sync_playwright
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Error as error:
            pytest.skip(f'Chromium unavailable: {error}')
        yield browser.new_page()
        browser.close()


@pytest.mark.parametrize('rate', SUPPORTED_RATES)
@pytest.mark.parametrize('channels', [1, 2])
def test_chromium_decodes_exact_pcm16_values_at_declared_rate(page, rate, channels):
    frames = 257
    values = np.zeros((frames, channels), dtype='<i2')
    values[:, 0] = np.linspace(-32768, 32767, frames).round().astype(int)
    if channels == 2:
        values[:, 1] = -values[::-1, 0] // 3  # asymmetric right channel exposes interleaving errors
    data = base64.b64encode(pcm16_wav(values, rate)).decode()
    result = page.evaluate(DECODE, {'data': data, 'rate': rate, 'channels': channels, 'frames': frames})
    assert (result['rate'], result['length'], result['channels']) == (rate, frames, channels)
    assert_chromium_integers(result, values)
    assert result['playable'] == pytest.approx(frames / rate, abs=1e-3)


def assert_chromium_integers(result, values):
    decoded = np.array(result['out'], dtype=np.float64).T
    # Chromium scales asymmetrically by float32 reciprocals: 1/32768 below zero,
    # 1/32767 above. Pocket's decoded evidence uses k/32768 throughout, so browser
    # floats differ on positive samples, but every integer is recovered exactly.
    integers = values.astype(np.float32)
    chromium = np.where(integers < 0, integers * np.float32(1 / 32768), integers * np.float32(1 / 32767))
    assert np.array_equal(decoded, chromium.astype(np.float64))
    recovered = np.where(decoded < 0, np.rint(decoded * 32768), np.rint(decoded * 32767))
    assert np.array_equal(recovered, values.astype(np.float64))


def test_chromium_recovers_every_pcm16_integer(page):
    values = np.arange(-32768, 32768).reshape(-1, 2).astype('<i2')
    data = base64.b64encode(pcm16_wav(values, 48000)).decode()
    result = page.evaluate(DECODE, {'data': data, 'rate': 48000, 'channels': 2, 'frames': len(values)})
    assert_chromium_integers(result, values)
