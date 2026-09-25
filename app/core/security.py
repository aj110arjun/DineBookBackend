from pwdlib import PasswordHash

_password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_secret(secret: str, secret_hash: str) -> bool:
    return _password_hash.verify(secret, secret_hash)
