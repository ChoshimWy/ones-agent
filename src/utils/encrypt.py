"""RSA 密码加密 (PKCS#1 v1.5)"""

import base64
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5


class JSEncryptPython:
    def __init__(self):
        self.key = None

    def setPublicKey(self, pem: str) -> None:
        if not pem.strip().startswith("-----BEGIN"):
            pem = f"-----BEGIN RSA PUBLIC KEY-----\n{pem}\n-----END RSA PUBLIC KEY-----"
        self.key = RSA.import_key(pem)

    setPrivateKey = setPublicKey

    def encrypt(self, plaintext: str) -> str | bool:
        try:
            cipher = PKCS1_v1_5.new(self.key)
            return base64.b64encode(cipher.encrypt(plaintext.encode())).decode()
        except Exception:
            return False
