"""asr 模块离线测试：校验参数、音频加载与游客初始化错误处理。"""
import unittest
from unittest import mock

from ddvoice import asr
from ddvoice.asr import ASRError, load_wav, MAX_AUDIO_BYTES


class LoadWavTest(unittest.TestCase):
    def test_不存在文件报错(self):
        with self.assertRaises(ASRError):
            load_wav('/not/exist.wav')

    def test_文件过大报错(self):
        with mock.patch('ddvoice.asr.Path') as mp:
            mp.return_value.stat.return_value.st_size = MAX_AUDIO_BYTES + 1
            with self.assertRaises(ASRError):
                load_wav('/fake/path.wav')


class TranscribePcmValidationTest(unittest.TestCase):
    def test_超时范围校验(self):
        with self.assertRaises(ASRError):
            asr.transcribe_pcm(b'\x00' * 100, timeout=0)
        with self.assertRaises(ASRError):
            asr.transcribe_pcm(b'\x00' * 100, timeout=301)

    def test_pcm格式校验(self):
        with self.assertRaises(ASRError):
            asr.transcribe_pcm('not bytes')
        with self.assertRaises(ASRError):
            asr.transcribe_pcm(b'')
        with self.assertRaises(ASRError):
            asr.transcribe_pcm(b'\x00\x01\x02')  # 奇数长度


class BootstrapGuestTest(unittest.TestCase):
    @mock.patch('ddvoice.asr.urllib.request.build_opener')
    def test_首页缺少路由数据报错(self, mock_build):
        mock_opener = mock.MagicMock()
        mock_opener.open.return_value.__enter__.return_value.read.return_value = b'no router data'
        mock_build.return_value = mock_opener
        with self.assertRaises(ASRError):
            asr.bootstrap_guest(timeout=5)

    @mock.patch('ddvoice.asr.urllib.request.build_opener')
    def test_网络异常报错(self, mock_build):
        mock_opener = mock.MagicMock()
        mock_opener.open.side_effect = OSError('connection refused')
        mock_build.return_value = mock_opener
        with self.assertRaises(ASRError):
            asr.bootstrap_guest(timeout=5)


class TranscribePcmErrorPathTest(unittest.TestCase):
    """Mock 掉底层 websocket 与 bootstrap，验证关键错误分支。"""

    @mock.patch('websocket.create_connection')
    @mock.patch('ddvoice.asr.bootstrap_guest')
    def test_连接失败报错(self, mock_bootstrap, mock_ws):
        mock_bootstrap.return_value = {}
        mock_ws.side_effect = OSError('connection failed')
        with self.assertRaises(ASRError) as ctx:
            asr.transcribe_pcm(b'\x00' * 6400, duration=0.2)
        self.assertIn('语音连接失败', str(ctx.exception))

    @mock.patch.dict('sys.modules', {'websocket': None})
    @mock.patch('ddvoice.asr.bootstrap_guest')
    def test_依赖缺失报错(self, mock_bootstrap):
        mock_bootstrap.return_value = {}
        with self.assertRaises(ASRError) as ctx:
            asr.transcribe_pcm(b'\x00' * 6400, duration=0.2)
        self.assertIn('websocket-client', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
