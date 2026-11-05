"""server 模块测试：用 httpx ASGITransport 离线测试 HTTP 接口。"""
import io

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from ddvoice import server
from ddvoice.server import app


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as c:
        yield c


@pytest.mark.asyncio
async def test_health(client):
    r = await client.get('/health')
    assert r.status_code == 200
    body = r.json()
    assert body['status'] == 'ok'
    assert body['backend'] == 'doubao-web-anonymous'


@pytest.mark.asyncio
async def test_models(client):
    r = await client.get('/v1/models')
    assert r.status_code == 200
    ids = [m['id'] for m in r.json()['data']]
    assert 'doubao-voice' in ids
    assert 'whisper-1' in ids


@pytest.mark.asyncio
async def test_缺少file字段(client):
    r = await client.post('/v1/audio/transcriptions', data={'model': 'doubao-voice'})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_不支持的model(client):
    files = {'file': ('a.wav', b'data', 'audio/wav')}
    r = await client.post('/v1/audio/transcriptions', data={'model': 'gpt-4'}, files=files)
    assert r.status_code == 400
    assert '仅支持' in r.json()['error']['message']


@pytest.mark.asyncio
async def test_不支持的response_format(client):
    files = {'file': ('a.wav', b'data', 'audio/wav')}
    r = await client.post('/v1/audio/transcriptions',
                          data={'model': 'doubao-voice', 'response_format': 'verbose_json'},
                          files=files)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_不支持流式(client):
    files = {'file': ('a.wav', b'data', 'audio/wav')}
    r = await client.post('/v1/audio/transcriptions',
                          data={'model': 'doubao-voice', 'stream': 'true'},
                          files=files)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_空文件(client):
    files = {'file': ('a.wav', b'', 'audio/wav')}
    r = await client.post('/v1/audio/transcriptions',
                          data={'model': 'doubao-voice'}, files=files)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_鉴权启用后无key被拒(client, monkeypatch):
    monkeypatch.setenv('DDVOICE_API_KEY', 'secret')
    r = await client.post('/v1/audio/transcriptions',
                          data={'model': 'doubao-voice'},
                          files={'file': ('a.wav', b'x', 'audio/wav')})
    assert r.status_code == 401
    monkeypatch.delenv('DDVOICE_API_KEY', raising=False)


@pytest.mark.asyncio
async def test_鉴权启用后正确key通过(client, monkeypatch):
    monkeypatch.setenv('DDVOICE_API_KEY', 'secret')
    # 缺少 file 字段会在鉴权之后被 400 拦截，说明鉴权已通过
    r = await client.post('/v1/audio/transcriptions',
                          headers={'Authorization': 'Bearer secret'},
                          data={'model': 'doubao-voice'})
    assert r.status_code == 400
    monkeypatch.delenv('DDVOICE_API_KEY', raising=False)


@pytest.mark.asyncio
async def test_完整转写流程(client, monkeypatch):
    """Mock 底层 transcribe_pcm，验证 JSON 与 text 两种响应格式。"""
    class FakeResult:
        text = '测试转写结果'
    monkeypatch.setattr(server, '_decode_audio', lambda data: (b'\x00' * 32000, 1.0))
    monkeypatch.setattr(server, 'transcribe_pcm', lambda pcm, duration: FakeResult())
    files = {'file': ('a.wav', b'fake-wav-bytes', 'audio/wav')}
    # json 格式
    r = await client.post('/v1/audio/transcriptions',
                          data={'model': 'doubao-voice'}, files=files)
    assert r.status_code == 200
    assert r.json()['text'] == '测试转写结果'
    # text 格式
    r2 = await client.post('/v1/audio/transcriptions',
                           data={'model': 'doubao-voice', 'response_format': 'text'}, files=files)
    assert r2.status_code == 200
    assert r2.text == '测试转写结果'
    assert 'text/plain' in r2.headers['content-type']


@pytest.mark.asyncio
async def test_转写失败返回502(client, monkeypatch):
    from ddvoice.asr import ASRError
    monkeypatch.setattr(server, '_decode_audio', lambda data: (b'\x00' * 32000, 1.0))
    monkeypatch.setattr(server, 'transcribe_pcm',
                        lambda pcm, duration: (_ for _ in ()).throw(ASRError('语音服务不可用')))
    files = {'file': ('a.wav', b'fake', 'audio/wav')}
    r = await client.post('/v1/audio/transcriptions',
                          data={'model': 'doubao-voice'}, files=files)
    assert r.status_code == 502
    assert '语音服务不可用' in r.json()['error']['message']
