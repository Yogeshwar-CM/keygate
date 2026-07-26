from keygate import keys


def test_minted_key_has_expected_shape():
    minted = keys.mint()
    assert minted.plaintext.startswith("kg_v1_")
    assert keys.looks_like_key(minted.plaintext)
    assert minted.display_prefix == minted.plaintext[:14]
    assert len(minted.key_hash) == 64


def test_keys_are_unique():
    minted = {keys.mint().plaintext for _ in range(200)}
    assert len(minted) == 200


def test_hash_is_deterministic_and_hides_the_key():
    minted = keys.mint()
    assert keys.hash_key(minted.plaintext) == minted.key_hash
    assert minted.plaintext not in minted.key_hash
    assert keys.hash_key(minted.plaintext + "x") != minted.key_hash


def test_matches():
    minted = keys.mint()
    assert keys.matches(minted.plaintext, minted.key_hash)
    assert not keys.matches(keys.mint().plaintext, minted.key_hash)


def test_looks_like_key_rejects_impostors():
    assert not keys.looks_like_key("")
    assert not keys.looks_like_key("sk-proj-abcdef")
    assert not keys.looks_like_key("kg_v1_short")
    assert not keys.looks_like_key("kg_v2_" + "a" * 43)
    assert not keys.looks_like_key("kg_v1_" + "a" * 43 + "trailing")
    assert not keys.looks_like_key("kg_v1_" + "!" * 43)


def test_parse_bearer():
    assert keys.parse_bearer("Bearer abc") == "abc"
    assert keys.parse_bearer("bearer abc") == "abc"
    assert keys.parse_bearer("  Bearer   abc  ") == "abc"
    assert keys.parse_bearer("abc") == "abc"
    assert keys.parse_bearer("Bearer ") is None
    assert keys.parse_bearer("") is None
    assert keys.parse_bearer(None) is None
