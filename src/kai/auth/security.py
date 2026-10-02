import base64
import hashlib
import hmac
import secrets

_ALGO = "scrypt"
_N = 2**14
_R = 8
_P = 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P
    )
    return "{}${}${}${}${}${}".format(
        _ALGO,
        _N,
        _R,
        _P,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, n, r, p, salt_b64, digest_b64 = encoded.split("$")
        if algo != _ALGO:
            return False
        # The stored hash is trusted to describe a hash this service produced.
        # Without a floor, a row whose parameters were weakened to n=2 (or a
        # hand-edited value far above _N) would be verified as-is: a downgrade
        # to a near-zero cost makes every stored password cheap to crack
        # offline, and a huge n turns verification into a CPU-exhaustion
        # primitive on the unauthenticated login route. Reject anything that
        # is not exactly the cost this module hashes at.
        if (int(n), int(r), int(p)) != (_N, _R, _P):
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False
