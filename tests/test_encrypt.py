"""encrypt.py 测试"""

from src.utils.encrypt import JSEncryptPython

RSA_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA0orxr+Larwt3bqq0yt5D
DgNlOh3D5kDSmidNbr3nHe/ktgr4sTWoVJAFtn2fgLB6e9zf571eeOJJ4hqp5Su2
RRTOhOojE98gEjBAi1fB7OPLR0d2TYzE/P9ahaOhT89noIGQz+Pu2n9wBK/7dg6A
MeJ51Edn4p4WlP+XKWyfH78T6v5hQ9snt5Vtz5wbpEOu+X414ENswIAhLCOCqBzj
khNqfJG/fNH/SjsjbmsqCdedirZAu8DYWBPv1x+vFn7hBOd2G40FnsWAAR8ekHgB
b+wB0DkHlDhIGK6QmbVZh4vKCcPk4QDrGY3rQPGrECGqmIi9BZK75sUeNTec6jp6
gQIDAQAB
-----END PUBLIC KEY-----"""


class TestJSEncryptPython:
    def test_encrypt_returns_base64_string(self):
        rsa = JSEncryptPython()
        rsa.setPublicKey(RSA_PEM)
        result = rsa.encrypt("hello")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_encrypt_different_inputs_produce_different_outputs(self):
        rsa = JSEncryptPython()
        rsa.setPublicKey(RSA_PEM)
        a = rsa.encrypt("password1")
        b = rsa.encrypt("password2")
        assert a != b

    def test_encrypt_same_input_produces_different_outputs(self):
        """RSA PKCS1 v1.5 包含随机填充，相同输入产生不同密文"""
        rsa = JSEncryptPython()
        rsa.setPublicKey(RSA_PEM)
        a = rsa.encrypt("same")
        b = rsa.encrypt("same")
        assert a != b

    def test_encrypt_without_key_returns_false(self):
        rsa = JSEncryptPython()
        assert rsa.encrypt("test") is False
