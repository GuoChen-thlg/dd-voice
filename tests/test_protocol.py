"""protocol 编解码单元测试（离线，不依赖网络）。"""
import unittest

from ddvoice.protocol import ProtocolError, decode_response, encode_request, varint


class VarintTest(unittest.TestCase):
    def test_varint_边界(self):
        self.assertEqual(varint(0), b'\x00')
        self.assertEqual(varint(127), b'\x7f')
        self.assertEqual(varint(128), b'\x80\x01')
        self.assertEqual(varint(2**64 - 1), b'\xff' * 9 + b'\x01')

    def test_varint_非法输入(self):
        with self.assertRaises(ProtocolError):
            varint(-1)
        with self.assertRaises(ProtocolError):
            varint(2**64)
        with self.assertRaises(ProtocolError):
            varint('x')


class EncodeTest(unittest.TestCase):
    def test_编码包含各字段(self):
        packet = encode_request('StartTask', 'APPKEY', task_id='t1',
                                payload={'a': 1}, audio=b'\x01\x02', sequence=3)
        self.assertIsInstance(packet, bytes)
        self.assertIn(b'StartTask', packet)
        self.assertIn(b'APPKEY', packet)
        self.assertIn(b'{"a": 1}', packet)
        self.assertIn(b'\x01\x02', packet)

    def test_无可选字段(self):
        packet = encode_request('EndASR', 'APPKEY')
        self.assertIn(b'EndASR', packet)


class DecodeTest(unittest.TestCase):
    def test_json_文本帧(self):
        result = decode_response('{"event": "TaskStarted"}')
        self.assertEqual(result['event'], 'TaskStarted')

    def test_json_非法(self):
        with self.assertRaises(ProtocolError):
            decode_response('not json')
        with self.assertRaises(ProtocolError):
            decode_response('[1,2]')

    def test_二进制往返(self):
        # encode 产生的是合法 protobuf 字段序列，decode 应能还原已知字段
        data = varint(4 << 3 | 2) + varint(11) + b'TaskStarted'
        result = decode_response(data)
        self.assertEqual(result['event'], 'TaskStarted')

    def test_截断数据报错(self):
        with self.assertRaises(ProtocolError):
            decode_response(b'\x22\x05abc')

    def test_未知字段跳过(self):
        data = varint(99 << 3 | 0) + varint(1)
        data += varint(4 << 3 | 2) + varint(2) + b'OK'
        self.assertEqual(decode_response(data)['event'], 'OK')


if __name__ == '__main__':
    unittest.main()
