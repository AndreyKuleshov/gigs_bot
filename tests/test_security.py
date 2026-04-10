"""Tests for encryption/decryption helpers."""

import pytest

from app.core.security import decrypt_json, encrypt_json


class TestEncryptDecrypt:
    def test_roundtrip(self):
        data = {"token": "abc123", "refresh_token": "xyz"}
        encrypted = encrypt_json(data)
        assert isinstance(encrypted, str)
        assert encrypted != str(data)
        result = decrypt_json(encrypted)
        assert result == data

    def test_different_ciphertexts(self):
        data = {"key": "value"}
        a = encrypt_json(data)
        b = encrypt_json(data)
        # Fernet uses random IV so ciphertexts should differ
        assert a != b

    def test_tampered_token_raises(self):
        encrypted = encrypt_json({"a": 1})
        tampered = encrypted[:-5] + "XXXXX"
        with pytest.raises(ValueError, match="Cannot decrypt"):
            decrypt_json(tampered)

    def test_garbage_input_raises(self):
        with pytest.raises(ValueError, match="Cannot decrypt"):
            decrypt_json("not-a-valid-token")
